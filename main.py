import os
import json
import asyncio
from urllib.parse import parse_qs
import traceback
import base64
import audioop

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

# Audio settings
# Twilio uses 8000Hz an µ-law encoding. We will convert this to raw PCM for OpenAI.
TWILIO_SAMPLE_RATE = 8000
AUDIO_FORMAT = "pcm_s16le" # Signed 16-bit PCM, little-endian
CHANNELS = 1
BITS_PER_SAMPLE = 16

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
                f"wss://api.openai.com/v1/realtime?model={MODEL}&encoding={AUDIO_FORMAT}&sample_rate={TWILIO_SAMPLE_RATE}",
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
                                # Twilio sends µ-law audio base64 encoded.
                                mulaw_data = base64.b64decode(data["media"]["payload"])
                                # Convert µ-law to raw PCM (linear 16-bit).
                                pcm_data = audioop.ulaw2lin(mulaw_data, 2)
                                # Send raw PCM data to OpenAI
                                await ai_websocket.send_bytes(pcm_data)
                            elif event == "stop":
                                if has_received_media:
                                    # This message is no longer needed with raw audio streaming
                                    has_received_media = False
                    except WebSocketDisconnect:
                        print("Twilio WebSocket disconnected.")
                    finally:
                        # Signal to OpenAI that the user audio stream is finished
                        await ai_websocket.send_str(json.dumps({"type": "input_audio.stream.end"}))


                async def openai_to_twilio():
                    try:
                        async for msg in ai_websocket:
                            if msg.type == aiohttp.WSMsgType.BINARY:
                                # OpenAI sends back raw PCM audio.
                                pcm_data = msg.data
                                # Convert PCM to µ-law for Twilio.
                                mulaw_data = audioop.lin2ulaw(pcm_data, 2)
                                # Base64 encode the µ-law data.
                                base64_mulaw = base64.b64encode(mulaw_data).decode('utf-8')
                                # Send back to Twilio.
                                await websocket.send_json({"event": "media", "streamSid": stream_sid, "media": {"payload": base64_mulaw}})
                            elif msg.type == aiohttp.WSMsgType.TEXT:
                                # This handles text messages like transcripts or errors
                                print(f"Received text from OpenAI: {msg.data}")
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
    # This API now uses a 'session.init' message
    await ai_ws.send_str(json.dumps({
        "type": "session.init",
        "session": {"turn_detection": {"type": "server_vad"}, "voice": "alloy", "instructions": prompt, "output_format":{"encoding": "pcm_s16le", "sample_rate": 8000}}
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
