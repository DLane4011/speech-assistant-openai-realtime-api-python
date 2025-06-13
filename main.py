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

# ─────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PORT = int(os.getenv("PORT", 5050))
# The VOICE variable is not used by the OpenAI Realtime API, which has its own voice selection.
# It is kept here for reference in case you integrate other TTS services.
VOICE = "alloy"
LOG_EVENT_TYPES = [
    "error",
    "response.content.done",
    "rate_limits.updated",
    "response.done",
    "input_audio_buffer.committed",
    "input_audio_buffer.speech_stopped",
    "input_audio_buffer.speech_started",
    "session.created",
]
SHOW_TIMING_MATH = False  # set True if you want timing math printed

if not OPENAI_API_KEY:
    raise RuntimeError("Missing the OpenAI API key. Set OPENAI_API_KEY in your env.")

app = FastAPI()

# ─────────────────────────────────────────
# 0️⃣  Health‑check route
# ─────────────────────────────────────────
@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Twilio Media Stream Server is running!"}

# ─────────────────────────────────────────
# 1️⃣  Greeting + language menu (improved Polly voices)
# ─────────────────────────────────────────
@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(_: Request):
    vr = VoiceResponse()
    gather = vr.gather(
        action="/language-selection",
        method="POST",
        num_digits=1,
        timeout=4
    )
    gather.say(
        "Thank you for contacting the Tip Line. For English, stay on the line.",
        voice="Polly.Matthew"
    )
    gather.pause(length=1)
    gather.say(
        "Para español, presione el uno.",
        voice="Polly.Lupe"
    )
    # If the user doesn't press anything, redirect to continue the flow (defaulting to English)
    vr.redirect("/language-selection")
    return HTMLResponse(str(vr), media_type="application/xml")

@app.api_route("/language-selection", methods=["GET", "POST"])
async def language_selection(request: Request):
    body = (await request.body()).decode()
    digits = parse_qs(body).get("Digits", [""])[0]
    lang = "es" if digits.strip() == "1" else "en"
    
    # Construct the WebSocket URL dynamically
    # Note: For production, you'd want to use a more robust method for determining the host,
    # especially if behind a reverse proxy. `request.url.hostname` is a good start.
    host = request.url.hostname
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
    
    # Connect to OpenAI's Realtime API
    async with websockets.connect(
        "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01",
        extra_headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "OpenAI-Beta": "realtime=v1",
        },
    ) as openai_ws:
        await initialize_session(openai_ws, lang)
        await send_initial_conversation_item(openai_ws, lang)

        # State variables for managing the stream and interruption
        stream_sid = None
        latest_media_timestamp = 0
        last_assistant_item = None
        mark_queue = []
        response_start_timestamp_twilio = None

        async def receive_from_twilio():
            # ✨ FIX 1: Added `nonlocal` declarations for variables modified in this scope.
            nonlocal stream_sid, latest_media_timestamp, last_assistant_item, response_start_timestamp_twilio
            try:
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    event = data.get("event")

                    if event == "start":
                        stream_sid = data["start"]["streamSid"]
                        # Reset state for the new stream
                        latest_media_timestamp = 0
                        last_assistant_item = None
                        response_start_timestamp_twilio = None

                    elif event == "media" and openai_ws.open:
                        latest_media_timestamp = int(data["media"]["timestamp"])
                        await openai_ws.send(
                            json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": data["media"]["payload"],
                            })
                        )

                    elif event == "mark" and mark_queue:
                        # This confirms Twilio has finished playing an audio chunk we sent
                        mark_queue.pop(0)

            except WebSocketDisconnect:
                print(f"Twilio WebSocket disconnected for stream {stream_sid}.")
                if openai_ws.open:
                    await openai_ws.close()

        async def send_to_twilio():
            nonlocal stream_sid, last_assistant_item, response_start_timestamp_twilio
            try:
                async for oa_raw in openai_ws:
                    oa = json.loads(oa_raw)
                    oa_type = oa.get("type")

                    if oa_type == "response.audio.delta" and "delta" in oa:
                        # ✨ FIX 2: Removed redundant base64 decoding and re-encoding.
                        # The 'delta' field from OpenAI is already the base64 string Twilio needs.
                        payload = oa["delta"] 
                        
                        await websocket.send_json(
                            {
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": payload},
                            }
                        )
                        if response_start_timestamp_twilio is None:
                            # Capture the timestamp of the first audio chunk for this response
                            response_start_timestamp_twilio = latest_media_timestamp
                        
                        # Store the ID of the assistant's message for potential interruption
                        if oa.get("item_id"):
                            last_assistant_item = oa["item_id"]
                        
                        # Send a mark to track when this audio chunk is played
                        await send_mark(websocket, stream_sid)

                    elif oa_type == "input_audio_buffer.speech_started" and last_assistant_item:
                        # The user has started speaking while the assistant was talking.
                        await handle_interrupt()

            except websockets.exceptions.ConnectionClosed as e:
                print(f"OpenAI WebSocket connection closed: {e}")
            except Exception as e:
                print(f"Error relaying to Twilio: {e}")

        async def handle_interrupt():
            nonlocal response_start_timestamp_twilio, last_assistant_item
            # Check if there's an active response to interrupt
            if mark_queue and response_start_timestamp_twilio is not None:
                elapsed_ms = latest_media_timestamp - response_start_timestamp_twilio
                print(f"Interrupting assistant after {elapsed_ms}ms.")

                # Tell OpenAI to stop generating audio for this item
                await openai_ws.send(
                    json.dumps(
                        {
                            "type": "conversation.item.truncate",
                            "item_id": last_assistant_item,
                            "content_index": 0,
                            "audio_end_ms": elapsed_ms,
                        }
                    )
                )
                
                # Tell Twilio to clear its outbound audio buffer immediately
                await websocket.send_json({"event": "clear", "streamSid": stream_sid})
                
                # Reset state after interruption
                mark_queue.clear()
                last_assistant_item = None
                response_start_timestamp_twilio = None

        async def send_mark(conn, sid):
            if sid:
                mark_name = "responsePart"
                await conn.send_json(
                    {
                        "event": "mark",
                        "streamSid": sid,
                        "mark": {"name": mark_name},
                    }
                )
                mark_queue.append(mark_name)

        # Run the two coroutines concurrently to handle bidirectional streaming
        await asyncio.gather(receive_from_twilio(), send_to_twilio())

async def send_initial_conversation_item(openai_ws, lang: str):
    """Sends a synthetic user message to OpenAI to make the AI speak first."""
    prompts = {
        "en": "Please greet the caller in English and ask how you can help.",
        "es": "Por favor, saluda al llamante en español y pregunta en qué puedes ayudar.",
    }
    await openai_ws.send(
        json.dumps(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompts.get(lang, prompts["en"])},
                    ],
                },
            }
        )
    )
    # Trigger the response generation for the item we just created
    await openai_ws.send(json.dumps({"type": "response.create"}))

async def initialize_session(openai_ws, lang: str):
    """Sends the initial configuration and system prompt to the OpenAI session."""
    instructions = (
        "You are an AI answering calls on a tip line. Greet the caller and ask for details. "
        "Be helpful, respectful, and never assume information that has not been said. "
        "Your responses should be concise and natural-sounding. "
        f"You must conduct the entire conversation in {'Spanish' if lang == 'es' else 'English'}."
    )
    await openai_ws.send(
        json.dumps(
            {
                "type": "session.create",
                "instructions": instructions,
                # You can configure the output audio format and voice here if the API supports it
                # "output_format": {"encoding": "mulaw", "sample_rate": 8000},
                # "voice": "aura-asteria-en",
            }
        )
    )
