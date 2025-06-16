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

load_dotenv()

# --- Configuration ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PUBLIC_URL = os.getenv("PUBLIC_URL")
MODEL = "gpt-4o-realtime-preview-2024-10-01"
# --- FIX FOR CHOPPY AUDIO: Trying a different voice model ---
VOICE_ID = "nova" 

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is missing from Railway Variables.")

app = FastAPI()

@app.get("/", response_class=JSONResponse)
async def health():
    return {"status": "ok"}

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def inbound_call(_: Request):
    response = VoiceResponse()
    gather = response.gather(action="/language-selection", method="POST", num_digits=1, timeout=5)
    gather.say("You have reached the employee tip line. For English, please stay on the line.", voice="Polly.Matthew-Neural")
    gather.pause(length=1)
    gather.say("Para español, presione uno.", voice="Polly.Lupe-Neural")
    response.redirect("/language-selection")
    return HTMLResponse(str(response), media_type="application/xml")

@app.api_route("/language-selection", methods=["GET", "POST"])
async def language_selection(request: Request):
    body = (await request.body()).decode()
    digits = parse_qs(body).get("Digits", [""])[0]
    lang = "es" if digits.strip() == "1" else "en"

    domain = PUBLIC_URL or request.headers.get("host")
    ws_url = f"wss://{domain}/media-stream/{lang}"

    response = VoiceResponse()
    connect = Connect()
    connect.stream(url=ws_url)
    response.append(connect)
    response.pause(length=15)
    response.say("We're sorry, but there was an issue connecting. Please call back later.")
    return HTMLResponse(str(response), media_type="application/xml")

@app.websocket("/media-stream/{lang}")
async def media_stream(websocket: WebSocket, lang: str):
    await websocket.accept()
    print(f"WebSocket connection accepted for language: {lang}")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                f"wss://api.openai.com/v1/realtime?model={MODEL}",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "OpenAI-Beta": "realtime=v1",
                }
            ) as ai_websocket:
                print("Successfully connected to OpenAI.")
                
                await setup_session(ai_websocket, lang)
                await asyncio.sleep(0.25)
                await send_greeting(ai_websocket, lang)

                stream_sid = None
                # --- FIX FOR DROPPED CALLS ---
                has_received_media = False

                async def twilio_to_openai():
                    nonlocal stream_sid, has_received_media
                    try:
                        async for raw in websocket.iter_text():
                            data = json.loads(raw)
                            event = data.get("event")
                            if event == "start":
                                stream_sid = data["start"]["streamSid"]
                            elif event == "media":
                                has_received_media = True
                                await ai_websocket.send_str(json.dumps({"type": "input_audio_buffer.append", "audio": data["media"]["payload"]}))
                            elif event == "stop":
                                if has_received_media:
                                    await asyncio.sleep(0.15) # Pause for network
                                    await ai_websocket.send_str(json.dumps({"type": "input_audio_buffer.commit"}))
                                    has_received_media = False
                    except WebSocketDisconnect:
                        print("Twilio WebSocket disconnected.")

                async def openai_to_twilio():
                    try:
                        async for msg in ai_websocket:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = json.loads(msg.data)
                                if data.get("type") == "response.audio.delta" and "delta" in data:
                                    await websocket.send_json({"event": "media", "streamSid": stream_sid, "media": {"payload": data["delta"]}})
                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                print(f"OpenAI WebSocket error: {msg}")
                    except Exception:
                        print("OpenAI connection closed.")

                await asyncio.gather(twilio_to_openai(), openai_to_twilio())

    except aiohttp.ClientResponseError as e:
        print(f"Failed to connect to OpenAI: {e.status} {e.message}")
    except Exception as e:
        print(f"An unhandled error occurred: {e}")
        traceback.print_exc()
    finally:
        print("Media stream finished.")


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
        "session": {"turn_detection": {"type": "server_vad"}, "input_audio_format": "g711_ulaw", "output_audio_format": "g711_ulaw", "voice": VOICE_ID, "instructions": prompt}
    }))

async def send_greeting(ai_ws, lang: str):
    greetings = {
        "en": "Thank you for calling the anonymous tip line. How can I help you today?",
        "es": "Gracias por llamar a la línea de denuncias anónimas. ¿Cómo puedo ayudarle hoy?",
    }
    await ai_ws.send_str(json.dumps({
        "type": "conversation.item.create",
        "item": {"type": "message", "role": "assistant", "content": [{"type": "text", "text": greetings[lang]}]}
    }))
    await ai_ws.send_str(json.dumps({"type": "response.create"}))

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
