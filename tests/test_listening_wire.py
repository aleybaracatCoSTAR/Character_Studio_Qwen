"""Real loopback WebSockets/HTTP with fake models; never invokes a GPU."""
import asyncio
import socket
import unittest

import uvicorn

from listening_runtime.app import ListeningRuntime, create_app
from listening_runtime.nemotron import ChunkGeometry
from live_voice.listening import ListeningClient


class WireRecognizer:
    ready = False

    def load(self): self.ready = True
    def close(self): self.ready = False
    def transcribe(self, audio, partial):
        words = []
        for pcm, initial in audio.windows(ChunkGeometry(640, 800, 4, 4, 160, 320)):
            words.append('hello' if initial else 'again')
            partial(' '.join(words))
        return ' '.join(words)


class WireTurn:
    ready = False

    def load(self): self.ready = True
    def close(self): self.ready = False
    def predict(self, pcm):
        return {'complete':True, 'probability':.875, 'prediction':1}


class ListeningWireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.socket.bind(('127.0.0.1', 0))
        except PermissionError:
            self.socket.close()
            self.skipTest('This environment denies local listening sockets.')
        self.socket.listen(128)
        self.socket.setblocking(False)
        self.runtime = ListeningRuntime(WireRecognizer(), WireTurn())
        config = uvicorn.Config(create_app(self.runtime), host='127.0.0.1', port=0,
                                log_level='error', access_log=False, lifespan='on', ws='websockets')
        self.server = uvicorn.Server(config)
        self.server_task = asyncio.create_task(self.server.serve(sockets=[self.socket]))
        for _ in range(2000):
            if self.server.started: break
            if self.server_task.done(): await self.server_task
            await asyncio.sleep(.001)
        self.assertTrue(self.server.started, 'The local fake-model service did not become ready.')
        self.client = ListeningClient(f'http://127.0.0.1:{self.socket.getsockname()[1]}')

    async def asyncTearDown(self):
        if hasattr(self, 'client'): await self.client.close()
        if hasattr(self, 'server'):
            self.server.should_exit = True
            try: await asyncio.wait_for(self.server_task, 5)
            finally: self.socket.close()

    async def test_real_client_streams_partials_classifies_concurrently_and_resets_for_next_utterance(self):
        self.assertEqual(await self.client.health(), {'asr':True,'turn':True,'ready':True})
        for expected in ('hello again', 'hello'):
            partials, observed = [], asyncio.Event()
            async def on_partial(text):
                partials.append(text)
                observed.set()
            stream = await self.client.open_stream(on_partial)
            try:
                # The ASR generator is waiting for audio, but Smart Turn has an
                # independent CPU worker and must answer before ASR finishes.
                decision = await self.client.classify(b'\0' * 640)
                self.assertTrue(decision.complete)
                self.assertEqual(decision.probability, .875)
                await stream.send(b'\x01\0' * 640)
                await asyncio.wait_for(observed.wait(), 1)
                self.assertEqual(partials, ['hello'])
                self.assertFalse(stream.finished)
                if expected == 'hello again':
                    observed.clear()
                    await stream.send(b'\x01\0' * 640)
                    await asyncio.wait_for(observed.wait(), 1)
                    self.assertEqual(partials[-1], 'hello again')
                self.assertEqual(await stream.finish(), expected)
            finally:
                await stream.close()
            for _ in range(1000):
                if self.runtime.active is None: break
                await asyncio.sleep(.001)
            self.assertIsNone(self.runtime.active)


if __name__ == '__main__': unittest.main()
