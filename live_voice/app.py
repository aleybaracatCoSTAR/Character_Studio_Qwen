import asyncio
from contextlib import asynccontextmanager
from functools import partial
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from urllib.parse import unquote

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from studio.characters import Character, example
from .providers import LocalProviders, SpeechRecognition
from .session import LiveSession
from .turns import TurnDetector
from .voice import Delivery, MAX_START_BYTES, VoiceSettings, VOICE_DIRECTORY
from .reactions import EmotionalTrigger
from .director import DirectorSettings
from .narrative import NarrativeSettings, StoryControl
from .deployment import profile
from .inspection_downloads import ExportDownloads, MAX_EXPORT_BYTES
from .presets import preset, presets
from .archive import ArchiveSettings, ArchiveStore
from .archive_database import ArchiveDatabase, MAX_IMPORT_BYTES, MAX_DATABASE_ENTRIES
from .archive_source import QueeringTheMap, ArchiveSourceUnavailable, qtm_entry_id


async def _archive_job(callback, *args, dispose=None, **kwargs):
    """Keep ownership of a finite worker after caller cancellation.

    Thread cancellation cannot stop an import transaction. Wait for its actual
    completion before the caller removes its upload or releases the write lock.
    A canceled snapshot creation also closes the otherwise unclaimed result.
    Shield the executor Future directly, without a separately cancelable Task.
    """
    worker = asyncio.get_running_loop().run_in_executor(None, partial(callback, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancelled:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue  # Repeated cancellation must not abandon the thread.
            except BaseException:
                break
        try:
            result = worker.result()
        except BaseException:
            pass  # Retrieve worker errors, but preserve the caller cancellation.
        else:
            if dispose is not None:
                try:
                    await _archive_job(dispose, result)
                except BaseException:
                    pass  # Disposal is owned to completion; cancellation wins.
        raise cancelled


def create_app(preview=False, providers=None, vad_factory=None, archive_store=None, archive_importer=None):
    deployment = profile()
    public = Path(__file__).resolve().parent.parent / "frontend"
    services = providers or LocalProviders(SpeechRecognition(os.environ.get("LIVE_STT_MODEL_DIR"),
                                                           os.environ.get("LIVE_STT_DEVICE", "cpu")),
                                          os.environ.get("LIVE_LLM_URL", "http://127.0.0.1:18092"),
                                          os.environ.get("LIVE_TTS_URL", "http://127.0.0.1:18093"),
                                          director_url=os.environ.get("LIVE_DIRECTOR_URL"),
                                          director_model=os.environ.get("LIVE_DIRECTOR_MODEL", "live-character"),
                                          llm_model=os.environ.get("LIVE_LLM_MODEL", "live-character"),
                                          director_timeout=os.environ.get("LIVE_DIRECTOR_TIMEOUT",deployment["director_timeout"]),
                                          verify_voice=os.environ.get('LIVE_VERIFY_VOICE','1') != '0',
                                          verification_timeout=os.environ.get('LIVE_VOICE_VERIFY_TIMEOUT','20'))
    call_lock = asyncio.Lock()
    downloads = ExportDownloads()
    owns_archive_store = archive_store is None
    archive_store = archive_store or ArchiveDatabase(Path(os.environ.get('LIVE_ARCHIVE_DIR') or
        str(Path(os.environ.get('LIVE_VOICE_STORE', str(Path.home() / '.local/share/character-studio'))) / 'archive')))
    archive_importer = archive_importer or QueeringTheMap()
    archive_write_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(asyncio.to_thread(services.stt.load)) if not preview else None
        yield
        if task and not task.done(): task.cancel()
        downloads.clear()
        await services.close()
        if owns_archive_store: await asyncio.to_thread(archive_store.close)

    app = FastAPI(title="Character Studio · Live voice", lifespan=lifespan)

    @app.middleware("http")
    async def no_cache(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index(): return FileResponse(public / "live.html")

    @app.get('/archive/')
    def archive_index(): return FileResponse(public / 'live.html')

    def require_archive_origin(request):
        if request.headers.get('origin') != str(request.base_url).rstrip('/'):
            raise HTTPException(status_code=403,detail='Open the studio in its own browser tab.')

    def read_archive():
        try: return archive_store.list()
        except (OSError, ValueError):
            raise HTTPException(status_code=503,detail='The selected-story archive could not be read. Check its local storage.') from None

    def archive_page(q='', offset=0, limit=25):
        if isinstance(archive_store, ArchiveDatabase):
            try: return {**archive_store.page(query=q,offset=offset,limit=limit),
                         'scope':'library','selection_limit':50,'capacity':MAX_DATABASE_ENTRIES}
            except (OSError, ValueError, sqlite3.Error):
                raise HTTPException(status_code=503,detail='The local archive database could not be searched.') from None
        return {'entries':read_archive(),'limit':50,'scope':'selected'}

    @app.get('/api/archive/entries')
    def archive_entries(q: str = Query('',max_length=200), offset: int = Query(0,ge=0),
                        limit: int = Query(25,ge=1,le=50)):
        return archive_page(q,offset,limit)

    @app.get('/api/archive/selection')
    def archive_selection(ids: str = Query('',max_length=1700)):
        try:
            selected = ArchiveSettings(entry_ids=ids.split(',') if ids else []).entry_ids
            entries = []
            for entry_id in selected:
                try: entries.extend(entry.model_dump() for entry in archive_store.snapshot([entry_id]))
                except KeyError: pass
            return {'entries':entries}
        except (ValueError, TypeError):
            raise HTTPException(status_code=400,detail='Choose at most 50 saved story IDs.') from None
        except (OSError, sqlite3.Error):
            raise HTTPException(status_code=503,detail='The selected stories could not be read.') from None

    @app.post('/api/archive/import')
    async def import_archive_file(request: Request):
        require_archive_origin(request)
        if not isinstance(archive_store, ArchiveDatabase):
            raise HTTPException(status_code=409,detail='Restart with the archive database enabled to import a collection.')
        if request.headers.get('content-type','').split(';',1)[0].strip().lower() != 'application/octet-stream':
            raise HTTPException(status_code=400,detail='Upload a JSON, JSONL or CSV collection file.')
        filename = unquote(request.headers.get('x-archive-filename',''))
        suffix = Path(filename).suffix.lower()
        if suffix not in ('.json','.jsonl','.csv') or len(filename)>500:
            raise HTTPException(status_code=400,detail='Choose a JSON, JSONL or CSV collection file.')
        declared = request.headers.get('content-length')
        if declared is not None:
            try:
                if int(declared)<0: raise ValueError()
                if int(declared)>MAX_IMPORT_BYTES:
                    raise HTTPException(status_code=413,detail='The collection file exceeds 128 MiB.')
            except ValueError:
                raise HTTPException(status_code=400,detail='The collection upload size is invalid.') from None
        async with archive_write_lock:
            if call_lock.locked(): raise HTTPException(status_code=409,detail='End the current call before importing a collection.')
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(prefix='character-archive-',suffix=suffix,delete=False) as stream:
                    temporary = Path(stream.name)
                    size = 0
                    async for packet in request.stream():
                        size += len(packet)
                        if size>MAX_IMPORT_BYTES:
                            raise HTTPException(status_code=413,detail='The collection file exceeds 128 MiB.')
                        stream.write(packet)
                if call_lock.locked(): raise HTTPException(status_code=409,detail='End the current call before importing a collection.')
                result = await _archive_job(archive_store.import_file, temporary)
                return {**result,**archive_page()}
            except OverflowError:
                raise HTTPException(status_code=413,detail='The collection exceeds the archive capacity or story-length limit.') from None
            except (ValueError, TypeError, UnicodeError):
                raise HTTPException(status_code=400,detail='The collection could not be imported. Check its format and story fields; no partial import was saved.') from None
            except (OSError, sqlite3.Error):
                raise HTTPException(status_code=503,detail='The collection could not be saved to the local database.') from None
            finally:
                if temporary is not None: temporary.unlink(missing_ok=True)

    @app.post('/api/archive/entries', status_code=201)
    async def add_archive_entry(request: Request):
        require_archive_origin(request)
        if request.headers.get('content-type','').split(';',1)[0].strip().lower() != 'application/json':
            raise HTTPException(status_code=400,detail='Use an archive story request.')
        body = bytearray()
        async for packet in request.stream():
            body.extend(packet)
            if len(body) > 64 * 1024:
                raise HTTPException(status_code=413,detail='The selected story is too large.')
        try:
            values = json.loads(body)
            if not isinstance(values,dict) or set(values) - {'entry_id','title','text','source_url'}:
                raise ValueError()
            if any(not isinstance(value,str) for value in values.values()): raise ValueError()
            if 'entry_id' in values:
                if any(values.get(key,'').strip() for key in ('title','text','source_url')): raise ValueError()
                entry_id = qtm_entry_id(values['entry_id'])
            else:
                if not values.get('text','').strip(): raise ValueError()
                values['source_url'] = values.get('source_url','').strip() or 'https://www.queeringthemap.com/'
                entry_id = None
        except (ValueError, TypeError, UnicodeError):
            raise HTTPException(status_code=400,detail='Choose one story number/entry URL, or paste a story with its Queering the Map source.') from None
        async with archive_write_lock:
            if call_lock.locked(): raise HTTPException(status_code=409,detail='End the current call before changing its archive.')
            try:
                entry = await archive_importer.fetch(entry_id) if entry_id is not None else values
                if entry_id is not None: entry = {**entry,'provenance':'fetched'}
                if call_lock.locked(): raise HTTPException(status_code=409,detail='End the current call before changing its archive.')
                saved = await _archive_job(archive_store.add,**entry)
                return {'entry':saved,**archive_page()} if isinstance(archive_store,ArchiveDatabase) else {'entry':saved,'entries':archive_store.list()}
            except ArchiveSourceUnavailable:
                raise HTTPException(status_code=502,detail='That story could not be read from Queering the Map. Check its number, or paste a selected story instead.') from None
            except OverflowError:
                raise HTTPException(status_code=413,detail='This archive is full or the story is too long. Remove a story before adding more.') from None
            except (ValueError, TypeError):
                raise HTTPException(status_code=400,detail='Use a nonempty story, a short title and a valid Queering the Map source URL.') from None
            except (OSError, sqlite3.Error):
                raise HTTPException(status_code=503,detail='The story could not be saved to the local archive.') from None

    @app.delete('/api/archive/entries/{entry_id}')
    async def remove_archive_entry(entry_id: str, request: Request):
        require_archive_origin(request)
        async with archive_write_lock:
            if call_lock.locked(): raise HTTPException(status_code=409,detail='End the current call before changing its archive.')
            try:
                if not await _archive_job(archive_store.remove,entry_id): raise HTTPException(status_code=404,detail='Story not found.')
                return archive_page() if isinstance(archive_store,ArchiveDatabase) else {'entries':archive_store.list()}
            except (ValueError, OSError, sqlite3.Error):
                raise HTTPException(status_code=503,detail='The story could not be removed from the local archive.') from None

    @app.get("/api/example")
    def character(): return example().model_dump()

    @app.get('/api/characters/presets')
    def character_presets(): return presets()

    @app.get('/api/characters/presets/{preset_id}')
    def character_preset(preset_id: str):
        try: return preset(preset_id)
        except KeyError: raise HTTPException(status_code=404,detail='Character preset not found.') from None

    @app.get("/api/story/example")
    def story_example():
        path=Path(__file__).resolve().parent.parent/'characters/anna-story.json'
        return NarrativeSettings.model_validate_json(path.read_text()).model_dump()

    @app.get("/api/voice/{preset}")
    def voice_preview(preset: str):
        from fastapi import HTTPException
        if preset not in ("female", "male"): raise HTTPException(status_code=404)
        return FileResponse(VOICE_DIRECTORY / f"{preset}.wav",media_type="audio/wav")

    @app.get("/api/status")
    async def status():
        health = {"stt":False,"llm":False,"tts":False} if preview else await services.health()
        return {"preview":preview,"ready":all(health.values()),"services":health,
                "voice_pipeline":"responsive-context-v4",
                "model_inspection":True,
                "inspection_download":True,
                "character_presets":True,
                "archive":({"available":True,"scope":"library","limit":MAX_DATABASE_ENTRIES,
                            "selection_limit":50,"bulk_import":True} if isinstance(archive_store,ArchiveDatabase)
                           else {"available":True,"scope":"selected","limit":50}),
                "beat_entry_replies":True, "voice_retry":True,
                "voice_verification":bool(getattr(services,'verify_voice',False)),
                "busy":call_lock.locked(), "stt_error":getattr(services.stt,"error",None),
                "profile":os.environ.get("LIVE_PROFILE","shared"),
                "models":{"actor":deployment['actor']['id'],"director":(deployment['director'] or deployment['actor'])['id']},
                "director":{"model":getattr(services,"director_model","live-character"),
                            "shared_model":getattr(services,"director_url",None)==getattr(services,"llm_url",None) and getattr(services,"director_model","live-character")=="live-character"}}

    @app.post('/api/inspection/downloads')
    async def prepare_inspection_download(request: Request):
        expected = str(request.base_url).rstrip('/')
        if request.headers.get('origin') != expected:
            raise HTTPException(status_code=403,detail='Open the studio in its own browser tab.')
        if request.headers.get('content-type','').split(';',1)[0].lower() != 'application/json':
            raise HTTPException(status_code=400,detail='Use a model-input download request.')
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_EXPORT_BYTES:
                raise HTTPException(status_code=413,detail='The model-input download is too large.')
        try:
            values = json.loads(data)
            if not isinstance(values,dict): raise ValueError()
            token = downloads.create(values.get('filename'),values.get('content'),values.get('mime'))
        except OverflowError:
            raise HTTPException(status_code=413,detail='The model-input download is too large.') from None
        except (ValueError,TypeError,UnicodeError):
            raise HTTPException(status_code=400,detail='Choose a valid text or JSON model-input download.') from None
        return {'download_url':'/api/inspection/downloads/'+token}

    @app.get('/api/inspection/downloads/{token}')
    async def get_inspection_download(token: str, request: Request):
        origin = request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            raise HTTPException(status_code=403,detail='Open the studio in its own browser tab.')
        export = downloads.get(token)
        if export is None:
            raise HTTPException(status_code=404,detail='This download expired. Download the request again from the viewer.')
        return Response(export['content'],media_type=export['mime']+';charset=utf-8',headers={
            'Content-Disposition':'attachment; filename="'+export['filename']+'"',
            'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})

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
        archive_search = None
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
            initial = await asyncio.wait_for(socket.receive_text(), timeout=15)
            if len(initial.encode("utf-8")) > MAX_START_BYTES: raise ValueError("Voice reference is too large.")
            config = json.loads(initial)
            if not isinstance(config, dict) or config.get("type") != "start":
                raise ValueError("Start a character call first.")
            selected = Character.model_validate(config.get("character"))
            voice = VoiceSettings.model_validate(config.get("voice", {}))
            trigger = EmotionalTrigger.model_validate(config["trigger"]) if config.get("trigger") is not None else None
            director = DirectorSettings.model_validate(config.get("director", {}))
            narrative = NarrativeSettings.model_validate(config.get("narrative", {}))
            archive = ArchiveSettings.model_validate(config.get('archive', {}))
            try:
                archive_entries = ()
                if archive.enabled:
                    async with archive_write_lock:
                        if archive.scope == 'library':
                            if not isinstance(archive_store,ArchiveDatabase): raise ValueError('Database unavailable.')
                            archive_search = await _archive_job(archive_store.open_snapshot, dispose=lambda snapshot:snapshot.close())
                        else:
                            archive_entries = await _archive_job(archive_store.snapshot,archive.entry_ids)
            except (KeyError, ValueError, OSError, sqlite3.Error):
                await send_json({'type':'error','message':'The selected archive changed. End the call, check its stories and try again.'})
                return
            if vad_factory:
                detector = vad_factory()
            else:
                import webrtcvad
                vad = webrtcvad.Vad(2)
                detector = TurnDetector(lambda pcm:vad.is_speech(pcm,16000))
            session = LiveSession(services, selected, send_json, send_bytes, voice=voice, trigger=trigger, director=director,
                                  narrative=narrative, archive=archive if archive.enabled else None, archive_entries=archive_entries,
                                  archive_search=archive_search)
            listen_after = 0.0
            def clear_microphone():
                detector.pending.clear(); detector.reset()
                session.input_active = False
            await send_json({"type":"ready", "sample_rate":16000, "output_rate":44100,
                             "endpoint_ms":detector.silence_frames * 20, "turn_taking":"Stop reply to interrupt; mic resumes after playback",
                             "voice":"uploaded reference" if voice.reference_audio else f"{voice.preset} reference" if voice.preset in ("female","male") else "first generated reply anchors this call"})
            await session.start_story()
            while True:
                message = await socket.receive()
                if message["type"] == "websocket.disconnect": break
                if message.get("bytes") is not None:
                    packet = message["bytes"]
                    if len(packet) > 8192 or len(packet) % 2: raise ValueError("Invalid microphone frame.")
                    # Speech detection alone cannot distinguish a person from speaker echo or noise.
                    # Discard input while generating AND playing; do not buffer it for the next turn.
                    if session.reply_pending or time.monotonic() < listen_after:
                        clear_microphone()
                        continue
                    for event, pcm, endpoint_ms in detector.feed(packet):
                        if event == "start":
                            session.input_active = True
                            await send_json({"type":"speech_start"})
                        elif event == "prepare":
                            session.prepare_recognition(pcm)
                        elif event == "utterance":
                            await session.begin(pcm=pcm, endpoint_ms=endpoint_ms)
                            clear_microphone()
                            break
                        elif event == "noise":
                            session.input_active = False
                            await send_json({"type":"listening"})
                elif message.get("text") is not None:
                    if len(message["text"]) > 16000: raise ValueError("Control message too long.")
                    command = json.loads(message["text"])
                    if not isinstance(command, dict): raise ValueError("Invalid control message.")
                    if command.get("type") == "inspect":
                        await session.inspection.subscribe(command.get('enabled'))
                    elif command.get("type") == "retry_voice":
                        turn = command.get('turn_id')
                        if type(turn) is not int: raise ValueError('Invalid voice retry.')
                        if not await session.retry_voice(turn):
                            await send_json({'type':'voice_retry_unavailable', 'turn_id':turn,
                                             'message':'This voice reply is no longer available. Finish speaking or send a new message.'})
                    elif command.get("type") == "played":
                        turn, sequence = command.get("turn_id"), command.get("seq")
                        if type(turn) is not int or type(sequence) is not int:
                            raise ValueError("Invalid playback acknowledgement.")
                        session.history.acknowledge(turn,sequence)
                    elif command.get("type") == "playback_done":
                        turn = command.get("turn_id")
                        if type(turn) is not int: raise ValueError("Invalid playback completion.")
                        if session.playback_done(turn):
                            clear_microphone()
                            # Allow loudspeaker audio to decay before accepting another utterance.
                            listen_after = time.monotonic() + 0.35
                            await session.story_boundary()
                    elif command.get("type") == "text":
                        text = command.get("text","")
                        if not isinstance(text,str) or not 0 < len(text.strip()) <= 2000: raise ValueError("Enter a short message.")
                        await session.begin(text=text.strip())
                    elif command.get("type") == "mute":
                        clear_microphone()
                        # Muting capture must not cancel a finalized turn's ASR.
                        if not session.reply_pending: await session.prepared_recognition.clear()
                    elif command.get("type") == "delivery":
                        delivery = Delivery.model_validate(command.get("settings"))
                        session.voice = session.voice.model_copy(update=delivery.model_dump())
                    elif command.get("type") == "director":
                        session.director = DirectorSettings.model_validate(command.get("settings"))
                        await send_json({"type":"director_ack","message":"Direction saved for the next reply."})
                    elif command.get("type") == "story":
                        control=StoryControl.model_validate(command.get("control"))
                        await session.story_control(control.action)
                    elif command.get("type") == "trigger":
                        clear_microphone()
                        await session.fire_trigger()
                    elif command.get("type") == "clear_trigger":
                        await session.trigger_state(False)
                    elif command.get("type") == "interrupt":
                        await session.interrupt()
                        await session.prepared_recognition.clear()
                        clear_microphone()
                        listen_after = time.monotonic() + 0.35
                    elif command.get("type") == "stop": break
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass
        except ValidationError as exc:
            reason = exc.errors(include_input=False)[0]["msg"]
            try: await send_json({"type":"error", "message":"Check the call settings: " + reason})
            except RuntimeError: pass
        except (ValueError, json.JSONDecodeError):
            try: await send_json({"type":"error", "message":"Check the character fields and reconnect."})
            except RuntimeError: pass
        finally:
            if session: await session.close()
            elif archive_search is not None: await asyncio.to_thread(archive_search.close)
            call_lock.release()
            try: await socket.close()
            except RuntimeError: pass

    app.mount("/static",StaticFiles(directory=public),name="static")
    return app
