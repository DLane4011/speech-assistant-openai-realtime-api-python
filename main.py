import os
import json
import asyncio
import websockets
from urllib.parse import parse_qs

from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect
from dotenv import load_dotenv

load_dotenv()

# --- Configuration ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PORT = int(os.getenv("PORT", 8000))

if not OPENAI_API_KEY:
    raise RuntimeError("FATAL: OPENAI_API_KEY not found in environment variables.")

app = FastAPI()

# --- Main Application ---

@app.get("/", response_class=JSONResponse)
async def health_check():
    return {"message": "Twilio Media Stream Server is running!"}


@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(_: Request):
    vr = VoiceResponse()
    gather = vr.gather(action="/language-selection", method="POST", num_digits=1, timeout=5)
    gather.say("You've reached the employee tip line. For English, stay on the line.", voice="Polly.Matthew")
    gather.say("Para español, presione uno.", voice="Polly.Lupe")
    vr.redirect("/language-selection")
    return HTMLResponse(str(vr), media_type="application/xml")


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
            await initialize_session(openai_ws, lang)
            await send_initial_greeting(openai_ws, lang)

            stream_sid = None
            
            async def receive_from_twilio():
                nonlocal stream_sid
                try:
                    async for message in websocket.iter_text():
                        data = json.loads(message)
                        event = data.get("event")
                        if event == "start":
                            stream_sid = data["start"]["streamSid"]
                        elif event == "media" and openai_ws.open:
                            await openai_ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": data["media"]["payload"]}))
                except WebSocketDisconnect:
                    if openai_ws.open: await openai_ws.close()

            async def send_to_twilio():
                nonlocal stream_sid
                try:
                    async for oa_raw in openai_ws:
                        oa = json.loads(oa_raw)
                        if oa.get("type") == "response.audio.delta" and "delta" in oa:
                            await websocket.send_json({"event": "media", "streamSid": stream_sid, "media": {"payload": oa["delta"]}})
                except websockets.exceptions.ConnectionClosed:
                    pass
                except Exception as e:
                    print(f"ERROR: Exception in send_to_twilio loop: {e}")

            await asyncio.gather(receive_from_twilio(), send_to_twilio())
    
    except Exception as e:
        print(f"CRITICAL: Failed to connect or run main WebSocket loop: {e}")


# --- Helper Functions for OpenAI ---

# ✨ FINAL FIX IS HERE: Changed 'session.create' to 'session.update' ✨
async def initialize_session(openai_ws, lang: str):
    """Sends the initial configuration and system prompt to OpenAI."""
    system_prompt = (
        "You are a helpful and professional AI assistant for an employee tip line. "
        "You are to be respectful, confidential, and clear in your communication. "
        "Your goal is to understand the caller's report and gather necessary details. "
        f"You must conduct the entire conversation in {'Spanish' if lang == 'es' else 'English'}."
    )
    
    # Per the error log, we must use 'session.update' not 'session.create'.
    # The parameters are nested under a 'session' object.
    session_config = {
        "type": "session.update",
        "session": {
            "output_format": {
                "encoding": "mulaw",  # This is the format Twilio needs (μ-law)
                "sample_rate": 8000
            },
            "instructions": system_prompt
        }
    }
    
    print("INFO: Updating OpenAI session with config:", json.dumps(session_config))
    await openai_ws.send(json.dumps(session_config))


async def send_initial_greeting(openai_ws, lang: str):
    """Sends a synthetic message to make the AI speak its first greeting."""
    prompts = {"en": "Please greet the caller in English and ask how you can assist.", "es": "Por favor, saluda al llamante en español y pregunta en qué puedes ayudar."}
    initial_item = {
        "type": "conversation.item.create",
        "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": prompts.get(lang, prompts["en"])}]},
    }
    await openai_ws.send(json.dumps(initial_item))
    await openai_ws.send(json.dumps({"type": "response.create"}))


if __name__ == "__main__":
    import uvicorn
    local_port = int(os.getenv("PORT", 5050))
    uvicorn.run(app, host="0.0.0.0", port=local_port)
