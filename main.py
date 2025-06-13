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

# ---------------- Configuration ----------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PORT = int(os.getenv("PORT", 8000))  # Google Cloud Run default
OPENAI_PCM_SAMPLE_RATE = 24000  # GPT‑4o realtime returns 24 kHz PCM
TWILIO_MULAW_SAMPLE_RATE = 8000  # Twilio expects 8 kHz µ‑law

if not OPENAI_API_KEY:
    raise RuntimeError("FATAL: OPENAI_API_KEY not found in environment variables.")

app = FastAPI()

# ---------------- Health check ----------------
@app.get("/", response_class=JSONResponse)
async def health_check():
    return {"message": "Tip‑line voice assistant is running."}

# ---------------- Greeting + language menu ----------------
@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(_: Request):
    vr = VoiceResponse()
    gather = vr.gather(action="/language-selection", method="POST", num_digits=1, timeout=5)
    gather.say("You've reached the employee tip line. For English, stay on the line.", voice="Polly.Matthew")
    gather.say("Para español, presione uno.", voice="Polly.Lupe")
    vr.redirect("/language-selection")
    return HTMLResponse(str(vr), media_type="application/xml")

# ---------------- Branch on DTMF ----------------
@app.api_route("/language-selection", methods=["GET", "POST"])
async def language_selection(request: Request):
    body = (await request.body()).decode()
    digits = parse_qs(body).get("Digits", [""])[0]
    lang = "es" if digits.strip() == "1" else "en"
    host = request.headers.get("x-forwarded-host", request.url.hostname)  # Cloud Run passes host header
    ws_url = f"wss://{host}/media-stream?lang={lang}"
    connect = Connect()
    connect.stream(url=ws_url)
    vr = VoiceResponse()
    vr.append(connect)
    return HTMLResponse(str(vr), media_type="application/xml")

# ---------------- Media Stream ----------------
@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
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

            # 1️⃣  Configure session then greet
            await initialize_session(openai_ws, lang)
            await asyncio.sleep(0.1)  # let OpenAI apply settings
            await send_initial_greeting(openai_ws, lang)

            stream_sid = None
            rate_state = None  # for audioop.ratecv

            async def twilio_to_openai():
                nonlocal stream_sid
                try:
                    async for msg in websocket.iter_text():
                        data = json.loads(msg)
                        if data.get("event") == "start":
                            stream_sid = data["start"]["streamSid"]
                        elif data.get("event") == "media" and openai_ws.open:
                            await openai_ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": data["media"]["payload"],
                            }))
                except WebSocketDisconnect:
                    if openai_ws.open:
                        await openai_ws.close()

            async def openai_to_twilio():
                nonlocal rate_state
                try:
                    async for oa_raw in openai_ws:
                        oa = json.loads(oa_raw)
                        if oa.get("type") == "response.audio.delta" and "delta" in oa:
                            # 24 kHz PCM → 8 kHz µ‑law
                            pcm_24k = base64.b64decode(oa["delta"])
                            pcm_8k, rate_state = audioop.ratecv(pcm_24k, 2, 1, OPENAI_PCM_SAMPLE_RATE, TWILIO_MULAW_SAMPLE_RATE, rate_state)
                            mulaw = audioop.lin2ulaw(pcm_8k, 2)
                            await websocket.send_json({
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": base64.b64encode(mulaw).decode()},
                            })
                except websockets.exceptions.ConnectionClosed:
                    pass

            await asyncio.gather(twilio_to_openai(), openai_to_twilio())

    except Exception as e:
        print("CRITICAL: WebSocket loop error:", e)

# ---------------- OpenAI helpers ----------------
async def initialize_session(openai_ws, lang: str):
    system_prompt = (
        "You are a professional, calm, neutral AI operator for an anonymous employee tip line at a retail company. "
        "Gather the who, what, when, where, and any evidence. "
        "Ask one concise question at a time. "
        "Speak clearly and slowly. "
        f"Respond only in {'Spanish' if lang == 'es' else 'English'}."
    )
    await openai_ws.send(json.dumps({
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad"},
            "instructions": system_prompt,
        },
    }))

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

# ---------------- Entrypoint (local) ----------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)

