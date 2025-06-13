import os
import json
import base64
import asyncio
import websockets
import audioop
from urllib.parse import parse_qs

from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect
from dotenv import load_dotenv

load_dotenv()

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PORT = int(os.getenv("PORT", 8000))  # Cloud Run default
OPENAI_PCM_SAMPLE_RATE = 24000        # GPT‑4o realtime output
TWILIO_MULAW_SAMPLE_RATE = 8000       # µ‑law for Twilio

if not OPENAI_API_KEY:
    raise RuntimeError("FATAL: OPENAI_API_KEY env var missing.")

app = FastAPI()

# ──────────────────────────────────────────────
# 0️⃣  Health check
# ──────────────────────────────────────────────
@app.get("/", response_class=JSONResponse)
async def health_check():
    return {"status": "ok", "msg": "Tip‑line voice assistant running."}

# ──────────────────────────────────────────────
# 1️⃣  Greeting + language selection (DTMF)
# ──────────────────────────────────────────────
@app.api_route("/incoming-call", methods=["GET", "POST"])
async def incoming_call(_: Request):
    vr = VoiceResponse()
    gather = vr.gather(action="/language-selection", method="POST", num_digits=1, timeout=5)
    gather.say("You’ve reached the employee tip line. For English, stay on the line.", voice="Polly.Matthew")
    gather.say("Para español, presione uno.", voice="Polly.Lupe")
    vr.redirect("/language-selection")  # fall‑through if no input
    return HTMLResponse(str(vr), media_type="application/xml")

# ──────────────────────────────────────────────
# 2️⃣  Branch based on DTMF
# ──────────────────────────────────────────────
@app.api_route("/language-selection", methods=["GET", "POST"])
async def language_selection(request: Request):
    body = (await request.body()).decode()
    digits = parse_qs(body).get("Digits", [""])[0]
    lang = "es" if digits.strip() == "1" else "en"
    host = request.headers.get("x-forwarded-host", request.url.hostname)
    ws_url = f"wss://{host}/media-stream?lang={lang}"
    connect = Connect()
    connect.stream(url=ws_url)
    vr = VoiceResponse()
    vr.append(connect)
    return HTMLResponse(str(vr), media_type="application/xml")

# ──────────────────────────────────────────────
# 3️⃣  Media Stream bridge
# ──────────────────────────────────────────────
@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()
    lang = websocket.query_params.get("lang", "en")

    try:
        async with websockets.connect(
            "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01",
            extra_headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "OpenAI-Beta": "realtime=v1",
            },
        ) as openai_ws:

            print("🔗 Connected to OpenAI realtime WS – lang:", lang)

            # Configure session *first*
            await initialize_session(openai_ws, lang)
            await asyncio.sleep(0.5)  # let caller audio begin flowing
            await send_initial_greeting(openai_ws, lang)

            stream_sid = None
            rate_state = None  # audioop state for ratecv

            # — Twilio → OpenAI —
            async def twilio_to_openai():
                nonlocal stream_sid
                try:
                    async for raw in websocket.iter_text():
                        data = json.loads(raw)
                        evt = data.get("event")

                        if evt == "start":
                            stream_sid = data["start"]["streamSid"]
                            print("🔊 Twilio stream started", stream_sid)

                        elif evt == "media" and openai_ws.open:
                            print("⬆️  Forwarding audio chunk to OpenAI")
                            await openai_ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": data["media"]["payload"],
                            }))
                except WebSocketDisconnect:
                    print("⚠️  Twilio WebSocket disconnected")
                    if openai_ws.open:
                        await openai_ws.close()

            # — OpenAI → Twilio —
            async def openai_to_twilio():
                nonlocal rate_state
                try:
                    async for oa_raw in openai_ws:
                        oa = json.loads(oa_raw)
                        if oa.get("type") == "response.audio.delta" and "delta" in oa:
                            # 24 kHz PCM → 8 kHz µ‑law
                            pcm24 = base64.b64decode(oa["delta"])
                            pcm8, rate_state = audioop.ratecv(
                                pcm24, 2, 1,
                                OPENAI_PCM_SAMPLE_RATE,
                                TWILIO_MULAW_SAMPLE_RATE,
                                rate_state,
                            )
                            mulaw = audioop.lin2ulaw(pcm8, 2)
                            await websocket.send_json({
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": base64.b64encode(mulaw).decode()},
                            })
                except websockets.exceptions.ConnectionClosed:
                    print("ℹ️  OpenAI WS closed")

            await asyncio.gather(twilio_to_openai(), openai_to_twilio())

    except Exception as e:
        print("CRITICAL: media_stream error:", e)

# ──────────────────────────────────────────────
# 4️⃣  OpenAI helper functions
# ──────────────────────────────────────────────
async def initialize_session(openai_ws, lang: str):
    sys_prompt = (
        "You are a calm, neutral AI operator for an anonymous employee tip line. "
        "Wait until the caller finishes speaking before asking a follow‑up. "
        "Gather: who, what, when, where, evidence. "
        "One concise question at a time. "
        f"Speak only in {'Spanish' if lang == 'es' else 'English'}."
    )
    await openai_ws.send(json.dumps({
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad"},
            "instructions": sys_prompt,
        },
    }))
    print("✅ Sent session.update to OpenAI")

async def send_initial_greeting(openai_ws, lang: str):
    prompts = {
        "en": "Thank you for calling the anonymous tip line. How can I assist you today?",
        "es": "Gracias por llamar a la línea de denuncias anónimas. ¿Cómo puedo ayudarle hoy?",
    }
    await openai_ws.send(json.dumps({
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": prompts.get(lang, prompts['en'])}],
        },
    }))
    await openai_ws.send(json.dumps({"type": "response.create"}))
    print("👋 Initial greeting queued for OpenAI")

# ──────────────────────────────────────────────
# 5️⃣  Entrypoint
# ──────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
