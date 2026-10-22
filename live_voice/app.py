import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from studio.characters import Character, example
from .providers import LocalProviders, SpeechRecognition
from .session import LiveSession
from .turns import TurnDetector


def create_app(preview=False, providers=None, vad_factory=None):
    public = Path(__file__).resolve().parent.parent / "frontend"
    services = providers or LocalProviders(SpeechRecognition(os.environ.get("LIVE_STT_MODEL_DIR"),
                                                           os.environ.get("LIVE_STT_DEVICE", "cpu")),
                                          os.environ.get("LIVE_LLM_URL", "http://127.0.0.1:18092"),
                                          os.environ.get("LIVE_TTS_URL", "http://127.0.0.1:18093"))
    call_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(asyncio.to_thread(services.stt.load)) if not preview else None
        yield
        if task and not task.done(): task.cancel()
        await services.close()

    app = FastAPI(title="Character Studio · Live voice", lifespan=lifespan)

    @app.middleware("http")
    async def no_cache(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index(): return FileResponse(public / "live.html")

    @app.get("/api/example")
    def character(): return example().model_dump()

    @app.get("/api/status")
    async def status():
        health = {"stt":False,"llm":False,"tts":False} if preview else await services.health()
        return {"preview":preview,"ready":all(health.values()),"services":health,
                "busy":call_lock.locked(), "stt_error":getattr(services.stt,"error",None)}

    @app.websocket("/api/live")
    async def live(socket: WebSocket):
        origin = socket.headers.get("origin")
        expected = str(socket.base_url).replace("ws://", "http://").replace("wss://", "https://").rstrip("/")
        if not origin or origin != expected:
            await socket.close(code=1008, reason="Open the studio's own browser tab."); return
        if preview or call_lock.locked():
            await socket.close(code=1013, reason="Preview mode or another active call."); return
        await call_lock.acquire()
        session = None
        send_lock = asyncio.Lock()
        async def send_json(value):
            async with send_lock: await socket.send_json(value)
        async def send_bytes(value):
            async with send_lock: await socket.send_bytes(value)
        try:
            await socket.accept()
            health = await services.health()
            if not all(health.values()):
                await send_json({"type":"error", "message":"Start speech recognition, the text model and Drama 3 first."}); return
            config = await asyncio.wait_for(socket.receive_json(), timeout=15)
            if not isinstance(config, dict) or config.get("type") != "start":
                raise ValueError("Start a character call first.")
            selected = Character.model_validate(config.get("character"))
            if vad_factory:
                detector = vad_factory()
            else:
                import webrtcvad
                vad = webrtcvad.Vad(2)
                detector = TurnDetector(lambda pcm:vad.is_speech(pcm,16000))
            session = LiveSession(services, selected, send_json, send_bytes)
            await send_json({"type":"ready", "sample_rate":16000, "output_rate":44100,
                             "endpoint_ms":480, "voice":"first generated phrase anchors this call"})
            while True:
                message = await socket.receive()
                if message["type"] == "websocket.disconnect": break
                if message.get("bytes") is not None:
                    packet = message["bytes"]
                    if len(packet) > 8192 or len(packet) % 2: raise ValueError("Invalid microphone frame.")
                    for event, pcm, endpoint_ms in detector.feed(packet):
                        if event == "start":
                            await session.interrupt()
                            await send_json({"type":"speech_start"})
                        elif event == "utterance":
                            await session.begin(pcm=pcm, endpoint_ms=endpoint_ms)
                        elif event == "noise": await send_json({"type":"listening"})
                elif message.get("text") is not None:
                    if len(message["text"]) > 16000: raise ValueError("Control message too long.")
                    command = json.loads(message["text"])
                    if not isinstance(command, dict): raise ValueError("Invalid control message.")
                    if command.get("type") == "played":
                        turn, sequence = command.get("turn_id"), command.get("seq")
                        if type(turn) is not int or type(sequence) is not int:
                            raise ValueError("Invalid playback acknowledgement.")
                        session.history.acknowledge(turn,sequence)
                    elif command.get("type") == "text":
                        text = command.get("text","")
                        if not isinstance(text,str) or not 0 < len(text.strip()) <= 2000: raise ValueError("Enter a short message.")
                        await session.begin(text=text.strip())
                    elif command.get("type") == "mute":
                        detector.pending.clear(); detector.reset()
                    elif command.get("type") == "interrupt":
                        await session.interrupt()
                    elif command.get("type") == "stop": break
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass
        except (ValueError, ValidationError, json.JSONDecodeError):
            try: await send_json({"type":"error", "message":"Check the character fields and reconnect."})
            except RuntimeError: pass
        finally:
            if session: await session.close()
            call_lock.release()
            try: await socket.close()
            except RuntimeError: pass

    app.mount("/static",StaticFiles(directory=public),name="static")
    return app
