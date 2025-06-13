import os
import json
import base64
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
PORT = int(os.getenv("PORT", 5050))

if not OPENAI_API_KEY:
    raise RuntimeError("Missing the OpenAI API key. Please check your .env file.")

app = FastAPI()

# --- Main Application ---

@app.get("/", response_class=JSONResponse)
async def health_check():
    """A simple endpoint to check if the server is running."""
    return {"message": "Twilio Media Stream Server is running!"}


# ✨ STEP 1: GREETING & LANGUAGE MENU ✨
# This is the first thing Twilio hits. It now plays a menu.
@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(_: Request):
    """Handles the incoming call and presents a language selection menu."""
    vr = VoiceResponse()
    
    # Use <Gather> to collect the caller's keypad input
    gather = vr.gather(
        action="/language-selection",  # Send the choice to this new endpoint
        method="POST",
        num_digits=1,
        timeout=5  # Wait 5 seconds for input
    )
    
    # Say the greeting using high-quality, non-robotic voices
    gather.say(
        "You've reached the employee tip line. For English, stay on the line.",
        voice="Polly.Matthew"  # A realistic male voice for English
    )
    gather.say(
        "Para español, presione uno.",
        voice="Polly.Lupe"  # A realistic female voice for Spanish
    )
    
    # If the caller doesn't press anything, redirect to the selection endpoint.
    # It will default to English.
    vr.redirect("/language-selection")
    
    return HTMLResponse(str(vr), media_type="application/xml")


# ✨ STEP 2: HANDLE THE LANGUAGE CHOICE ✨
# This new endpoint connects the call to the AI after a language is chosen.
@app.api_route("/language-selection", methods=["GET", "POST"])
async def language_selection(request: Request):
    """Connects the call to the media stream with the chosen language."""
    body = (await request.body()).decode()
    
    # Figure out which language was chosen
    digits = parse_qs(body).get("Digits", [""])[0]
    lang = "es" if digits.strip() == "1" else "en"
    
    # Get the public server host (works with ngrok)
    host = request.headers.get("x-forwarded-host", request.url.hostname)
    
    # Create the WebSocket URL and pass the language as a parameter
    ws_url = f"wss://{host}/media-stream?lang={lang}"
    print(f"Connecting to WebSocket with language: {lang} at {ws_url}")
    
    # Create the TwiML to connect the call to our WebSocket
    connect = Connect()
    connect.stream(url=ws_url)
    vr = VoiceResponse()
    vr.append(connect)
    
    return HTMLResponse(str(vr), media_type="application/xml")


# ✨ STEP 3: RUN THE AI CONVERSATION ✨
# This handles the real-time conversation with OpenAI.
@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """Handles the WebSocket connection for the real-time audio stream."""
    await websocket.accept()
    
    # Get the language that was passed from the previous step
    lang = websocket.query_params.get("lang", "en")
    
    async with websockets.connect(
        "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01",
        extra_headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "OpenAI-Beta": "realtime=v1",
        },
    ) as openai_ws:
        
        # 1. Configure the OpenAI session with the right instructions and language
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
                if openai_ws.open:
                    await openai_ws.close()

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

        # Run both functions at the same time
        await asyncio.gather(receive_from_twilio(), send_to_twilio())


# --- Helper Functions for OpenAI ---

async def initialize_session(openai_ws, lang: str):
    """Sends the initial configuration and system prompt to OpenAI."""
    # This is the AI's main instruction or "personality".
    system_prompt = (
        "You are a helpful and professional AI assistant for an employee tip line. "
        "You are to be respectful, confidential, and clear in your communication. "
        "Your goal is to understand the caller's report and gather necessary details. "
        f"You must conduct the entire conversation in {'Spanish' if lang == 'es' else 'English'}."
    )
    
    await openai_ws.send(
        json.dumps({
            "type": "session.create",
            "instructions": system_prompt,
        })
    )

async def send_initial_greeting(openai_ws, lang: str):
    """Sends a synthetic message to make the AI speak its first greeting."""
    # We create language-specific prompts to ask the AI to start the conversation.
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
    # This triggers the AI to respond to the prompt we just sent.
    await openai_ws.send(json.dumps({"type": "response.create"}))


# This part is only needed if you run the file directly
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
