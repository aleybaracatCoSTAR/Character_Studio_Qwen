"""Optional local streaming recognition and semantic turn detection.

This client loads no models. Recognition receives each PCM frame once, while
the independent endpoint request examines bounded snapshots of the same turn.
"""
import asyncio
from collections import deque
from dataclasses import dataclass
import inspect
import json
import math
import time
from urllib.parse import urlsplit

import httpx


RATE = 16000
FRAME_BYTES = 640
MAX_PCM_BYTES = RATE * 2 * 15
MAX_TEXT_CHARS = 4000


class ListeningError(RuntimeError):
    def __init__(self, message, *, reason='listening_unavailable'):
        super().__init__(message)
        self.reason = reason


async def _emit(callback, *args):
    if callback is None: return
    value = callback(*args)
    if inspect.isawaitable(value): await value


def _connect_without_redirects(*args, **kwargs):
    from websockets.asyncio.client import connect
    class LocalConnect(connect):
        def process_redirect(self, error): return error
    return LocalConnect(*args, **kwargs)


def _pcm(value):
    if not isinstance(value, (bytes, bytearray, memoryview)) or not 0 < len(value) <= MAX_PCM_BYTES or len(value) % 2:
        raise ListeningError('The microphone audio was not a valid short recording.', reason='invalid_audio')
    return bytes(value)


@dataclass(frozen=True)
class TurnDecision:
    probability: float
    complete: bool


class ListeningClient:
    """HTTP and WebSocket client for the isolated, loopback-only service."""
    def __init__(self, base_url='http://127.0.0.1:18095', *, transport=None,
                 connect_factory=None, turn_timeout=5, finish_timeout=30):
        url = urlsplit(base_url)
        if (url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1')
                or url.username or url.password or url.query or url.fragment or url.path not in ('', '/')):
            raise ValueError('Use the local listening service address.')
        self.base_url = base_url.rstrip('/')
        self.ws_url = 'ws' + self.base_url[4:] + '/stream'
        self.connect_factory = connect_factory
        self.turn_timeout = max(.05, min(5, float(turn_timeout)))
        self.finish_timeout = max(.05, min(30, float(finish_timeout)))
        self.http = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(5, connect=2),
                                      follow_redirects=False, trust_env=False)

    async def health(self):
        try:
            response = await self.http.get(self.base_url + '/health', timeout=2)
            response.raise_for_status(); data = response.json()
            asr = data.get('asr') is True; turn = data.get('turn') is True
            return {'asr':asr, 'turn':turn, 'ready':asr and turn and data.get('ready') is True}
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            return {'asr':False, 'turn':False, 'ready':False}

    async def classify(self, pcm):
        pcm = _pcm(pcm)
        try:
            response = await self.http.post(self.base_url + '/turn', content=pcm,
                headers={'Content-Type':'application/octet-stream'}, timeout=self.turn_timeout)
            response.raise_for_status(); value = response.json()
            probability, complete = value.get('probability'), value.get('complete')
            if complete is None and type(value.get('prediction')) is int and value['prediction'] in (0, 1):
                complete = bool(value['prediction'])
            if (type(probability) not in (int, float) or not math.isfinite(probability)
                    or not 0 <= probability <= 1 or type(complete) is not bool):
                raise ValueError('Invalid endpoint decision.')
            return TurnDecision(float(probability), complete)
        except httpx.TimeoutException:
            raise ListeningError('The turn detector did not answer in time.', reason='endpoint_timeout') from None
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            raise ListeningError('The turn detector is unavailable. Please try again.', reason='endpoint_unavailable') from None

    async def open_stream(self, on_partial=None):
        factory = self.connect_factory or _connect_without_redirects
        try:
            socket = await factory(self.ws_url, open_timeout=3, close_timeout=1,
                                   max_size=32768, compression=None, proxy=None)
        except asyncio.CancelledError: raise
        except Exception:
            raise ListeningError('Streaming speech recognition is unavailable. Please try again.') from None
        stream = RecognitionStream(socket, on_partial=on_partial, finish_timeout=self.finish_timeout)
        try:
            await stream.start(); return stream
        except asyncio.CancelledError:
            await stream.close(); raise
        except Exception:
            await stream.close()
            raise ListeningError('Streaming speech recognition did not become ready. Please try again.') from None

    async def close(self): await self.http.aclose()


class RecognitionStream:
    def __init__(self, socket, *, on_partial=None, finish_timeout=30):
        self.socket=socket; self.on_partial=on_partial; self.finish_timeout=finish_timeout
        self.total=0; self.finished=False; self.closed=False; self.reader=None
        loop=asyncio.get_running_loop(); self.ready=loop.create_future(); self.final=loop.create_future()
        # A cancelled turn may never await these futures. Consume failures so
        # loop diagnostics cannot retain a service response or abandoned error.
        for future in (self.ready,self.final):
            future.add_done_callback(lambda item:None if item.cancelled() else item.exception())

    async def start(self):
        self.reader=asyncio.create_task(self._read())
        await self.socket.send(json.dumps({'type':'start','chunk_ms':560}))
        try: await asyncio.wait_for(asyncio.shield(self.ready),3)
        except asyncio.TimeoutError:
            raise ListeningError('Streaming speech recognition did not become ready.', reason='recognition_timeout') from None

    async def _read(self):
        try:
            async for packet in self.socket:
                if not isinstance(packet,str) or len(packet)>32768: raise ValueError('Invalid recognition event.')
                event=json.loads(packet)
                if not isinstance(event,dict): raise ValueError('Invalid recognition event.')
                kind=event.get('type')
                if kind=='ready':
                    if self.ready.done() or event.get('sample_rate')!=RATE or event.get('chunk_ms')!=560:
                        raise ValueError('Invalid recognition readiness.')
                    self.ready.set_result(True)
                elif kind in ('partial','final'):
                    text=event.get('text')
                    if not self.ready.done() or not isinstance(text,str) or len(text)>MAX_TEXT_CHARS:
                        raise ValueError('Invalid recognition text.')
                    if kind=='partial': await _emit(self.on_partial,text)
                    else:
                        if not self.finished or self.final.done(): raise ValueError('Unexpected recognition completion.')
                        self.final.set_result(text.strip()); return
                else: raise ValueError('Recognition service rejected the turn.')
            if not self.final.done(): raise ValueError('Recognition ended before completion.')
        except asyncio.CancelledError: raise
        except Exception:
            error=ListeningError('Streaming speech recognition did not finish. Please try again.',reason='recognition_unavailable')
            for future in (self.ready,self.final):
                if not future.done(): future.set_exception(error)

    async def send(self, pcm):
        pcm=_pcm(pcm)
        if self.closed or self.finished or len(pcm)%FRAME_BYTES or self.total+len(pcm)>MAX_PCM_BYTES:
            raise ListeningError('The streaming microphone turn exceeded its limit.',reason='invalid_audio')
        self.total+=len(pcm)
        # The 200 ms preroll is split to the service's 20–80 ms wire range.
        for offset in range(0,len(pcm),2560): await self.socket.send(pcm[offset:offset+2560])
        if self.final.done(): await self.final

    async def finish(self):
        if self.closed: raise ListeningError('The streaming microphone turn has ended.',reason='recognition_unavailable')
        if not self.finished:
            self.finished=True; await self.socket.send(json.dumps({'type':'finish'}))
        try: return await asyncio.wait_for(asyncio.shield(self.final),self.finish_timeout)
        except asyncio.TimeoutError:
            raise ListeningError('Streaming speech recognition did not finish in time. Please try again.',reason='recognition_timeout') from None

    async def close(self):
        if self.closed:return
        self.closed=True
        try:
            if not self.final.done(): await asyncio.wait_for(self.socket.send(json.dumps({'type':'cancel'})),1)
        except Exception: pass
        if self.reader and self.reader is not asyncio.current_task():
            self.reader.cancel(); await asyncio.gather(self.reader,return_exceptions=True)
        for future in (self.ready,self.final):
            if not future.done(): future.cancel()
        try: await asyncio.wait_for(self.socket.close(),1.5)
        except Exception: pass


class StreamingTurnDetector:
    """20 ms VAD votes and 200 ms preroll; silence alone does not end a turn."""
    def __init__(self, is_speech, max_seconds=15):
        self.is_speech=is_speech; self.max_frames=max(50,min(750,int(max_seconds*50)))
        self.reset()

    def reset(self):
        self.pending=bytearray(); self.preroll=deque(maxlen=10); self.votes=deque(maxlen=5)
        self.frames=[]; self.active=False; self.voiced=0; self.silent=0; self.revision=0; self.ended=False

    @property
    def silence_ms(self): return self.silent*20
    def audio(self): return b''.join(self.frames)

    def feed(self, packet):
        if not isinstance(packet,(bytes,bytearray,memoryview)) or len(packet)>8192 or len(packet)%2:
            raise ListeningError('The microphone audio packet was invalid.',reason='invalid_audio')
        if self.ended:return []
        self.pending.extend(packet); events=[]
        while len(self.pending)>=FRAME_BYTES:
            frame=bytes(self.pending[:FRAME_BYTES]); del self.pending[:FRAME_BYTES]
            speech=bool(self.is_speech(frame))
            if not self.active:
                self.preroll.append(frame); self.votes.append(speech)
                if sum(self.votes)>=3:
                    self.active=True; self.frames=list(self.preroll); self.voiced=sum(self.votes)
                    self.revision+=1; events.append(('start',self.audio()))
                continue
            self.frames.append(frame); self.voiced+=int(speech)
            if speech:
                if self.silent: events.append(('resume',None))
                self.silent=0; self.revision+=1
            else:self.silent+=1
            events.append(('audio',frame))
            if len(self.frames)>=self.max_frames:
                self.ended=True; events.append(('limit',None)); self.pending.clear(); break
        return events


class ListeningSession:
    """Non-blocking microphone ingestion with one ASR stream per utterance.

    Callbacks may be synchronous or async. on_error receives only fixed safe
    text; on_endpoint and on_final receive bounded numeric/enum timing metadata.
    The owner closes the shared ListeningClient separately.
    """
    def __init__(self, client, is_speech, *, on_start=None, on_partial=None,
                 on_endpoint=None, on_final=None, on_error=None, complete_threshold=.7,
                 min_silence_ms=300, max_silence_ms=1200, recheck_ms=200, clock=time.monotonic):
        self.client=client; self.detector=StreamingTurnDetector(is_speech)
        self.on_start=on_start; self.on_partial=on_partial; self.on_endpoint=on_endpoint
        self.on_final=on_final; self.on_error=on_error; self.clock=clock
        self.complete_threshold=max(.5,min(1,float(complete_threshold)))
        self.min_silence_ms=max(300,int(min_silence_ms)); self.max_silence_ms=max(self.min_silence_ms,min(1200,int(max_silence_ms)))
        self.recheck_ms=max(200,int(recheck_ms)); self.task=None; self._check_task=None; self._notice_task=None
        self._epoch=0; self._input_active=False; self._finalizing=False; self.closed=False
        self._queue=None; self._next_check=self.min_silence_ms; self._decision=None; self._timed_out=False

    @property
    def input_active(self): return self._input_active

    async def feed(self, packet):
        if self.closed or self._finalizing:return False
        try: events=self.detector.feed(packet)
        except ListeningError: raise
        except Exception:
            raise ListeningError('The microphone could not be checked for speech.',reason='vad_unavailable') from None
        for kind,pcm in events:
            if kind=='start':
                self._epoch+=1; self._input_active=True; self._queue=asyncio.Queue(maxsize=751)
                self._decision=None; self._timed_out=False; self._next_check=self.min_silence_ms
                self.task=asyncio.create_task(self._run(self._epoch,self._queue))
            if kind in ('start','audio'): self._queue.put_nowait(pcm)
            elif kind=='resume': self._next_check=self.min_silence_ms; self._timed_out=False; self._decision=None
            elif kind=='limit': self._finalize('max_duration')
        if self.detector.active and not self._finalizing:
            if self.detector.silence_ms>=self.max_silence_ms:
                self._finalize('endpoint_timeout' if self._timed_out else 'pause_limit')
            else:self._schedule_check()
        return True

    def _schedule_check(self):
        if (self._finalizing or not self.detector.active or self.detector.silence_ms<self._next_check
                or self._check_task and not self._check_task.done()): return
        self._next_check=self.detector.silence_ms+self.recheck_ms
        self._check_task=asyncio.create_task(self._check(self._epoch,self.detector.revision,self.detector.audio()))

    async def _check(self, epoch, revision, pcm):
        try:
            decision=await self.client.classify(pcm)
            if epoch!=self._epoch or self._finalizing or revision!=self.detector.revision:return
            self._decision=decision
            if decision.complete and decision.probability>=self.complete_threshold:self._finalize('smart_turn')
        except asyncio.CancelledError: raise
        except ListeningError as error:
            if epoch!=self._epoch or revision!=self.detector.revision or self._finalizing:return
            if error.reason=='endpoint_timeout':self._timed_out=True
            else:await self._fail(epoch,'The turn detector is unavailable. Please try again.')
        except Exception:
            await self._fail(epoch,'The turn detector is unavailable. Please try again.')
        finally:
            if self._check_task is asyncio.current_task():self._check_task=None
            if epoch==self._epoch and not self._finalizing:self._schedule_check()

    def _finalize(self, reason):
        if self._finalizing:return
        self._finalizing=True
        if self._check_task and self._check_task is not asyncio.current_task():self._check_task.cancel()
        probability=self._decision.probability if self._decision else None
        self._metrics={'streaming_asr':True,'stream_finalize_ms':0,'endpoint_reason':reason,
                       'turn_probability':probability,'endpoint_ms':self.detector.silence_ms}
        self._notice_task=asyncio.create_task(_emit(self.on_endpoint,self.detector.silence_ms,dict(self._metrics)))
        self._queue.put_nowait(None)

    async def _run(self, epoch, queue):
        stream=None
        try:
            await _emit(self.on_start)
            if epoch!=self._epoch or self.closed:return
            async def partial(text):
                if epoch==self._epoch and not self.closed:await _emit(self.on_partial,text)
            stream=await self.client.open_stream(on_partial=partial)
            while True:
                pcm=await queue.get()
                if pcm is None:break
                await stream.send(pcm)
            if self._notice_task:await self._notice_task
            if self.detector.voiced<5:raise ListeningError('No clear speech was heard. Please try again.',reason='no_speech')
            started=self.clock(); text=await stream.finish()
            if epoch!=self._epoch or self.closed:return
            if not isinstance(text,str) or not text.strip() or len(text)>MAX_TEXT_CHARS:
                raise ListeningError('No clear speech was recognized. Please try again.',reason='no_speech')
            metrics={**self._metrics,'stream_finalize_ms':max(0,round((self.clock()-started)*1000))}
            self._input_active=False
            await _emit(self.on_final,self.detector.audio(),text.strip(),metrics['endpoint_ms'],metrics)
        except asyncio.CancelledError: raise
        except ListeningError as error:
            await self._fail(epoch,'No clear speech was recognized. Please try again.' if error.reason=='no_speech'
                             else 'Streaming speech recognition did not finish. Please try again.')
        except Exception:await self._fail(epoch,'Streaming speech recognition did not finish. Please try again.')
        finally:
            if stream:await stream.close()
            if epoch==self._epoch:
                self.detector.reset();self._input_active=False;self._finalizing=False;self._queue=None

    async def _fail(self, epoch, message):
        if epoch!=self._epoch or self.closed:return
        await self.reset()
        await _emit(self.on_error,message)

    async def reset(self):
        self._epoch+=1;self._input_active=False;self._finalizing=False;self.detector.reset();self._queue=None
        current=asyncio.current_task(); tasks={task for task in (self.task,self._check_task,self._notice_task) if task and task is not current}
        for task in tasks:task.cancel()
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)
        self.task=None;self._check_task=None;self._notice_task=None

    async def cancel(self):await self.reset()
    async def close(self):self.closed=True;await self.reset()
