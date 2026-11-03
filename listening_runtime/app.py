"""Loopback-only optional listening service, with bounded worker ownership."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import time

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .nemotron import (CHUNK_MS, MAX_AUDIO_BYTES, MAX_TEXT, MODEL_ID, MODEL_REVISION,
                       SAMPLE_RATE, ListeningError, NemotronRecognizer, PCMStream)
from .turn import SmartTurn

logger = logging.getLogger(__name__)
IDLE_TIMEOUT = 20
FINISH_TIMEOUT = 30
TURN_TIMEOUT = 5


async def _owned(future):
    """A canceled caller retains the worker until its real completion."""
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError as cancelled:
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        try:
            future.result()
        except BaseException:
            pass
        raise cancelled


@dataclass
class Utterance:
    audio: PCMStream
    worker: asyncio.Future


class ListeningRuntime:
    def __init__(self, recognizer, turn):
        self.recognizer, self.turn = recognizer, turn
        self._asr_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='nemotron-asr')
        self._turn_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='smart-turn-cpu')
        self.active = None
        self.turn_busy = False
        self._turn_future = None
        self.error = None
        self.closed = False

    @property
    def ready(self):
        return not self.closed and self.recognizer.ready and self.turn.ready and self.error is None

    async def load(self):
        loop = asyncio.get_running_loop()
        for engine, pool, code in ((self.recognizer, self._asr_pool, 'asr_load_failed'),
                                   (self.turn, self._turn_pool, 'turn_load_failed')):
            try:
                await _owned(loop.run_in_executor(pool, engine.load))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = code
                logger.error('Listening startup failed: %s (%s).', code, type(exc).__name__)
                return

    def health(self):
        return {'ready': self.ready, 'asr': bool(self.recognizer.ready), 'turn': bool(self.turn.ready),
                'busy': self.active is not None, 'chunk_ms': CHUNK_MS, 'sample_rate': SAMPLE_RATE,
                'model': MODEL_ID, 'revision': MODEL_REVISION, 'error': self.error}

    def start(self, partial):
        if not self.ready:
            raise ListeningError('not_ready')
        if self.active is not None:
            raise ListeningError('busy')
        audio = PCMStream(idle_timeout=IDLE_TIMEOUT)
        def run():
            try:
                text = self.recognizer.transcribe(audio, partial)
                if not isinstance(text, str) or len(text) > MAX_TEXT:
                    raise ListeningError('transcript_limit')
                return text.strip()
            except ListeningError:
                raise
            except Exception as exc:
                logger.warning('Listening recognition failed (%s).', type(exc).__name__)
                raise ListeningError('inference_failed') from None
        worker = asyncio.get_running_loop().run_in_executor(self._asr_pool, run)
        utterance = Utterance(audio, worker)
        self.active = utterance
        return utterance

    async def release(self, utterance):
        utterance.audio.cancel()
        try:
            await _owned(utterance.worker)
        except BaseException:
            pass
        finally:
            utterance.audio.clear()
            if self.active is utterance:
                self.active = None

    async def predict_turn(self, pcm):
        if not self.ready:
            raise ListeningError('not_ready')
        if self.turn_busy:
            raise ListeningError('busy')
        self.turn_busy = True
        try:
            self._turn_future = asyncio.get_running_loop().run_in_executor(self._turn_pool, self.turn.predict, pcm)
            return await _owned(self._turn_future)
        except ListeningError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning('Turn completion check failed (%s).', type(exc).__name__)
            raise ListeningError('turn_failed') from None
        finally:
            self.turn_busy = False
            self._turn_future = None

    async def close(self):
        self.closed = True
        if self.active is not None:
            await self.release(self.active)
        if self._turn_future is not None:
            try: await _owned(self._turn_future)
            except BaseException: pass
        # Lifespan shutdown follows endpoint completion; no queued model calls
        # are admitted. Never leave a canceled ASR generator holding its cache.
        self._asr_pool.shutdown(wait=False, cancel_futures=True)
        self._turn_pool.shutdown(wait=False, cancel_futures=True)
        self.recognizer.close()
        self.turn.close()


def default_runtime():
    store = Path(os.environ.get('LISTENING_STORE') or
                 str(Path(os.environ.get('LIVE_VOICE_STORE', str(Path.home() / '.local/share/character-studio'))) / 'listening'))
    recognizer = NemotronRecognizer(os.environ.get('LISTENING_MODEL_DIR', str(store / 'nemotron')),
                                   os.environ.get('LISTENING_DEVICE', 'cuda:0'))
    turn = SmartTurn(os.environ.get('LISTENING_TURN_MODEL', str(store / 'smart-turn/smart-turn-v3.2-cpu.onnx')))
    return ListeningRuntime(recognizer, turn)


def _command(message):
    if not isinstance(message, str) or len(message) > 1024:
        raise ListeningError('invalid_control')
    try:
        value = json.loads(message)
    except (ValueError, TypeError):
        raise ListeningError('invalid_control') from None
    if not isinstance(value, dict):
        raise ListeningError('invalid_control')
    return value


def create_app(runtime=None):
    runtime = runtime or default_runtime()

    @asynccontextmanager
    async def lifespan(app):
        try:
            await runtime.load()
            if not runtime.ready:
                raise RuntimeError('The isolated listening checkpoints failed to load. Check the listening setup and local downloads.')
            yield
        finally:
            await runtime.close()

    app = FastAPI(title='Character Studio · Listening experiment', lifespan=lifespan)
    app.state.listening = runtime

    @app.get('/health')
    def health():
        return JSONResponse(runtime.health(), status_code=200 if runtime.ready else 503)

    @app.post('/turn')
    async def turn_prediction(request: Request):
        if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/octet-stream':
            raise HTTPException(400, detail={'code':'invalid_turn_audio','message':ListeningError.MESSAGES['invalid_turn_audio']})
        body = bytearray()
        async for packet in request.stream():
            body.extend(packet)
            if len(body) > MAX_AUDIO_BYTES:
                raise HTTPException(413, detail={'code':'audio_limit','message':ListeningError.MESSAGES['audio_limit']})
        if not body or len(body) % 2:
            raise HTTPException(400, detail={'code':'invalid_turn_audio','message':ListeningError.MESSAGES['invalid_turn_audio']})
        try:
            return await asyncio.wait_for(runtime.predict_turn(bytes(body)), TURN_TIMEOUT)
        except asyncio.TimeoutError:
            raise HTTPException(504, detail={'code':'timeout','message':ListeningError.MESSAGES['timeout']}) from None
        except ListeningError as exc:
            status = 409 if exc.code == 'busy' else 503
            raise HTTPException(status, detail={'code':exc.code,'message':str(exc)}) from None

    @app.websocket('/stream')
    async def stream(socket: WebSocket):
        await socket.accept()
        utterance = receive_task = partial_task = None
        live = True
        try:
            if not runtime.ready:
                raise ListeningError('not_ready')
            origin = socket.headers.get('origin')
            expected = str(socket.base_url).replace('ws://', 'http://').replace('wss://', 'https://').rstrip('/')
            if origin is not None and origin != expected:
                raise ListeningError('invalid_start')
            first = await asyncio.wait_for(socket.receive(), 5)
            if first['type'] == 'websocket.disconnect':
                return
            command = _command(first.get('text'))
            if (set(command) - {'type', 'chunk_ms'} or command.get('type') != 'start'
                    or command.get('chunk_ms', CHUNK_MS) != CHUNK_MS):
                raise ListeningError('invalid_start')
            loop = asyncio.get_running_loop()
            partials = asyncio.Queue(maxsize=8)
            def enqueue(text):
                if not live or not isinstance(text, str) or len(text) > MAX_TEXT:
                    return
                if partials.full():
                    partials.get_nowait()
                partials.put_nowait(text)
            def partial(text):
                loop.call_soon_threadsafe(enqueue, text)
            utterance = runtime.start(partial)
            await socket.send_json({'type':'ready','chunk_ms':CHUNK_MS,'sample_rate':SAMPLE_RATE})
            receive_task = asyncio.create_task(socket.receive())
            partial_task = asyncio.create_task(partials.get())
            finished = False
            deadline = time.monotonic() + IDLE_TIMEOUT
            while True:
                done, _ = await asyncio.wait((receive_task, partial_task, utterance.worker),
                                             timeout=max(0, deadline-time.monotonic()),
                                             return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    raise ListeningError('timeout')
                if utterance.worker in done:
                    text = utterance.worker.result()
                    if not finished:
                        raise ListeningError('inference_failed')
                    await socket.send_json({'type':'final','text':text})
                    return
                if partial_task in done:
                    await socket.send_json({'type':'partial','text':partial_task.result()})
                    partial_task = asyncio.create_task(partials.get())
                if receive_task in done:
                    message = receive_task.result()
                    if message['type'] == 'websocket.disconnect':
                        return
                    if message.get('bytes') is not None:
                        if finished:
                            raise ListeningError('invalid_control')
                        utterance.audio.feed(message['bytes'])
                        deadline = time.monotonic() + IDLE_TIMEOUT
                    else:
                        control = _command(message.get('text'))
                        if set(control) != {'type'}:
                            raise ListeningError('invalid_control')
                        if control['type'] == 'cancel':
                            return
                        if control['type'] != 'finish' or finished:
                            raise ListeningError('invalid_control')
                        utterance.audio.finish()
                        finished = True
                        deadline = time.monotonic() + FINISH_TIMEOUT
                    receive_task = asyncio.create_task(socket.receive())
        except WebSocketDisconnect:
            # Transport closure contains no reusable transcript or model state.
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc if isinstance(exc, ListeningError) else ListeningError('timeout' if isinstance(exc, asyncio.TimeoutError) else 'inference_failed')
            try:
                await socket.send_json({'type':'error','code':error.code,'message':str(error)})
            except Exception:
                pass
        finally:
            live = False
            for task in (receive_task, partial_task):
                if task is not None:
                    task.cancel()
            await asyncio.gather(*(task for task in (receive_task, partial_task) if task is not None), return_exceptions=True)
            if utterance is not None:
                await runtime.release(utterance)
            try:
                await socket.close()
            except (RuntimeError, WebSocketDisconnect):
                pass

    return app
