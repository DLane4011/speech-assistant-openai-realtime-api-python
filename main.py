import os
import json
import base64
import asyncio
import audioop
from urllib.parse import parse_qs

import websockets
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from twilio.twiml.voice_response import VoiceResponse, Connect
from dotenv import load_dotenv

# ──────────────────────────────────────────────
# ENV & CONSTANTS
# ──────────────────────────────────────────────
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PORT = int(os.getenv("PORT", 8000))  # Railway default 8000 → forwarded to 8080 automatically
PUBLIC_URL = os.getenv("PUBLIC_URL")  # e.g. my‑service.up.railway.app (NO protocol!)
MODEL = "gpt-4o-realtime-preview-2024-10-01"
OPENAI_PCM_RATE = 24000  # 24‑kHz PCM from OpenAI
TWILIO_MULAW_RATE = 8000  # 8‑kHz μ‑law used by Twilio

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is missing—add it to Railway Variables.")

# ──────────────────────────────────────────────
# FastAPI setup
# ──────────────────────────────────────────────
app = FastAPI()

@app.get("/", response_class=JSONResponse)
async def health():
    return {"status": "ok"}

# ──────────────────────────────────────────────
# 1️⃣  Inbound phone call – language menu
# ──────────────────────────────────────────────
@app.api_route("/incoming-call", methods=["GET", "POST"])
async def inbound(_: Request):
    vr = VoiceResponse()
    gather = vr.gather(action="/language-selection", method="POST", num_digits=1, timeout=5)
    gather.say("You’ve reached the employee tip line. For English, stay on the line.", voice="Polly.Matthew")
    gather.pause(length=1)
    gather.say("Para español, presione uno.", voice="Polly.Lupe")
    # fall‑through → /language-selection
    vr.redirect("/language-selection")
    return HTMLResponse(str(vr), media_type="application/xml")

# ──────────────────────────────────────────────
# 2️⃣  Handle DTMF, return <Stream>
# ──────────────────────────────────────────────
@app.api_route("/language-selection", methods=["GET", "POST"])
async def choose_language(request: Request):
    body = (await request.body()).decode()
    digits = parse_qs(body).get("Digits", [""])[0]
    lang = "es" if digits.strip() == "1" else "en"
    print("Language selected:", lang, "digits=", digits)

    domain = PUBLIC_URL or request.headers.get("host") or request.url.hostname
    if not PUBLIC_URL:
        print("⚠️  PUBLIC_URL not set. Using domain from request:", domain)
    ws_url = f"wss://{domain}/media-stream?lang={lang}"
    print("WebSocket URL →", ws_url)

    vr = VoiceResponse()
    connect = Connect()
    connect.stream(url=ws_url)
    vr.append(connect)
    return HTMLResponse(str(vr), media_type="application/xml")

# ──────────────────────────────────────────────
# 3️⃣  Media‑stream bridge
# ──────────────────────────────────────────────
@app.websocket("/media-stream")
async def media(ws: WebSocket):
    await ws.accept()
    lang = ws.query_params.get("lang", "en")
    print("WS CONNECT, lang=", lang)

    try:
        async with websockets.connect(
            f"wss://api.openai.com/v1/realtime?model={MODEL}",
            extra_headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "OpenAI-Beta": "realtime=v1",
            },
        ) as ai:
            await setup_session(ai, lang)
            greeted = False
            stream_sid = None
            rate_state = None

            async def twilio_to_openai():
                nonlocal greeted, stream_sid
                try:
                    async for raw in ws.iter_text():
                        data = json.loads(raw)
                        evt = data.get("event")
                        if evt == "start":
                            stream_sid = data["start"]["streamSid"]
                            print("Twilio stream started", stream_sid)
                        elif evt == "media":
                            if not greeted:
                                await send_greeting(ai, lang)
                                greeted = True
                            await ai.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": data["media"]["payload"],
                            }))
                        elif evt == "stop":
                            break
                except WebSocketDisconnect:
                    pass

            async def openai_to_twilio():
                try:
                    async for raw in ai:
                        msg = json.loads(raw)
                        if msg.get("type") == "response.audio.delta" and "delta" in msg:
                            await ws.send_json({
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": msg["delta"]},
                            })
                except websockets.exceptions.ConnectionClosed:
                    pass

            await asyncio.gather(twilio_to_openai(), openai_to_twilio())
    except Exception as e:
        print("CRITICAL: ", e)
    finally:
        await ws.close()
        print("WS CLOSED")

# ──────────────────────────────────────────────
# 4️⃣  OpenAI helper functions
# ──────────────────────────────────────────────
async def setup_session(ai_ws, lang: str):
    prompt = (
        "You are a calm, neutral AI for an anonymous employee tip line. "
        "Ask one question at a time and wait for the caller to finish before speaking again. "
        "Gather who, what, when, where, and any evidence. "
        f"Respond only in {'Spanish' if lang == 'es' else 'English'}."
    )
    await ai_ws.send(json.dumps({
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad"},
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": "alloy",
            "instructions": prompt,
        },
    }))

async def send_greeting(ai_ws, lang: str):
    greetings = {
        "en": "Thank you for calling the anonymous tip line. How can I help you today?",
        "es": "Gracias por llamar a la línea de denuncias anónimas. ¿Cómo puedo ayudarle hoy?",
    }
    await ai_ws.send(json.dumps({
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": greetings[lang]}],
        },
    }))
    await ai_ws.send(json.dumps({"type": "response.create"}))

# ──────────────────────────────────────────────
# Entrypoint (Railway)
# ──────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
