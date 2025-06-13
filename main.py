import os
import json
import base64
import audioop
import asyncio
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from openai import AsyncOpenAI
import websockets

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID = os.getenv("ASSISTANT_ID")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = AsyncOpenAI(api_key=OPENAI_API_KEY)

@app.post("/incoming-call")
async def incoming_call(request: Request):
    response = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather action="/language-selection" numDigits="1" timeout="5">
        <Say>Welcome to the employee tip line. For English, press 1. Para español, oprima dos.</Say>
    </Gather>
    <Say>No input received. Goodbye.</Say>
</Response>
"""
    return PlainTextResponse(response, media_type="application/xml")

@app.post("/language-selection")
async def language_selection(request: Request):
    form = await request.form()
    digits = form.get("Digits", "")
    lang = "en" if digits != "2" else "es"

    public_url = os.getenv("PUBLIC_URL")
    if not public_url:
        host = request.headers.get("host")
        if not host:
            raise RuntimeError("PUBLIC_URL env var not set. Set it to your public https domain (no protocol)")
        public_url = host
        print(f"PUBLIC_URL not set. Using domain from request: {public_url}")
    else:
        print(f"PUBLIC_URL set from env: {public_url}")

    websocket_url = f"wss://{public_url}/media-stream?lang={lang}"
    print(f"Language selected: {lang} (digits={digits})")
    print(f"Twilio WebSocket URL: {websocket_url}")

    response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Start>
        <Stream url="{websocket_url}" />
    </Start>
    <Say>Start speaking after the beep.</Say>
    <Pause length="60" />
    <Say>We did not receive any input. Goodbye.</Say>
</Response>
"""
    return PlainTextResponse(response, media_type="application/xml")

@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()
    print("WebSocket connection open")

    try:
        params = websocket.query_params
        lang = params.get("lang", "en")

        async with websockets.connect(
            "wss://api.openai.com/v1/realtime/speech",
            extra_headers={"Authorization": f"Bearer {OPENAI_API_KEY}"}
        ) as ai_ws:
            await ai_ws.send(json.dumps({
                "assistant_id": ASSISTANT_ID,
                "language": lang
            }))

            while True:
                data = await websocket.receive_text()
                msg = json.loads(data)

                evt = msg.get("event")
                if evt == "start":
                    stream_sid = msg["start"]["streamSid"]
                    print(f"Twilio stream started: {stream_sid}")
                elif evt == "media":
                    audio = base64.b64decode(msg["media"]["payload"])
                    pcm = audioop.ulaw2lin(audio, 2)
                    await ai_ws.send(pcm)
                elif evt == "stop":
                    print("Twilio stream stopped")
                    break

    except WebSocketDisconnect:
        print("WebSocket disconnected")

    except Exception as e:
        print(f"Unhandled error: {e}")

    finally:
        await websocket.close()
        print("WebSocket connection closed")
