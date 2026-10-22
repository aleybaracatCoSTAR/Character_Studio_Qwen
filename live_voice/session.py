import asyncio
import logging
import struct
import time

from .providers import synthetic_reference
from .turns import PhraseChunker, ReplyHistory

logger = logging.getLogger(__name__)


class LiveSession:
    def __init__(self, providers, character, send_json, send_bytes):
        self.providers = providers
        self.character = character
        self.send_json = send_json
        self.send_bytes = send_bytes
        self.history = ReplyHistory()
        self.task = None
        self.turn_id = 0
        self.reference = None
        self.started = 0

    async def interrupt(self):
        old = self.turn_id
        was_active = self.history.current is not None or self.task is not None
        heard = self.history.interrupt()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if old and was_active:
            await self.send_json({"type":"interrupted", "turn_id":old, "heard":heard})

    async def begin(self, pcm=None, text=None, endpoint_ms=0):
        await self.interrupt()
        self.turn_id += 1
        turn_id = self.turn_id
        self.started = time.perf_counter() - endpoint_ms / 1000
        self.task = asyncio.create_task(self.run(turn_id, pcm, text, endpoint_ms))

    async def run(self, turn_id, pcm, text, endpoint_ms):
        started = self.started
        metrics = {"endpoint_ms":endpoint_ms}
        try:
            await self.send_json({"type":"turn_start", "turn_id":turn_id})
            now = time.perf_counter()
            if text is None: text = await self.providers.stt.transcribe(pcm)
            metrics["stt_ms"] = round((time.perf_counter() - now) * 1000)
            if not text or not text.strip():
                await self.send_json({"type":"empty_transcript", "turn_id":turn_id}); return
            self.history.begin(turn_id, text)
            await self.send_json({"type":"transcript", "turn_id":turn_id, "text":text})
            queue = asyncio.Queue(maxsize=2)
            chunker = PhraseChunker()
            answer = []
            llm_started = time.perf_counter()

            async def produce():
                async for delta in self.providers.text(self.character, self.history.prompt_messages()):
                    if not answer: metrics["llm_first_token_ms"] = round((time.perf_counter() - llm_started) * 1000)
                    answer.append(delta)
                    await self.send_json({"type":"text_delta", "turn_id":turn_id, "text":delta})
                    for phrase in chunker.feed(delta): await queue.put(phrase)
                for phrase in chunker.feed("", final=True): await queue.put(phrase)
                await queue.put(None)

            async def consume():
                sequence = 0
                while True:
                    phrase = await queue.get()
                    if phrase is None: break
                    self.history.add_phrase(sequence, phrase)
                    await self.send_json({"type":"phrase_start", "turn_id":turn_id, "seq":sequence, "text":phrase})
                    tts_started = time.perf_counter()
                    reference_audio = bytearray()
                    async for chunk in self.providers.audio(phrase, self.reference):
                        if "tts_first_chunk_ms" not in metrics:
                            metrics["tts_first_chunk_ms"] = round((time.perf_counter() - tts_started) * 1000)
                            metrics["speech_end_to_first_audio_ms"] = round((time.perf_counter() - started) * 1000)
                            await self.send_json({"type":"metrics", "turn_id":turn_id, **metrics})
                        if self.reference is None: reference_audio.extend(chunk)
                        await self.send_bytes(struct.pack("!II", turn_id, sequence) + chunk)
                    # Use the first generated phrase as the synthetic voice reference for this call.
                    if self.reference is None and reference_audio:
                        self.reference = synthetic_reference(bytes(reference_audio), phrase)
                    await self.send_json({"type":"phrase_done", "turn_id":turn_id, "seq":sequence})
                    sequence += 1

            tasks = [asyncio.create_task(produce()), asyncio.create_task(consume())]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done(): task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            if not answer: raise RuntimeError("The text model returned no spoken reply.")
            await self.send_json({"type":"response_done", "turn_id":turn_id, "text":"".join(answer)})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Keep dialogue, character data and captured audio out of logs.
            logger.warning("Live turn failed (%s); inspect the local model service logs.", type(exc).__name__)
            await self.send_json({"type":"error", "turn_id":turn_id,
                                  "message":"Voice generation failed. Check the LLM and Drama 3 server logs."})

    async def close(self):
        if self.task:
            self.task.cancel(); await asyncio.gather(self.task, return_exceptions=True)
