"""No checkpoint, GPU or model imports: bounded real-time listening contracts."""
import asyncio
from contextlib import nullcontext
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace, GeneratorType
import unittest
from unittest.mock import patch

import httpx
import numpy as np
from fastapi.testclient import TestClient

from listening_runtime.app import ListeningRuntime, create_app, _owned
from listening_runtime.nemotron import (PCMStream, ChunkGeometry, ListeningError,
    NemotronRecognizer, verify_pin, MODEL_ID, MODEL_REVISION, MAX_AUDIO_BYTES, MAX_QUEUED_BYTES)
from listening_runtime.turn import SmartTurn

GEOMETRY = ChunkGeometry(640, 800, 4, 4, 160, 320)
PACKET = b'\x01\0' * 640


class FakeRecognizer:
    def __init__(self):
        self.ready = False
        self.processed = []
        self.cancelled = threading.Event()

    def load(self): self.ready = True
    def close(self): self.ready = False
    def transcribe(self, audio, partial):
        words = []
        try:
            for pcm, initial in audio.windows(GEOMETRY):
                self.processed.append((pcm, initial))
                words.append('first' if initial else 'next')
                partial(' '.join(words))
            return ' '.join(words)
        except ListeningError:
            self.cancelled.set()
            raise


class FakeTurn:
    def __init__(self): self.ready = False; self.calls = []
    def load(self): self.ready = True
    def close(self): self.ready = False
    def predict(self, pcm):
        self.calls.append(pcm)
        return {'complete':True,'probability':.8,'prediction':1}


class PCMStreamTests(unittest.TestCase):
    def test_final_short_audio_and_last_partial_window_are_padded_not_dropped(self):
        audio = PCMStream()
        audio.feed(b'\x02\0' * 1200); audio.finish()
        windows = list(audio.windows(GEOMETRY))
        self.assertEqual([initial for _, initial in windows], [True, False])
        self.assertEqual([len(pcm) for pcm, _ in windows], [1280, 1600])
        self.assertEqual(windows[0][0], b'\x02\0' * 640)
        self.assertEqual(windows[1][0], b'\x02\0' * 720 + b'\0' * 160)
        empty = PCMStream(); empty.finish()
        self.assertEqual(list(empty.windows(GEOMETRY)), [])

    def test_fixed_packet_audio_and_queue_bounds(self):
        for packet in (b'', b'x' * 639, b'x' * 641, b'x' * 2562):
            with self.subTest(size=len(packet)), self.assertRaises(ListeningError): PCMStream().feed(packet)
        PCMStream().feed(b'\0' * 1278)  # A real resampler need not emit multiples of 20 ms.
        queued = PCMStream()
        for _ in range(MAX_QUEUED_BYTES // 640): queued.feed(b'\0' * 640)
        with self.assertRaisesRegex(ListeningError, 'faster'): queued.feed(b'\0' * 640)
        full = PCMStream()
        with patch('listening_runtime.nemotron.MAX_QUEUED_BYTES', MAX_AUDIO_BYTES):
            for _ in range(MAX_AUDIO_BYTES // 640): full.feed(b'\0' * 640)
            with self.assertRaisesRegex(ListeningError, '15 seconds'): full.feed(b'\0' * 640)

    def test_cancel_unblocks_a_waiting_generator_and_clear_removes_audio(self):
        audio = PCMStream(); audio.feed(b'\0' * 640)
        errors = []
        def consume():
            try: list(audio.windows(GEOMETRY))
            except ListeningError as error: errors.append(error.code)
        thread = threading.Thread(target=consume); thread.start()
        audio.cancel(); thread.join(1)
        self.assertFalse(thread.is_alive()); self.assertEqual(errors, ['cancelled'])
        audio.clear(); self.assertEqual(audio._audio, b'')

    def test_idle_timeout_and_repeated_finish_are_explicit(self):
        audio = PCMStream(idle_timeout=.001)
        with self.assertRaisesRegex(ListeningError, 'timed out'): next(audio.windows(GEOMETRY))
        audio.finish()
        with self.assertRaises(ListeningError): audio.finish()
        with self.assertRaises(ListeningError): audio.feed(PACKET)

    def test_offline_pin_requires_exact_identity_revision_and_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            with self.assertRaises(ListeningError): verify_pin(directory, MODEL_ID, MODEL_REVISION)
            marker = directory / 'listening-checkpoint.json'
            marker.write_text(json.dumps({'schema':1,'model_id':MODEL_ID,'revision':MODEL_REVISION}))
            verify_pin(directory, MODEL_ID, MODEL_REVISION)
            with self.assertRaises(ListeningError): verify_pin(directory, MODEL_ID, 'wrong')


class SmartTurnTests(unittest.TestCase):
    def model(self, probability=.8):
        model = SmartTurn('not-loaded.onnx'); captured = {}
        def extractor(samples, **kwargs):
            captured['samples'] = samples.copy(); captured['kwargs'] = kwargs
            return SimpleNamespace(input_features=np.zeros((1,80,800), dtype=np.float32))
        class Session:
            def run(self, output_names, inputs):
                captured['inputs'] = inputs
                return [np.array([[probability]],dtype=np.float32)]
        model.extractor, model.session = extractor, Session()
        return model, captured

    def test_short_audio_is_left_padded_and_inference_is_finite_cpu_format(self):
        model, captured = self.model()
        result = model.predict(b'\x01\0' * 320)
        samples = captured['samples']
        self.assertEqual(samples.shape, (128000,))
        self.assertTrue(np.all(samples[:-320] == 0)); self.assertTrue(np.all(samples[-320:] > 0))
        self.assertEqual(captured['kwargs']['sampling_rate'], 16000)
        self.assertTrue(captured['kwargs']['do_normalize'])
        self.assertEqual(captured['inputs']['input_features'].shape, (1,80,800))
        self.assertTrue(result['complete']); self.assertAlmostEqual(result['probability'], .8, places=6)

    def test_long_audio_uses_only_last_eight_seconds(self):
        model, captured = self.model()
        pcm = b'\x01\0' * 32000 + b'\x02\0' * 128000
        model.predict(pcm)
        self.assertTrue(np.all(captured['samples'] == 2/32768))

    def test_invalid_probability_audio_and_exact_threshold_are_rejected_or_false(self):
        for probability in (float('nan'), float('inf'), -.1, 1.1):
            model, _ = self.model(probability)
            with self.subTest(probability=probability), self.assertRaises(ListeningError): model.predict(b'\0\0')
        model, _ = self.model(.5)
        self.assertFalse(model.predict(b'\0\0')['complete'])
        for pcm in (b'', b'x', b'\0' * (MAX_AUDIO_BYTES + 2)):
            with self.subTest(size=len(pcm)), self.assertRaises(ListeningError): model.predict(pcm)


class NemotronAdapterTests(unittest.TestCase):
    def test_actual_adapter_uses_generator_cache_api_and_preserves_padded_final_input(self):
        calls, partials, arguments = [], [], {}
        class Features:
            def __getitem__(self, item): return self
            def to(self, **kwargs): return self
        class Tokens:
            def __init__(self, values): self.values = values
            def tolist(self): return self.values
        class Processor:
            tokenizer = SimpleNamespace(decode=lambda ids, **kwargs:' '.join(str(i) for i in ids))
            def decode(self, ids, **kwargs): return ' '.join(str(i) for i in ids)
            def __call__(self, samples, **kwargs):
                calls.append((samples.copy(), kwargs))
                return SimpleNamespace(input_features=Features())
            def batch_decode(self, values, **kwargs): return ['final words']
        class Model:
            device, dtype = 'fake-device', 'fake-dtype'
            def generate(self, **kwargs):
                arguments.update(kwargs)
                if not isinstance(kwargs['input_features'], GeneratorType): raise AssertionError('Not a real generator')
                for index, features in enumerate(kwargs['input_features'], 1):
                    kwargs['streamer'].put(Tokens([index]))
                kwargs['streamer'].end()
                return SimpleNamespace(sequences=[[1, 2]])
        recognizer = NemotronRecognizer('never-loaded')
        recognizer.model, recognizer.processor, recognizer.geometry = Model(), Processor(), GEOMETRY
        audio = PCMStream(); audio.feed(b'\x02\0' * 1200); audio.finish()
        with patch.dict(sys.modules, {'torch':SimpleNamespace(inference_mode=nullcontext)}):
            text = recognizer.transcribe(audio, partials.append)
        self.assertEqual(text, 'final words')
        self.assertEqual(partials, ['1', '1 2'])
        self.assertEqual(arguments['num_lookahead_tokens'], 6)
        self.assertEqual(arguments['max_new_tokens'], 4096)
        self.assertFalse(arguments['do_sample'])
        self.assertEqual([call[1]['is_first_audio_chunk'] for call in calls], [True, False])
        self.assertEqual([len(call[0]) for call in calls], [640, 800])
        self.assertTrue(np.all(calls[-1][0][-80:] == 0))

    def test_real_560ms_processor_geometry_flushes_short_tail(self):
        geometry = ChunkGeometry(7880, 9360, 49, 56, 160, 512)
        audio = PCMStream()
        remaining = 10000 * 2
        while remaining:
            size = min(2560, remaining)
            audio.feed(b'\x01\0' * (size//2)); remaining -= size
        audio.finish()
        windows = list(audio.windows(geometry))
        self.assertEqual([len(pcm)//2 for pcm, initial in windows], [7880, 9360])
        # The next STFT window begins at49*160-512/2=7584samples;
        # every remaining real sample survives before zero padding.
        self.assertEqual(windows[1][0], b'\x01\0' * (10000-7584) + b'\0' * ((9360-(10000-7584))*2))


class RuntimeProtocolTests(unittest.TestCase):
    def setUp(self):
        self.asr, self.turn = FakeRecognizer(), FakeTurn()
        self.runtime = ListeningRuntime(self.asr, self.turn)
        self.client_context = TestClient(create_app(self.runtime))
        self.client = self.client_context.__enter__()
        self.addCleanup(self.client_context.__exit__, None, None, None)

    def test_readiness_requires_loaded_models_and_partials_arrive_before_finish(self):
        self.assertTrue(self.client.get('/health').json()['ready'])
        with self.client.websocket_connect('/stream') as socket:
            socket.send_json({'type':'start','chunk_ms':560})
            self.assertEqual(socket.receive_json()['type'], 'ready')
            socket.send_bytes(PACKET)
            partial = socket.receive_json()
            self.assertEqual(partial, {'type':'partial','text':'first'})
            self.assertEqual(len(self.asr.processed), 1)
            socket.send_bytes(PACKET)
            self.assertEqual(socket.receive_json(), {'type':'partial','text':'first next'})
            socket.send_json({'type':'finish'})
            self.assertEqual(socket.receive_json(), {'type':'final','text':'first next'})

    def test_empty_finish_and_next_utterance_have_no_cross_call_cache(self):
        for packet, expected in ((None,''),(PACKET,'first')):
            with self.client.websocket_connect('/stream') as socket:
                socket.send_json({'type':'start'}); socket.receive_json()
                if packet:
                    socket.send_bytes(packet); self.assertEqual(socket.receive_json()['text'], 'first')
                socket.send_json({'type':'finish'})
                self.assertEqual(socket.receive_json(), {'type':'final','text':expected})

    def test_cpu_turn_prediction_works_while_asr_waits_for_more_audio(self):
        with self.client.websocket_connect('/stream') as socket:
            socket.send_json({'type':'start'}); socket.receive_json()
            response = self.client.post('/turn',headers={'Content-Type':'application/octet-stream'},content=PACKET)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()['complete'])
            socket.send_json({'type':'cancel'})
        self.assertTrue(self.asr.cancelled.wait(1))

    def test_busy_invalid_start_and_packet_errors_are_safe_and_cleanup(self):
        with self.client.websocket_connect('/stream') as first:
            first.send_json({'type':'start'}); first.receive_json()
            with self.client.websocket_connect('/stream') as second:
                second.send_json({'type':'start'})
                self.assertEqual(second.receive_json()['code'], 'busy')
            first.send_json({'type':'cancel'})
        with self.client.websocket_connect('/stream') as socket:
            socket.send_json({'type':'start','chunk_ms':80})
            self.assertEqual(socket.receive_json()['code'], 'invalid_start')
        with self.client.websocket_connect('/stream') as socket:
            socket.send_json({'type':'start'}); socket.receive_json(); socket.send_bytes(b'PRIVATE')
            response = socket.receive_json()
            self.assertEqual(response['code'], 'invalid_packet')
            self.assertNotIn('PRIVATE', json.dumps(response))

    def test_http_audio_bounds_and_not_ready_health(self):
        for content, headers, expected in ((b'',{'Content-Type':'application/octet-stream'},400),
                (b'x',{'Content-Type':'application/octet-stream'},400),
                (PACKET,{'Content-Type':'text/plain'},400),
                (b'\0' * (MAX_AUDIO_BYTES+2),{'Content-Type':'application/octet-stream'},413)):
            with self.subTest(size=len(content)):
                self.assertEqual(self.client.post('/turn',headers=headers,content=content).status_code, expected)
        self.turn.ready = False
        response = self.client.get('/health')
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()['ready'])
        with self.client.websocket_connect('/stream') as socket:
            self.assertEqual(socket.receive_json()['code'], 'not_ready')

    def test_idle_timeout_cancels_generator_and_does_not_leak_audio(self):
        with patch('listening_runtime.app.IDLE_TIMEOUT', .02):
            with self.client.websocket_connect('/stream') as socket:
                socket.send_json({'type':'start'}); socket.receive_json()
                self.assertEqual(socket.receive_json()['code'], 'timeout')
        self.assertTrue(self.asr.cancelled.wait(1))

    def test_recognition_failure_has_fixed_client_and_log_diagnostics(self):
        def fail(audio, partial): raise ValueError('PRIVATE_TRANSCRIPT')
        self.asr.transcribe = fail
        with self.assertLogs('listening_runtime.app', level='WARNING') as logs:
            with self.client.websocket_connect('/stream') as socket:
                socket.send_json({'type':'start'}); socket.receive_json()
                response = socket.receive_json()
        self.assertEqual(response['code'], 'inference_failed')
        self.assertNotIn('PRIVATE_TRANSCRIPT', json.dumps(response) + '\n'.join(logs.output))


class OwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def ready(self, event):
        for _ in range(1000):
            if event.is_set(): return
            await asyncio.sleep(.001)
        self.fail('Worker did not reach its boundary.')

    async def test_repeated_cancel_retains_worker_until_cleanup_and_does_not_queue_new_asr(self):
        entered, released = threading.Event(), threading.Event()
        class SlowCancel(FakeRecognizer):
            def transcribe(self, audio, partial):
                try: return super().transcribe(audio, partial)
                finally: entered.set(); released.wait(3)
        engine, turn = SlowCancel(), FakeTurn()
        runtime = ListeningRuntime(engine, turn); await runtime.load()
        utterance = runtime.start(lambda text:None)
        task = asyncio.create_task(runtime.release(utterance))
        await self.ready(entered)
        task.cancel(); await asyncio.sleep(.005); task.cancel(); await asyncio.sleep(.005)
        self.assertIs(runtime.active, utterance)
        with self.assertRaisesRegex(ListeningError, 'active'): runtime.start(lambda text:None)
        released.set(); await task
        self.assertIsNone(runtime.active); self.assertEqual(utterance.audio._audio, b'')
        await runtime.close()

    async def test_owned_worker_error_after_cancel_keeps_cancellation(self):
        entered, released = threading.Event(), threading.Event()
        def work():
            entered.set(); released.wait(3); raise ValueError('PRIVATE_ERROR')
        worker = asyncio.get_running_loop().run_in_executor(None, work)
        task = asyncio.create_task(_owned(worker)); await asyncio.sleep(0); await self.ready(entered)
        task.cancel(); await asyncio.sleep(.005); self.assertFalse(task.done())
        released.set()
        with self.assertRaises(asyncio.CancelledError): await task

    async def test_loading_failure_never_reports_ready_or_logs_raw_exception(self):
        class Failing(FakeRecognizer):
            def load(self): raise ValueError('PRIVATE_CHECKPOINT')
        runtime = ListeningRuntime(Failing(), FakeTurn())
        with self.assertLogs('listening_runtime.app', level='ERROR') as logs:
            await runtime.load()
        self.assertFalse(runtime.health()['ready'])
        self.assertEqual(runtime.health()['error'], 'asr_load_failed')
        self.assertNotIn('PRIVATE_CHECKPOINT', '\n'.join(logs.output))
        await runtime.close()

    async def test_failed_startup_exits_instead_of_serving_a_false_healthy_port(self):
        class Failing(FakeRecognizer):
            def load(self): raise ValueError('PRIVATE_CHECKPOINT')
        runtime = ListeningRuntime(Failing(), FakeTurn())
        with self.assertLogs('listening_runtime.app', level='ERROR'):
            with self.assertRaisesRegex(RuntimeError, 'failed to load') as caught:
                with TestClient(create_app(runtime)):
                    self.fail('An unready service entered its running lifespan.')
        self.assertNotIn('PRIVATE_CHECKPOINT', str(caught.exception))
        self.assertTrue(runtime.closed)


if __name__ == '__main__': unittest.main()
