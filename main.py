import os
import json
import base64
import asyncio
import websockets
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect
from dotenv import load_dotenv

load_dotenv()

# Configuration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
PORT = int(os.getenv('PORT', 5050))
VOICE = 'alloy'
LOG_EVENT_TYPES = [
    'error', 'response.content.done', 'rate_limits.updated',
    'response.done', 'input_audio_buffer.committed',
    'input_audio_buffer.speech_stopped', 'input_audio_buffer.speech_started',
    'session.created'
]
SHOW_TIMING_MATH = False

app = FastAPI()

if not OPENAI_API_KEY:
    raise ValueError('Missing the OpenAI API key. Please set it in the .env file.')

@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Twilio Media Stream Server is running!"}

# ─────────────────────────────
# 1️⃣  Greeting + Language Menu
# ─────────────────────────────
# Offer English (default / silence) or Spanish (press 1)
@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(_: Request):
    vr = VoiceResponse()
    gather = vr.gather(
        action="/language-selection",
        method="POST",
        num_digits=1,
        timeout=4  # seconds to wait for DTMF before fallback
    )
    gather.say(
        "Thank you for contacting the Tip Line. For English, stay on the line. "
        "Para español, presione el uno.",
        voice="alice",
        language="en-US"
    )
    # If nothing pressed, Twilio will fall through to /language-selection
    vr.redirect("/language-selection")
    return HTMLResponse(str(vr), media_type="application/xml")

# ─────────────────────────────
# 2️⃣  Route based on choice
# ─────────────────────────────
@app.api_route("/language-selection", methods=["GET", "POST"])
async def language_selection(request: Request):
    form = await request.form()
    digits = form.get("Digits", "")
    lang = "es" if digits == "1" else "en"  # default to English

    vr = VoiceResponse()
    host = request.url.hostname  # dynamic for Railway / prod
    ws_url = f"wss://{host}/media-stream?lang={lang}"
    connect = Connect()
    connect.stream(url=ws_url)
    vr.append(connect)
    return HTMLResponse(str(vr), media_type="application/xml")

# ─────────────────────────────
# 3️⃣  Real‑time Media Stream
# ─────────────────────────────
@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    await websocket.accept()
    lang = websocket.query_params.get("lang", "en")  # 'en' or 'es'

    async with websockets.connect(
        'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01',
        extra_headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "OpenAI-Beta": "realtime=v1"
        }
    ) as openai_ws:
        await initialize_session(openai_ws, lang)
        await send_initial_conversation_item(openai_ws, lang)

        # ── Stateful vars ──
        stream_sid = None
        latest_media_timestamp = 0
        last_assistant_item = None
        mark_queue = []
        response_start_timestamp_twilio = None

        # ───────────────────
        # Twilio → OpenAI
        # ───────────────────
        async def receive_from_twilio():
            nonlocal stream_sid, latest_media_timestamp
            try:
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    if data['event'] == 'media' and openai_ws.open:
                        latest_media_timestamp = int(data['media']['timestamp'])
                        await openai_ws.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": data['media']['payload']
                        }))
                    elif data['event'] == 'start':
                        stream_sid = data['start']['streamSid']
                        latest_media_timestamp = 0
                        last_assistant_item = None
                    elif data['event'] == 'mark' and mark_queue:
                        mark_queue.pop(0)
            except WebSocketDisconnect:
                if openai_ws.open:
                    await openai_ws.close()

        # ───────────────────
        # OpenAI → Twilio
        # ───────────────────
        async def send_to_twilio():
            nonlocal stream_sid, last_assistant_item, response_start_timestamp_twilio
            try:
                async for oa_msg in openai_ws:
                    msg = json.loads(oa_msg)
                    if msg.get('type') == 'response.audio.delta' and 'delta' in msg:
                        audio_payload = base64.b64encode(base64.b64decode(msg['delta'])).decode('utf-8')
                        await websocket.send_json({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": audio_payload}
                        })
                        if response_start_timestamp_twilio is None:
                            response_start_timestamp_twilio = latest_media_timestamp
                        if msg.get('item_id'):
                            last_assistant_item = msg['item_id']
                        await send_mark(websocket, stream_sid)
                    if msg.get('type') == 'input_audio_buffer.speech_started' and last_assistant_item:
                        await handle_interrupt()
            except Exception as e:
                print("Error sending to Twilio:", e)

        async def handle_interrupt():
            nonlocal response_start_timestamp_twilio, last_assistant_item
            if mark_queue and response_start_timestamp_twilio is not None:
                elapsed = latest_media_timestamp - response_start_timestamp_twilio
                await openai_ws.send(json.dumps({
                    "type": "conversation.item.truncate",
                    "item_id": last_assistant_item,
                    "content_index": 0,
                    "audio_end_ms": elapsed
                }))
                await websocket.send_json({"event": "clear", "streamSid": stream_sid})
                mark_queue.clear()
                last_assistant_item = None
                response_start_timestamp_twilio = None

        async def send_mark(conn, sid):
            if sid:
                await conn.send_json({
                    "event": "mark",
                    "streamSid": sid,
                    "mark": {"name": "responsePart"}
                })
                mark_queue.append('responsePart')

        await asyncio.gather(receive_from_twilio(), send_to_twilio())

# ─────────────────────────────
# 4️⃣  OpenAI Session Helpers
# ─────────────────────────────
async def send_initial_conversation_item(openai_ws, lang: str):
    prompts = {
        "en": "Please greet the caller in English and ask how you can help.",
        "es": "Por favor, saluda al llamante en español y pregunta en qué puedes ayudar."
    }
    await openai_ws.send(json.dumps({
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": prompts.get(lang, prompts['en'])}]
        }
    }))
    await openai_ws.send(json.dumps({"type": "response.create"}))

async def initialize_session(openai_ws, lang: str):
    instructions = (
        "You are a helpful and bubbly AI assistant. "
        "You have a penchant for dad jokes, owl jokes, and subtle rickrolling. "
        "Always stay positive, but work in a joke when appropriate."
    )
    if lang == "es":
        instructions += " Respond only in Spanish."
    await openai_ws.send(json.dumps({
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad"},
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": VOICE,
            "instructions": instructions,
            "modalities": ["text", "audio"],
            "temperature": 0.8
        }
    }))

# ─────────────────────────────
# 5️⃣  Entrypoint
# ─────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
