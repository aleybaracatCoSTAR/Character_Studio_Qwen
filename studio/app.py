"""A same-origin local studio. No public hosting or external model API."""
import asyncio
import base64
from contextlib import asynccontextmanager
import json
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .audio import MAX_AUDIO_BYTES, read_recording
from .characters import Character, example, system_prompt
from .engine import Busy, CharacterChanged, NotReady, QwenEngine, UserTurn


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    character: Character
    text: str = Field(default="", max_length=2000)
    session_id: UUID | None = None
    speak: bool = True


def create_app(engine=None):
    engine = engine or QwenEngine(preview=True)
    public = Path(__file__).resolve().parent.parent / "frontend"

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(asyncio.to_thread(engine.load)) if not engine.preview else None
        yield
        if task and not task.done():
            task.cancel()

    app = FastAPI(title="Character Studio Qwen", lifespan=lifespan)
    app.state.engine = engine

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        # Reject cross-origin writes; this is still a private localhost prototype, not an authenticated service.
        origin = request.headers.get("origin")
        if request.method == "POST" and origin and origin != str(request.base_url).rstrip("/"):
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "Use the studio's own browser tab."}, status_code=403)
        try:
            content_length = int(request.headers.get("content-length", "0"))
        except ValueError:
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "Invalid request length."}, status_code=400)
        if content_length > MAX_AUDIO_BYTES + 65536:
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "Recording is too large."}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/")
    def index():
        return FileResponse(public / "index.html")

    @app.get("/api/status")
    def status():
        return engine.status()

    @app.get("/api/example")
    def get_example():
        return example().model_dump()

    @app.post("/api/prompt")
    def preview_prompt(character: Character):
        return {"prompt": system_prompt(character)}

    @app.post("/api/chat")
    async def chat(payload: str = Form(...), audio: UploadFile | None = File(default=None)):
        try:
            data = ChatRequest.model_validate_json(payload)
        except (ValidationError, json.JSONDecodeError) as exc:
            raise HTTPException(422, "Check the character fields and question.") from exc
        samples = None
        if audio is not None:
            raw = await audio.read(MAX_AUDIO_BYTES + 1)
            await audio.close()
            try:
                samples = await asyncio.to_thread(read_recording, raw)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        if not data.text.strip() and samples is None:
            raise HTTPException(400, "Type a question or record your voice first.")
        try:
            result = await asyncio.to_thread(engine.reply, data.character, UserTurn(data.text.strip(), samples),
                                             data.session_id, data.speak)
        except NotReady as exc:
            raise HTTPException(503, str(exc)) from exc
        except (Busy, CharacterChanged) as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            # No prompts, recordings or raw traceback are sent to the browser or access log.
            raise HTTPException(500, "Generation failed. Check GPU memory and the server terminal.") from exc
        wav = result.pop("wav")
        return {**result, "audio_base64": base64.b64encode(wav).decode() if wav else None,
                "audio_mime": "audio/wav" if wav else None}

    app.mount("/static", StaticFiles(directory=public), name="static")
    return app
