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
PORT = int(os.getenv("PORT", 8000))
OPENAI_PCM_SAMPLE_RATE = 24000
TWILIO_MULAW_SAMPLE_RATE = 8000

if not OPENAI_API_KEY:
    raise RuntimeError("FATAL: OPENAI_API_KEY env var missing.")

app = FastAPI()

# ──────────────────────────────────────────────
# 0️⃣  Health check
# ──────────────────────────────────────────────
@app.get("/", response_class=JSONResponse)
async def health_check():
    return {"status": "ok"}

# ──────────────────────────────────────────────
# 1️⃣  Greeting + language menu
# ──────────────────────────────────────────────
@app.api_route("/incoming-call", methods=["GET", "POST"])
async def incoming_call(_: Request):
    vr = VoiceResponse()
    gather = vr.gather(action="/language-selection", method="POST", num_digits=1, timeout=5)
    gather.say("You’ve reached the employee tip line. For English, stay on the line.", voice="Polly.Matthew")
    gather.pause(length=1)
    gather.say("Para español, presione uno.", voice="Polly.Lupe")
    vr.redirect("/language-selection")
    return HTMLResponse(str(vr), media_type="application/xml")

# ──────────────────────────────────────────────
# 2️⃣  Branch based on DTMF
# ──────────────────────────────────────────────
@app.api_route("/language-selection", methods=["GET", "POST"])
async def language_selection(request: Request):
    body = (await request.body()).decode()
    digits = parse_qs(body).get("Digits", [""])[0]
    lang = "es" if digits.strip() == "1" else "en"
    print("🌐 Language selected:", lang, "digits=", digits)
    host = request.headers.get("x-forwarded-host", request.url.hostname)
    ws_url = f"wss://{host}/media-stream?lang={lang}"
    connect = Connect()
    connect.stream(url=ws_url)
    vr = VoiceResponse()
    vr.append(connect)
    return HTMLResponse(str(vr), media_type="application/xml")

# ──────────────────────────────────────────────
# 3️⃣  Media stream WebSocket handler
# ──────────────────────────────────────────────
@app.websocket("/media-stream")
async def media_stream(ws: WebSocket):
    await ws.accept()
    lang = ws.query_params.get("lang", "en")

    try:
        async with websockets.connect(
            "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01",
            extra_headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "OpenAI-Beta": "realtime=v1",
            },
        ) as ai_ws:
            # Initialize session with language-specific instructions
            await initialize_session(ai_ws, lang)

            greeted = False
            stream_sid = None
            rate_state = None

            async def twilio_to_openai():
                nonlocal stream_sid, greeted
                try:
                    async for raw in ws.iter_text():
                        data = json.loads(raw)
                        evt = data.get("event")
                        if evt == "start":
                            stream_sid = data["start"]["streamSid"]
                            print("🔊 Twilio stream started", stream_sid)
                        elif evt == "media" and ai_ws.open:
                            if not greeted:
                                await send_initial_greeting(ai_ws, lang)
                                greeted = True
                            await ai_ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": data["media"]["payload"],
                            }))
                except WebSocketDisconnect:
                    if ai_ws.open:
                        await ai_ws.close()

            async def openai_to_twilio():
                nonlocal rate_state
                try:
                    async for ai_raw in ai_ws:
                        ai = json.loads(ai_raw)
                        if ai.get("type") == "response.audio.delta" and "delta" in ai:
                            pcm24 = base64.b64decode(ai["delta"])
                            pcm8, rate_state = audioop.ratecv(pcm24, 2, 1, OPENAI_PCM_SAMPLE_RATE, TWILIO_MULAW_SAMPLE_RATE, rate_state)
                            mulaw = audioop.lin2ulaw(pcm8, 2)
                            await ws.send_json({
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": base64.b64encode(mulaw).decode()},
                            })
                except websockets.exceptions.ConnectionClosed:
                    pass

            await asyncio.gather(twilio_to_openai(), openai_to_twilio())

    except Exception as e:
        print("CRITICAL:", e)

# ──────────────────────────────────────────────
# 4️⃣  OpenAI helpers
# ──────────────────────────────────────────────
async def initialize_session(ai_ws, lang):
    prompt_en = (
        "You are a calm, neutral AI for an anonymous employee tip line. "
        "Ask one question at a time and wait for the caller to finish before speaking again. "
        "Do not speak unless the user has clearly responded. "
        "Gather who, what, when, where, and any evidence. "
        "Respond only in English."
    )
    prompt_es = (
        "Eres una IA calmada y neutral para una línea anónima de denuncias de empleados. "
        "Haz una pregunta a la vez y espera a que el interlocutor termine antes de hablar de nuevo. "
        "No hables a menos que el usuario haya respondido claramente. "
        "Reúne quién, qué, cuándo, dónde y cualquier evidencia. "
        "Responde únicamente en español."
    )
    prompt = prompt_es if lang == "es" else prompt_en

    await ai_ws.send(json.dumps({
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad"},
            "instructions": prompt,
        },
    }))

async def send_initial_greeting(ai_ws, lang):
    greetings = {
        "en": "Thank you for calling the anonymous tip line. How can I assist you today?",
        "es": "Gracias por llamar a la línea de denuncias anónimas. ¿Cómo puedo ayudarle hoy?",
    }
    greeting_text = greetings.get(lang, greetings["en"])

    await ai_ws.send(json.dumps({
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": greeting_text}],
        },
    }))
    await ai_ws.send(json.dumps({"type": "response.create"}))

# ──────────────────────────────────────────────
# 5️⃣  Entrypoint
# ──────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
