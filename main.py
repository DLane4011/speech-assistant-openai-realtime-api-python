import os
import json
import asyncio
from urllib.parse import parse_qs
import traceback

import aiohttp

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from twilio.twiml.voice_response import VoiceResponse, Connect
from dotenv import load_dotenv

# --- SCRIPT START ---
print("--- PYTHON SCRIPT STARTING ---", flush=True)

load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PUBLIC_URL = os.getenv("PUBLIC_URL")
MODEL = "gpt-4o-realtime-preview-2024-10-01"

if not OPENAI_API_KEY:
    print("!!! FATAL: OPENAI_API_KEY IS MISSING FROM ENVIRONMENT VARIABLES !!!", flush=True)
    raise RuntimeError("OPENAI_API_KEY is missing—add it to Railway Variables.")
else:
    print("--- OPENAI_API_KEY loaded successfully.", flush=True)


app = FastAPI()

@app.get("/", response_class=JSONResponse)
async def health():
    return {"status": "ok"}

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def inbound(_: Request):
    print("--- INCOMING CALL RECEIVED ---", flush=True)
    vr = VoiceResponse()
    gather = vr.gather(action="/language-selection", method="POST", num_digits=1, timeout=5)
    gather.say("You have reached the employee tip line. For English, please stay on the line.", voice="Polly.Matthew-Neural")
    gather.pause(length=1)
    gather.say("Para español, presione uno.", voice="Polly.Lupe-Neural")
    vr.redirect("/language-selection")
    return HTMLResponse(str(vr), media_type="application/xml")

@app.api_route("/language-selection", methods=["GET", "POST"])
async def choose_language(request: Request):
    print("--- CHOOSING LANGUAGE ---", flush=True)
    body = (await request.body()).decode()
    digits = parse_qs(body).get("Digits", [""])[0]
    lang = "es" if digits.strip() == "1" else "en"
    print(f"Language selected: {lang} (digits received: '{digits}')", flush=True)

    domain = PUBLIC_URL or request.headers.get("host")
    
    # --- FIX #1: Change the URL to use a path parameter ---
    ws_url = f"wss://{domain}/media-stream/{lang}"
    # ------------------------------------------------------
    
    print(f"WebSocket URL -> {ws_url}", flush=True)

    vr = VoiceResponse()
    connect = Connect()
    connect.stream(url=ws_url)
    vr.append(connect)
    vr.pause(length=15)
    vr.say("We're sorry, but there was an issue connecting. Please call back later.")
    return HTMLResponse(str(vr), media_type="application/xml")

# --- FIX #2: Update the WebSocket endpoint to read the language from the path ---
@app.websocket("/media-stream/{lang}")
async def media(ws: WebSocket, lang: str):
# --------------------------------------------------------------------------
    print("--- MEDIA STREAM FUNCTION STARTED ---", flush=True)
    await ws.accept()
    
    # The 'lang' variable now comes directly from the URL path.
    print(f"WebSocket connection from Twilio accepted for language: {lang}", flush=True)

    try:
        print("Attempting to connect to OpenAI using aiohttp...", flush=True)
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                f"wss://api.openai.com/v1/realtime?model={MODEL}",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "OpenAI-Beta": "realtime=v1",
                }
            ) as ai:
                print(">>> SUCCESS: Connection to OpenAI established.", flush=True)
                
                await setup_session(ai, lang)
                await asyncio.sleep(0.25)
                await send_greeting(ai, lang)

                stream_sid = None
                has_received_media = False

                async def twilio_to_openai():
                    nonlocal stream_sid, has_received_media
                    try:
                        async for raw in ws.iter_text():
                            data = json.loads(raw)
                            evt = data.get("event")
                            if evt == "start":
                                stream_sid = data["start"]["streamSid"]
                            elif evt == "media":
                                has_received_media = True
                                await ai.send_str(json.dumps({ "type": "input_audio_buffer.append", "audio": data["media"]["payload"] }))
                            elif evt == "stop":
                                if has_received_media:
                                    print("Committing audio buffer to OpenAI...", flush=True)
                                    await ai.send_str(json.dumps({"type": "input_audio_buffer.commit"}))
                                    has_received_media = False
                    except WebSocketDisconnect:
                        print("Twilio WebSocket disconnected.", flush=True)

                async def openai_to_twilio():
                    try:
                        async for msg in ai:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = json.loads(msg.data)
                                print(f"Received from OpenAI: {data}", flush=True)
                                if data.get("type") == "response.audio.delta" and "delta" in data:
                                    await ws.send_json({ "event": "media", "streamSid": stream_sid, "media": {"payload": data["delta"]} })
                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                print(f"!!! OpenAI WebSocket error: {msg}", flush=True)
                    except Exception as e:
                        print(f"!!! OpenAI connection closed unexpectedly: {e}", flush=True)

                await asyncio.gather(twilio_to_openai(), openai_to_twilio())

    except aiohttp.ClientResponseError as e:
        print(f"!!! FAILED to connect to OpenAI. Status: {e.status}, Message: {e.message}", flush=True)
    except Exception as e:
        print(f"!!! CRITICAL UNHANDLED ERROR in WebSocket bridge: {e}", flush=True)
        traceback.print_exc()
    finally:
        print("--- MEDIA STREAM FUNCTION FINISHED ---", flush=True)


async def setup_session(ai_ws, lang: str):
    language_instruction = "You MUST respond exclusively in Spanish." if lang == 'es' else "You MUST respond exclusively in English."
    prompt = (
        "You are an AI assistant for an anonymous employee tip line. Your tone is calm, professional, and neutral. "
        "Your primary goal is to gather clear and detailed information about an incident. "
        "Ask one clear question at a time and wait for the caller to finish speaking before you reply. "
        "Your questions should guide the caller to provide information about: who was involved, what happened, "
        "when it occurred, where it took place, and if there is any evidence. "
        f"{language_instruction} Do not switch languages under any circumstances."
    )
    await ai_ws.send_str(json.dumps({
        "type": "session.update",
        "session": {"turn_detection": {"type": "server_vad"}, "input_audio_format": "g711_ulaw", "output_audio_format": "g711_ulaw", "voice": "alloy", "instructions": prompt}
    }))
    print(">>> SUCCESS: Session configured.", flush=True)

async def send_greeting(ai_ws, lang: str):
    greetings = {
        "en": "Thank you for calling the anonymous tip line. To ensure your anonymity, this call is not being recorded by us. How can I help you today?",
        "es": "Gracias por llamar a la línea de denuncias anónimas. Para garantizar su anonimato, esta llamada no está siendo grabada por nosotros. ¿Cómo puedo ayudarle hoy?",
    }
    await ai_ws.send_str(json.dumps({
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": greetings[lang]}]
        }
    }))
    await ai_ws.send_str(json.dumps({"type": "response.create"}))
    print(">>> SUCCESS: Greeting sent. Waiting for audio response...", flush=True)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)

print("--- PYTHON SCRIPT FINISHED LOADING ---", flush=True)
