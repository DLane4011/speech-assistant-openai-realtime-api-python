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

# This line is for local development. On Railway, it does nothing, which is fine.
load_dotenv()

# --- Configuration ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PORT = int(os.getenv("PORT", 8000)) # Railway typically uses port 8000

if not OPENAI_API_KEY:
    raise RuntimeError("FATAL: OPENAI_API_KEY not found in environment variables.")

app = FastAPI()

# --- Main Application ---

@app.get("/", response_class=JSONResponse)
async def health_check():
    """A simple endpoint to check if the server is running."""
    return {"message": "Twilio Media Stream Server is running!"}


# STEP 1: GREETING & LANGUAGE MENU
@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(_: Request):
    """Handles the incoming call and presents a language selection menu."""
    vr = VoiceResponse()
    gather = vr.gather(
        action="/language-selection",
        method="POST",
        num_digits=1,
        timeout=5
    )
    gather.say(
        "You've reached the employee tip line. For English, stay on the line.",
        voice="Polly.Matthew"
    )
    gather.say(
        "Para español, presione uno.",
        voice="Polly.Lupe"
    )
    vr.redirect("/language-selection")
    return HTMLResponse(str(vr), media_type="application/xml")


# STEP 2: HANDLE THE LANGUAGE CHOICE
@app.api_route("/language-selection", methods=["GET", "POST"])
async def language_selection(request: Request):
    """Connects the call to the media stream with the chosen language."""
    body = (await request.body()).decode()
    digits = parse_qs(body).get("Digits", [""])[0]
    lang = "es" if digits.strip() == "1" else "en"
    
    # Get the public server host from proxy headers (essential for Railway/ngrok)
    host = request.headers.get("x-forwarded-host", request.url.hostname)
    
    ws_url = f"wss://{host}/media-stream?lang={lang}"
    print(f"Connecting to WebSocket with language: {lang} at {ws_url}")
    
    connect = Connect()
    connect.stream(url=ws_url)
    vr = VoiceResponse()
    vr.append(connect)
    return HTMLResponse(str(vr), media_type="application/xml")


# STEP 3: RUN THE AI CONVERSATION
@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """Handles the WebSocket connection for the real-time audio stream."""
    await websocket.accept()
    lang = websocket.query_params.get("lang", "en")
    
    async with websockets.connect(
        "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01",
        extra_headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "OpenAI-Beta": "realtime=v1",
        },
    ) as openai_ws:
        
        # 1. Configure the OpenAI session
        await initialize_session(openai_ws, lang)
        
        # 2. Tell the AI to speak its first line
        await send_initial_greeting(openai_ws, lang)

        stream_sid = None
        
        async def receive_from_twilio():
            """Receive audio from Twilio and forward it to OpenAI."""
            nonlocal stream_sid
            try:
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    event = data.get("event")

                    if event == "start":
                        stream_sid = data["start"]["streamSid"]
                    elif event == "media" and openai_ws.open:
                        await openai_ws.send(
                            json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": data["media"]["payload"],
                            })
                        )
            except WebSocketDisconnect:
                print(f"Twilio WebSocket disconnected for stream {stream_sid}.")
                if openai_ws.open: await openai_ws.close()

        async def send_to_twilio():
            """Receive audio from OpenAI and forward it to Twilio."""
            nonlocal stream_sid
            try:
                async for oa_raw in openai_ws:
                    oa = json.loads(oa_raw)
                    if oa.get("type") == "response.audio.delta" and "delta" in oa:
                        # The 'delta' is already the base64 string Twilio needs
                        payload = oa["delta"]
                        await websocket.send_json({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": payload},
                        })
            except websockets.exceptions.ConnectionClosed as e:
                print(f"OpenAI WebSocket connection closed: {e}")
            except Exception as e:
                print(f"Error relaying to Twilio: {e}")

        await asyncio.gather(receive_from_twilio(), send_to_twilio())


# --- Helper Functions for OpenAI ---

# ✨ FIX IS HERE: Re-instated detailed session parameters from your original code. ✨
async def initialize_session(openai_ws, lang: str):
    """Sends the initial configuration and system prompt to OpenAI."""
    system_prompt = (
        "You are a helpful and professional AI assistant for an employee tip line. "
        "You are to be respectful, confidential, and clear in your communication. "
        "Your goal is to understand the caller's report and gather necessary details. "
        f"You must conduct the entire conversation in {'Spanish' if lang == 'es' else 'English'}."
    )
    
    # The OpenAI Realtime API needs to know the audio format it's receiving from Twilio
    # and the format it should send back. This is a critical step.
    session_config = {
        "type": "session.create",
        "audio_format": {
            "input_format": {
                "encoding": "mulaw",  # This is the format Twilio sends (μ-law)
                "sample_rate": 8000
            },
            "output_format": {
                "encoding": "mulaw", # Send back the same format
                "sample_rate": 8000
            }
        },
        "instructions": system_prompt
    }
    
    print("Initializing OpenAI session with config:", json.dumps(session_config))
    await openai_ws.send(json.dumps(session_config))


async def send_initial_greeting(openai_ws, lang: str):
    """Sends a synthetic message to make the AI speak its first greeting."""
    prompts = {
        "en": "Please greet the caller in English and ask how you can assist.",
        "es": "Por favor, saluda al llamante en español y pregunta en qué puedes ayudar.",
    }
    
    await openai_ws.send(
        json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": prompts.get(lang, prompts["en"])
                }],
            },
        })
    )
    await openai_ws.send(json.dumps({"type": "response.create"}))


# This part allows the app to run locally for testing
if __name__ == "__main__":
    import uvicorn
    # When running locally, it will use port 5050 if you have it in your .env
    # otherwise it defaults to 8000.
    local_port = int(os.getenv("PORT", 5050))
    print(f"Starting server locally on port {local_port}")
    uvicorn.run(app, host="0.0.0.0", port=local_port)
Use code with caution.
Python
Summary of the Fix
Restored initialize_session Details: The main change is in the initialize_session function. I've put back the detailed configuration that tells OpenAI exactly what kind of audio to expect from Twilio and what kind to send back.
encoding: "mulaw": This is the specific audio codec that phone systems and Twilio use. It's also known as G.711 μ-law.
sample_rate: 8000: This is the standard sample rate for telephone audio.
Why this works: Without this information, the OpenAI API might be sending back audio in a different format (like high-fidelity PCM) that Twilio's media stream can't understand, resulting in the static you hear. By explicitly defining the format, we ensure both systems are speaking the same audio language.
Robust Port for Railway: I changed the default port in the PORT = int(os.getenv("PORT", 8000)) line to 8000. Railway and many other hosting platforms automatically set a PORT environment variable, and it's often 8000. This makes the code slightly more robust for deployment. For local testing, I also updated the if __name__ == "__main__" block to use a different default port (5050) to avoid confusion.
Next Steps
Replace Your Code: Copy the entire new code block and overwrite your existing file on Railway.
Redeploy: Let Railway automatically redeploy your application with the new code.
Check Logs: Keep an eye on your Railway logs. You should see the Initializing OpenAI session with config... message.
Test the Call: Call your Twilio number again.
This time, after you select the language, you should hear the clear voice of the OpenAI assistant instead of static.

# This part is only needed if you run the file directly
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
