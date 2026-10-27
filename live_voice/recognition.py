"""Prepare ASR during a pause without treating provisional speech as a turn.

Only an exact match with the final PCM can reuse a result. The caller still
decides when an utterance has ended and when its text enters the conversation.
All data and tasks belong to one call; nothing is logged or persisted here.
"""
import asyncio
from collections.abc import Awaitable, Callable


MAX_PCM_BYTES = 15 * 16000 * 2


class PreparedRecognition:
    def __init__(self, transcribe: Callable[[bytes], Awaitable[str]]):
        self._transcribe = transcribe
        self._draft_pcm = None
        self._draft_task = None
        self._active_task = None
        self._closed = False
        self.last_reused = False

    @staticmethod
    def _pcm(value):
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise ValueError('Recognition needs PCM bytes.')
        size = value.nbytes if isinstance(value, memoryview) else len(value)
        if not size or size > MAX_PCM_BYTES or size % 2:
            raise ValueError('Use at most 15 seconds of aligned 16 kHz mono PCM16.')
        return bytes(value)

    @staticmethod
    def _observe(task):
        # A provisional utterance can be abandoned before anyone awaits it.
        # Observe errors without logging its audio or text. Awaiting later still
        # returns the same result or raises the same exception.
        if not task.cancelled(): task.exception()

    def _start(self, pcm):
        async def run():
            return await self._transcribe(pcm)
        task = asyncio.create_task(run())
        task.add_done_callback(self._observe)
        return task

    def prepare(self, pcm):
        """Start one provisional recognition; return whether a new job began."""
        if self._closed: return False
        pcm = self._pcm(pcm)
        if self._active_task is not None and not self._active_task.done():
            return False
        if self._draft_task is not None:
            if self._draft_pcm == pcm or not self._draft_task.done():
                return False
        self._draft_pcm = pcm
        self._draft_task = self._start(pcm)
        return True

    async def _discard_draft(self):
        task = self._draft_task
        self._draft_task = self._draft_pcm = None
        if task is not None:
            if not task.done(): task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def transcribe(self, pcm):
        """Resolve final PCM, reusing only a successful exact prepared match."""
        self.last_reused = False
        if self._closed: raise RuntimeError('Recognition for this call is closed.')
        pcm = self._pcm(pcm)
        if self._active_task is not None and not self._active_task.done():
            raise RuntimeError('Recognition is already resolving an utterance.')

        reused = self._draft_task is not None and self._draft_pcm == pcm
        if reused:
            task = self._draft_task
            self._draft_task = self._draft_pcm = None
        else:
            await self._discard_draft()
            if self._closed: raise RuntimeError('Recognition for this call is closed.')
            task = self._start(pcm)
        self._active_task = task
        try:
            try:
                text = await task
            except asyncio.CancelledError:
                raise
            except Exception:
                if not reused: raise
                # Preparation is an optimization. Its failure must not prevent
                # one normal recognition of the now-finalized utterance.
                if self._closed: raise
                reused = False
                task = self._active_task = self._start(pcm)
                text = await task
            self.last_reused = reused
            return text
        finally:
            if self._active_task is task: self._active_task = None

    async def clear(self):
        """Discard provisional/active work; the helper can be used again."""
        tasks = {task for task in (self._draft_task, self._active_task) if task is not None}
        self._draft_task = self._draft_pcm = self._active_task = None
        for task in tasks:
            if not task.done(): task.cancel()
        if tasks: await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self):
        """End this call and prevent new work from being scheduled."""
        self._closed = True
        await self.clear()
