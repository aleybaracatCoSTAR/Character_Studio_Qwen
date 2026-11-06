import asyncio
from contextlib import asynccontextmanager
from functools import partial
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from typing import Literal
from urllib.parse import unquote

from anyio import CancelScope
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Query
from fastapi.responses import FileResponse, Response, JSONResponse
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
from .collections import CollectionDatabase
from .collection_catalog import CollectionCatalog
from .museum_common import GUIDE_URL, MUSEUM_SOURCES, SOURCES, MuseumImportError, validate_import_input
from .museum_import import MuseumImporter, museum_error
from .museum_jobs import MuseumJobs
from .museum_image_lookup import MuseumImageLookup
from .engines import engine_name
from .museum_images import MuseumImages, MuseumImageUnavailable
from .listening import ListeningClient, ListeningSession, ListeningError
from .listening_config import EXPERIMENT, listening_mode, listening_profile


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


def create_app(preview=False, providers=None, vad_factory=None, archive_store=None, archive_importer=None, listening_client=None,
               collections_store=None, museum_importer=None, museum_images=None, museum_bulk_runner=None, museum_image_lookup=None,
               omni_provider=None):
    input_mode = listening_mode()
    deployment = listening_profile(profile(),input_mode)
    experimental = (listening_client or ListeningClient(os.environ.get('LIVE_LISTENING_URL','http://127.0.0.1:18095'))) if input_mode == EXPERIMENT else None
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
    default_engine = engine_name(os.environ.get('LIVE_DEFAULT_ENGINE','baseline'))
    if omni_provider is None and os.environ.get('LIVE_OMNI_URL'):
        from .omni import OmniProvider
        omni_provider = OmniProvider(services,os.environ['LIVE_OMNI_URL'],
            model=os.environ.get('LIVE_OMNI_MODEL','Qwen/Qwen3-Omni-30B-A3B-Instruct'))
    if default_engine == 'qwen-omni' and omni_provider is None:
        raise ValueError('Set LIVE_OMNI_URL before selecting the Qwen3-Omni runtime.')
    call_lock = asyncio.Lock()
    downloads = ExportDownloads()
    owns_archive_store = archive_store is None
    archive_directory = Path(os.environ.get('LIVE_ARCHIVE_DIR') or
        str(Path(os.environ.get('LIVE_VOICE_STORE', str(Path.home() / '.local/share/character-studio'))) / 'archive'))
    archive_store = archive_store or ArchiveDatabase(archive_directory)
    owns_collections_store = collections_store is None
    collections_store = collections_store or CollectionDatabase(archive_directory)
    collection_catalog = CollectionCatalog(archive_store, collections_store)
    museum_importer = museum_importer or MuseumImporter()
    museum_jobs = MuseumJobs(collections_store, runner=museum_bulk_runner)
    museum_images = museum_images or MuseumImages()
    museum_image_lookup = museum_image_lookup or MuseumImageLookup()
    archive_importer = archive_importer or QueeringTheMap()
    archive_write_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(asyncio.to_thread(services.stt.load)) if not preview else None
        yield
        if task and not task.done(): task.cancel()
        downloads.clear()
        await asyncio.to_thread(museum_jobs.close)
        if omni_provider is not None: await omni_provider.close()
        await services.close()
        if experimental is not None: await experimental.close()
        await museum_images.close()
        await museum_image_lookup.close()
        if owns_archive_store: await asyncio.to_thread(archive_store.close)
        if owns_collections_store: await asyncio.to_thread(collections_store.close)

    async def service_health(selected_services=None):
        health = await (selected_services or services).health()
        if experimental is not None:
            listening_health = await experimental.health()
            health = {**health,'streaming_asr':bool(listening_health.get('ready') and listening_health.get('asr')),
                      'turn_detector':bool(listening_health.get('ready') and listening_health.get('turn'))}
        return health

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

    @app.get('/archive/gallery/')
    def archive_gallery(): return FileResponse(public / 'live.html')

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

    @app.get('/api/archive/collections')
    def list_archive_collections():
        try: return {'collections':collection_catalog.list_collections()}
        except (OSError, ValueError, sqlite3.Error):
            raise HTTPException(status_code=503,detail='The saved collections could not be read.') from None

    @app.get('/api/archive/museums')
    def museum_sources():
        return {'sources':[source.public() for source in MUSEUM_SOURCES], 'guide_url':GUIDE_URL,
                'import_scope':'artist_or_subject_search', 'maximum_results':200,
                'full_collection_import':True,'full_import_resumable':True}

    @app.get('/api/archive/museums/imports/current')
    def current_museum_import():
        return {'job':museum_jobs.current()}

    @app.post('/api/archive/museums/imports/{identity}/stop')
    def stop_museum_import(identity: str, request: Request):
        require_archive_origin(request)
        try: return {'job':museum_jobs.stop(identity)}
        except KeyError:
            raise HTTPException(status_code=404,detail='This museum import is no longer current.') from None

    @app.post('/api/archive/museums/import')
    async def import_museum_collection(request: Request):
        require_archive_origin(request)
        if request.headers.get('content-type','').split(';',1)[0].strip().lower()!='application/json':
            raise HTTPException(status_code=400,detail='Choose a museum and an artist or subject.')
        raw = bytearray()
        async for part in request.stream():
            if len(raw)+len(part)>2048:
                raise HTTPException(status_code=413,detail='Use a short museum import request.')
            raw.extend(part)
        try:
            values = json.loads(raw)
            if not isinstance(values,dict) or set(values)-{'source','query','limit','scope'}: raise ValueError()
            source, query, limit = values.get('source'), values.get('query'), values.get('limit',25)
            scope = values.get('scope','matching')
            if scope not in ('matching','full'): raise ValueError()
            if scope == 'full':
                if not isinstance(source,str) or source not in (*SOURCES,'all') or 'query' in values or 'limit' in values:
                    raise ValueError()
                async with archive_write_lock:
                    if call_lock.locked(): raise HTTPException(status_code=409,detail='End the current call before starting a full museum import.')
                    try:
                        return JSONResponse({'job':museum_jobs.start(source)},status_code=202)
                    except RuntimeError as error:
                        raise HTTPException(status_code=409,detail=str(error)) from None
            if not isinstance(source,str) or source not in SOURCES or type(limit) is not int or limit>200: raise ValueError()
            query, limit = validate_import_input(source,query,limit)
        except (ValueError,TypeError,UnicodeError):
            raise HTTPException(status_code=400,detail='Choose a museum, an artist or subject up to 200 characters, and 1–200 artworks.') from None
        async with archive_write_lock:
            if call_lock.locked(): raise HTTPException(status_code=409,detail='End the current call before importing a museum collection.')
            if museum_jobs.running: raise HTTPException(status_code=409,detail='Stop the full collection import before importing a selection.')
            try:
                result = await _archive_job(museum_importer.import_collection,collections_store,source,query,limit)
                return {'results':[result], 'errors':[]}
            except MuseumImportError as error:
                return {'results':[], 'errors':[museum_error(error,source)]}
            except (OSError,sqlite3.Error):
                return {'results':[], 'errors':[museum_error(MuseumImportError(source,'storage_error'),source)]}

    @app.get('/api/archive/museum-images/aic/{identifier}')
    async def chicago_thumbnail(identifier: str):
        try: data = await museum_images.fetch(collections_store,identifier)
        except MuseumImageUnavailable:
            raise HTTPException(status_code=404,detail='This artwork thumbnail is unavailable. Open its museum source link.') from None
        except (OSError,sqlite3.Error):
            raise HTTPException(status_code=503,detail='The artwork thumbnail could not be read.') from None
        return Response(data,media_type='image/jpeg',headers={'X-Content-Type-Options':'nosniff'})

    @app.get('/api/archive/collections/entries')
    def collection_entries(ids: str = Query('',max_length=520), q: str = Query('',max_length=200),
                           offset: int = Query(0,ge=0), limit: int = Query(25,ge=1,le=50),
                           search_mode: Literal['smart','literal'] = 'smart'):
        try: return collection_catalog.page(ids.split(',') if ids else [], query=q, offset=offset, limit=limit,
                                           literal=search_mode=='literal')
        except KeyError:
            raise HTTPException(status_code=404,detail='A selected collection is no longer available.') from None
        except (ValueError, TypeError):
            raise HTTPException(status_code=400,detail='Choose between one and eight saved collections.') from None
        except (OSError, sqlite3.Error):
            raise HTTPException(status_code=503,detail='The saved collections could not be searched.') from None

    @app.post('/api/archive/collections/import')
    async def import_named_collection(request: Request):
        require_archive_origin(request)
        if request.headers.get('content-type','').split(';',1)[0].strip().lower() != 'application/octet-stream':
            raise HTTPException(status_code=400,detail='Upload a JSON, JSONL or CSV collection file.')
        filename = unquote(request.headers.get('x-archive-filename',''))
        name = unquote(request.headers.get('x-collection-name','')).strip()
        suffix = Path(filename).suffix.lower()
        if (suffix not in ('.json','.jsonl','.csv') or not 0<len(filename)<=200
                or any(ord(character)<32 or ord(character)==127 for character in filename)):
            raise HTTPException(status_code=400,detail='Choose a JSON, JSONL or CSV collection file.')
        if (not 0<len(name)<=80 or any(ord(character)<32 or ord(character)==127 for character in name)):
            raise HTTPException(status_code=400,detail='Give the collection a name within 80 characters.')
        declared = request.headers.get('content-length')
        if declared is not None:
            try:
                if int(declared)<0: raise ValueError()
                if int(declared)>MAX_IMPORT_BYTES:
                    raise HTTPException(status_code=413,detail='The collection file exceeds 128 MiB.')
            except ValueError:
                raise HTTPException(status_code=400,detail='The collection upload size is invalid.') from None
        async with archive_write_lock:
            if call_lock.locked(): raise HTTPException(status_code=409,detail='End the current call before adding a collection.')
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(prefix='character-collection-',suffix=suffix,delete=False) as stream:
                    temporary = Path(stream.name)
                    size = 0
                    async for packet in request.stream():
                        size += len(packet)
                        if size>MAX_IMPORT_BYTES:
                            raise HTTPException(status_code=413,detail='The collection file exceeds 128 MiB.')
                        stream.write(packet)
                if call_lock.locked(): raise HTTPException(status_code=409,detail='End the current call before adding a collection.')
                return await _archive_job(collections_store.import_file, temporary, label=name)
            except OverflowError:
                raise HTTPException(status_code=413,detail='The collection exceeds the record or field limits.') from None
            except (ValueError, TypeError, UnicodeError):
                raise HTTPException(status_code=400,detail='The file needs title, text and public HTTPS source_url fields. Check its format; no partial import was saved.') from None
            except (OSError, sqlite3.Error):
                raise HTTPException(status_code=503,detail='The collection could not be saved. Check the archive storage.') from None
            finally:
                if temporary is not None: temporary.unlink(missing_ok=True)

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
        baseline_health = {"stt":False,"llm":False,"tts":False} if preview else await service_health()
        omni_health = ({'stt':False,'omni':False,'director':False} if preview or omni_provider is None
                       else await service_health(omni_provider))
        health = omni_health if default_engine == 'qwen-omni' else baseline_health
        return {"preview":preview,"ready":all(health.values()),"services":health,
                "voice_pipeline":"qwen-omni-stream-v1" if default_engine == 'qwen-omni' else "voice-checked-v5", "voice_progress":True,
                'comparison':{'default_engine':default_engine,'director_policy':'same-turn',
                    'engines':{'baseline':{'available':True,'ready':all(baseline_health.values()),
                        'services':baseline_health,'output_rate':44100,'streaming_speech':False},
                        'qwen-omni':{'available':omni_provider is not None,'ready':omni_provider is not None and all(omni_health.values()),
                        'services':omni_health,'output_rate':24000,'streaming_speech':True,
                        'model':'Qwen/Qwen3-Omni-30B-A3B-Instruct','whole_reply_verification':False}}},
                "listening":{"mode":input_mode,'experimental':experimental is not None,
                             'input_asr':'Nemotron streaming 0.6B' if experimental is not None else 'Faster-Whisper base.en',
                             'turn_detector':'Smart Turn 3.2' if experimental is not None else 'VAD pause',
                             'chunk_ms':560 if experimental is not None else None},
                "model_inspection":True,
                "inspection_download":True,
                "character_presets":True, "archive_collections":True,
                "artwork_images":"dialogue-v2", "image_status":True,
                "archive":({"available":True,"scope":"library","limit":MAX_DATABASE_ENTRIES,
                            "selection_limit":50,"bulk_import":True} if isinstance(archive_store,ArchiveDatabase)
                           else {"available":True,"scope":"selected","limit":50}),
                "beat_entry_replies":True, "voice_retry":True, "voice_interruptions":True,
                "voice_verification":default_engine == 'baseline' and bool(getattr(services,'verify_voice',False)),
                "busy":call_lock.locked(), "stt_error":getattr(services.stt,"error",None),
                "profile":os.environ.get("LIVE_PROFILE","shared"),
                "models":{"actor":'Qwen/Qwen3-Omni-30B-A3B-Instruct' if default_engine == 'qwen-omni' else deployment['actor']['id'],
                          "director":(deployment['director'] or deployment['actor'])['id']},
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
        listener = None
        barge_in_timer = None
        listening_sequence = 0
        send_lock = asyncio.Lock()
        async def send_json(value):
            async with send_lock: await socket.send_json(value)
        async def send_bytes(value):
            async with send_lock: await socket.send_bytes(value)
        try:
            await socket.accept()
            baseline_preflight = None
            if omni_provider is None:
                # Preserve the original baseline's immediate readiness failure.
                baseline_preflight = await service_health()
                if not all(baseline_preflight.values()):
                    message = ('Experimental listening is not ready. Check the listening service log and local checkpoints.'
                        if experimental is not None and not (baseline_preflight.get('streaming_asr') and baseline_preflight.get('turn_detector'))
                        else 'Start speech recognition, the text model and Drama 3 first.')
                    await send_json({'type':'error','message':message}); return
            elif experimental is not None:
                listening_health = await experimental.health()
                if not (listening_health.get('ready') and listening_health.get('asr') and listening_health.get('turn')):
                    await send_json({'type':'error','message':'Experimental listening is not ready. Check the listening service log and local checkpoints.'}); return
            initial = await asyncio.wait_for(socket.receive_text(), timeout=15)
            if len(initial.encode("utf-8")) > MAX_START_BYTES: raise ValueError("Voice reference is too large.")
            config = json.loads(initial)
            if not isinstance(config, dict) or config.get("type") != "start":
                raise ValueError("Start a character call first.")
            # An older browser must never receive 24 kHz PCM using its fixed
            # 44.1 kHz player. Native streaming requires an explicit engine opt-in.
            selected_engine = engine_name(config.get('engine'))
            selected_services = omni_provider if selected_engine == 'qwen-omni' else services
            if selected_services is None:
                await send_json({'type':'error','message':'Start the streaming Qwen3-Omni stack before choosing this engine.'}); return
            health = baseline_preflight if selected_engine == 'baseline' and baseline_preflight is not None else await service_health(selected_services)
            if not all(health.values()):
                message = ('Experimental listening is not ready. Check the listening service log and local checkpoints.'
                           if experimental is not None and not (health.get('streaming_asr') and health.get('turn_detector'))
                           else 'Start Qwen3-Omni, speech recognition and the director first.' if selected_engine == 'qwen-omni'
                           else 'Start speech recognition, the text model and Drama 3 first.')
                await send_json({"type":"error", "message":message}); return
            interruptions = config.get('interruptions', False)
            if type(interruptions) is not bool: raise ValueError('Invalid interruption setting.')
            selected = Character.model_validate(config.get("character"))
            voice = VoiceSettings.model_validate(config.get("voice", {}))
            if selected_engine == 'qwen-omni':
                from .omni import native_voice
                try: native_voice(selected,voice)
                except ValueError as error:
                    await send_json({'type':'error','message':str(error)}); return
            trigger = EmotionalTrigger.model_validate(config["trigger"]) if config.get("trigger") is not None else None
            director = DirectorSettings.model_validate(config.get("director", {}))
            narrative = NarrativeSettings.model_validate(config.get("narrative", {}))
            archive = ArchiveSettings.model_validate(config.get('archive', {}))
            try:
                archive_entries = ()
                if archive.enabled:
                    async with archive_write_lock:
                        if archive.collection_ids:
                            archive_search = await _archive_job(collection_catalog.open_snapshot, archive.collection_ids,
                                                                 dispose=lambda snapshot:snapshot.close())
                        elif archive.scope == 'library':
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
            session = LiveSession(selected_services, selected, send_json, send_bytes, voice=voice, trigger=trigger, director=director,
                                  narrative=narrative, archive=archive if archive.enabled else None, archive_entries=archive_entries,
                                  archive_search=archive_search, image_lookup=museum_image_lookup)
            listen_after = 0.0
            accepted_barge_in_turn = None
            barge_in_reserved = False
            barge_in_epoch = 0
            def clear_barge_in_reservation():
                nonlocal barge_in_reserved, barge_in_timer, barge_in_epoch
                barge_in_reserved = False
                barge_in_epoch += 1
                if barge_in_timer is not None:
                    barge_in_timer.cancel()
                    barge_in_timer = None
            async def clear_microphone():
                nonlocal listening_sequence
                clear_barge_in_reservation()
                detector.pending.clear(); detector.reset()
                if listener is not None:
                    was_active = listener.input_active
                    await listener.reset()
                    if was_active:
                        listening_sequence += 1
                        await send_json({'type':'listening_state','phase':'idle','input_id':listening_sequence})
                session.input_active = False
            if experimental is not None:
                async def input_started():
                    nonlocal listening_sequence
                    clear_barge_in_reservation()
                    listening_sequence += 1
                    session.input_active = True
                    await send_json({'type':'speech_start'})
                    await send_json({'type':'listening_state','phase':'listening','input_id':listening_sequence})
                async def input_partial(text):
                    await send_json({'type':'listening_partial','text':text,'input_id':listening_sequence})
                async def input_endpoint(endpoint_ms, metrics):
                    await send_json({'type':'listening_state','phase':'finalizing','input_id':listening_sequence})
                async def input_final(pcm, text, endpoint_ms, metrics):
                    await session.begin(pcm=pcm if selected_engine == 'qwen-omni' else None,
                        text=text,endpoint_ms=endpoint_ms,recognition_metrics=metrics)
                async def input_error(message):
                    clear_barge_in_reservation()
                    session.input_active = False
                    await send_json({'type':'listening_state','phase':'error','input_id':listening_sequence,'message':message})
                listener = ListeningSession(experimental,detector.is_speech,on_start=input_started,
                    on_partial=input_partial,on_endpoint=input_endpoint,on_final=input_final,on_error=input_error)
            await send_json({"type":"ready", "sample_rate":16000, "output_rate":session.output_rate,
                             'engine':selected_engine,'streaming_speech':session.streaming_speech,
                             'voice_verification':not session.streaming_speech and bool(getattr(services,'verify_voice',False)),
                             "artwork_images":"dialogue-v2", "image_status":True,
                             "endpoint_ms":300 if experimental is not None else detector.silence_frames * 20,
                             "turn_taking":("Speak to interrupt; Stop reply remains available" if interruptions
                                            else "Stop reply to interrupt; mic resumes after playback"),
                             "interruptions":interruptions,
                             "listening_mode":input_mode,
                             "voice":native_voice(selected,voice) if selected_engine == 'qwen-omni' else
                                "uploaded reference" if voice.reference_audio else f"{voice.preset} reference" if voice.preset in ("female","male") else "first generated reply anchors this call"})
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
                        await clear_microphone()
                        continue
                    if listener is not None:
                        await listener.feed(packet)
                        # Barge-in reserves the floor before its buffered onset
                        # reaches the streaming listener's three VAD votes.
                        session.input_active = barge_in_reserved or listener.input_active
                        continue
                    for event, pcm, endpoint_ms in detector.feed(packet):
                        if event == "start":
                            clear_barge_in_reservation()
                            session.input_active = True
                            await send_json({"type":"speech_start"})
                        elif event == "prepare":
                            session.prepare_recognition(pcm)
                        elif event == "utterance":
                            await session.begin(pcm=pcm, endpoint_ms=endpoint_ms)
                            await clear_microphone()
                            break
                        elif event == "noise":
                            clear_barge_in_reservation()
                            session.input_active = False
                            await send_json({"type":"listening"})
                elif message.get("text") is not None:
                    if len(message["text"]) > 16000: raise ValueError("Control message too long.")
                    command = json.loads(message["text"])
                    if not isinstance(command, dict): raise ValueError("Invalid control message.")
                    if command.get("type") == "inspect":
                        await session.inspection.subscribe(command.get('enabled'))
                    elif command.get('type') == 'image_status':
                        # Browser rendering feedback updates the next prompt;
                        # it never starts speech or an additional model call.
                        session.image_status(command)
                    elif command.get('type') == 'interruptions':
                        enabled = command.get('enabled')
                        if type(enabled) is not bool: raise ValueError('Invalid interruption setting.')
                        interruptions = enabled
                        await send_json({'type':'interruptions_ack','enabled':interruptions})
                    elif command.get('type') == 'barge_in':
                        turn = command.get('turn_id')
                        if type(turn) is not int or not 0 < turn <= 0xffffffff:
                            raise ValueError('Invalid voice interruption.')
                        accepted = False
                        # The streaming recognizer and narrative clock can
                        # start turns independently of this receive loop.
                        async with session.begin_lock:
                            if (interruptions and not session.closed and turn == session.turn_id
                                    and turn != accepted_barge_in_turn and not session.input_active):
                                session.input_active = True
                                director_task = session.story_director_task
                                if director_task and not director_task.done():
                                    director_task.cancel()
                                    await asyncio.gather(director_task,return_exceptions=True)
                                await session.interrupt()
                                await session.prepared_recognition.clear()
                                await clear_microphone()
                                if not session.closed and turn == session.turn_id:
                                    # Admit the buffered first syllable immediately;
                                    # manual Stop reply retains its echo cooldown.
                                    session.input_active = True
                                    barge_in_reserved = True
                                    def expire_reservation(turn_id=turn, epoch=barge_in_epoch):
                                        nonlocal barge_in_reserved, barge_in_timer
                                        if (barge_in_reserved and epoch == barge_in_epoch
                                                and not session.closed and session.turn_id == turn_id):
                                            barge_in_reserved = False
                                            barge_in_timer = None
                                            if not detector.active and not (listener and listener.input_active):
                                                session.input_active = False
                                    barge_in_timer = asyncio.get_running_loop().call_later(2.0,expire_reservation)
                                    accepted_barge_in_turn = turn
                                    listen_after = 0.0
                                    accepted = True
                        await send_json({'type':'barge_in_ready','turn_id':turn,'accepted':accepted})
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
                            await clear_microphone()
                            # Allow loudspeaker audio to decay before accepting another utterance.
                            listen_after = time.monotonic() + 0.35
                            await session.story_boundary()
                    elif command.get("type") == "text":
                        text = command.get("text","")
                        if not isinstance(text,str) or not 0 < len(text.strip()) <= 2000: raise ValueError("Enter a short message.")
                        await clear_microphone()
                        await session.begin(text=text.strip())
                    elif command.get("type") == "mute":
                        await clear_microphone()
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
                        if control.action == 'cue': await clear_microphone()
                        await session.story_control(control.action)
                    elif command.get("type") == "trigger":
                        await clear_microphone()
                        await session.fire_trigger()
                    elif command.get("type") == "clear_trigger":
                        await session.trigger_state(False)
                    elif command.get("type") == "interrupt":
                        await session.interrupt()
                        await session.prepared_recognition.clear()
                        await clear_microphone()
                        listen_after = time.monotonic() + 0.35
                    elif command.get("type") == "stop": break
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass
        except ListeningError:
            try: await send_json({'type':'error','message':'The microphone could not be processed. Reconnect the call and try again.'})
            except RuntimeError: pass
        except ValidationError as exc:
            reason = exc.errors(include_input=False)[0]["msg"]
            try: await send_json({"type":"error", "message":"Check the call settings: " + reason})
            except RuntimeError: pass
        except (ValueError, json.JSONDecodeError):
            try: await send_json({"type":"error", "message":"Check the character fields and reconnect."})
            except RuntimeError: pass
        finally:
            # ASGI disconnect cancellation must not abandon the recognition
            # worker or leave the single-call lock held for the next visitor.
            with CancelScope(shield=True):
                try:
                    if barge_in_timer is not None: barge_in_timer.cancel()
                    try:
                        if listener is not None: await listener.close()
                    finally:
                        if session: await session.close()
                        elif archive_search is not None: await asyncio.to_thread(archive_search.close)
                finally:
                    call_lock.release()
                    try: await socket.close()
                    except RuntimeError: pass

    app.mount("/static",StaticFiles(directory=public),name="static")
    return app
