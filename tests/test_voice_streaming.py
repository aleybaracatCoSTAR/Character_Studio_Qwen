"""Streaming playback forwards a fixed take before EOS without replaying it."""
import asyncio
import json
import unittest

import httpx

from live_voice.providers import (LocalProviders, VoiceTakeIncomplete,
    VoiceProtocolError, VoiceServiceUnavailable)
from test_voice_generation import (PLAIN, STYLED, REFERENCE, DummySTT,
    delta, done, event, response)


PCM = b'\x19\x00' * 4410


class GatedSpeech(httpx.AsyncByteStream):
    def __init__(self):
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        self.started.set()
        yield delta(PCM)
        await self.finish.wait()
        yield delta(b'\x23\x00') + done()

    async def aclose(self):
        self.closed = True


class Recognition(DummySTT):
    def __init__(self, result=PLAIN):
        self.called = asyncio.Event()
        self.finish = asyncio.Event()
        self.result = result
        self.inputs = []

    async def transcribe(self, pcm):
        self.inputs.append(pcm)
        self.called.set()
        await self.finish.wait()
        return self.result


class VoiceStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_pcm_arrives_before_done_and_before_post_generation_recognition(self):
        stream, stt = GatedSpeech(), Recognition()
        requests, heard = [], []
        first = asyncio.Event()
        def serve(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, headers={'content-type':'text/event-stream'}, stream=stream)
        provider = LocalProviders(stt, transport=httpx.MockTransport(serve),
                                  voice_playback='streaming', verify_voice=True)
        async def consume():
            async for part in provider.audio(STYLED, list(REFERENCE)):
                heard.append(part)
                first.set()
        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(first.wait(), 1)
            self.assertFalse(stream.closed)
            self.assertFalse(stt.called.is_set())
            self.assertEqual(b''.join(heard), PCM)
            self.assertFalse(task.done())
            stream.finish.set()
            await asyncio.wait_for(stt.called.wait(), 1)
            self.assertEqual(b''.join(heard), PCM + b'\x23\x00')
            self.assertFalse(task.done())
            stt.finish.set()
            await asyncio.wait_for(task, 1)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]['input'], STYLED)
            self.assertEqual((requests[0]['ref_audio'], requests[0]['ref_text']), REFERENCE)
            self.assertEqual(requests[0]['stream_format'], 'sse')
            self.assertTrue(provider.last_voice_check['audio_delivered'])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await provider.close()

    async def test_misaligned_network_events_keep_every_sample_in_bounded_packets(self):
        pcm = bytes(range(256)) * 80 + b'\x19\x20'
        audio = response(delta(pcm[:1]) + delta(pcm[1:7]) + delta(pcm[7:]) + done(), widths=(65536,))
        provider = LocalProviders(DummySTT(), voice_playback='streaming',
            transport=httpx.MockTransport(lambda request: audio))
        try:
            chunks = [part async for part in provider.audio(STYLED, REFERENCE)]
            self.assertEqual(b''.join(chunks), pcm)
            self.assertTrue(all(len(part) <= 16384 and len(part) % 2 == 0 for part in chunks))
            self.assertTrue(audio.is_closed)
        finally:
            await provider.close()

    async def test_cap_missing_done_duplicate_event_and_service_error_never_replay_after_pcm(self):
        for kind in ('cap', 'missing_done', 'duplicate', 'error'):
            requests, heard, notices = [], [], []
            def serve(request):
                payload = json.loads(request.content)
                requests.append(payload)
                suffix = {
                    'cap':done(payload['max_new_tokens']),
                    'missing_done':b'',
                    'duplicate':done()+done(),
                    'error':event('speech.audio.error', error={'message':'PRIVATE details'}),
                }[kind]
                return response(delta(PCM) + suffix, widths=(65536,))
            async def notice(message): notices.append(message)
            provider = LocalProviders(DummySTT(), voice_playback='streaming',
                transport=httpx.MockTransport(serve))
            try:
                with self.subTest(kind=kind), self.assertLogs('live_voice.providers', 'WARNING') as logs:
                    with self.assertRaises(VoiceTakeIncomplete) as raised:
                        async for part in provider.audio(STYLED, REFERENCE, on_notice=notice):
                            heard.append(part)
                    self.assertEqual(b''.join(heard), PCM)
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(notices, [])
                    self.assertTrue(raised.exception.metadata['audio_delivered'])
                    self.assertEqual(raised.exception.metadata['attempts'], 1)
                    self.assertNotIn('PRIVATE', '\n'.join(logs.output))
                    self.assertNotIn(PLAIN, '\n'.join(logs.output))
            finally:
                await provider.close()

    async def test_post_generation_mismatch_is_reported_after_audio_without_automatic_retry(self):
        stt, requests, heard = Recognition('Completely unrelated invented words.'), [], []
        stt.finish.set()
        def serve(request):
            requests.append(json.loads(request.content))
            return response(delta(PCM) + done(), widths=(65536,))
        provider = LocalProviders(stt, verify_voice=True, voice_playback='streaming',
                                  transport=httpx.MockTransport(serve))
        try:
            with self.assertLogs('live_voice.providers', 'WARNING'):
                with self.assertRaises(VoiceTakeIncomplete) as raised:
                    async for part in provider.audio(STYLED, REFERENCE): heard.append(part)
            self.assertEqual(raised.exception.reason, 'speech_mismatch')
            self.assertTrue(raised.exception.metadata['audio_delivered'])
            self.assertEqual(len(requests), 1)
            self.assertEqual(b''.join(heard), PCM)
        finally:
            await provider.close()

    async def test_transient_failure_before_any_pcm_retries_once_with_same_style_and_frozen_reference(self):
        reference, requests = list(REFERENCE), []
        def serve(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                return httpx.Response(503)
            return response(delta(PCM) + done(), widths=(65536,))
        async def notice(message): reference[:] = ['PRIVATE replacement', 'Changed reference']
        provider = LocalProviders(DummySTT(), voice_playback='streaming',
                                  transport=httpx.MockTransport(serve))
        try:
            with self.assertLogs('live_voice.providers', 'WARNING'):
                heard = b''.join([part async for part in provider.audio(STYLED, reference, on_notice=notice)])
            self.assertEqual(heard, PCM)
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[0], requests[1])
            self.assertEqual((requests[1]['ref_audio'], requests[1]['ref_text']), REFERENCE)
        finally:
            await provider.close()

    async def test_transport_drop_after_pcm_surfaces_without_replay(self):
        requests, heard = [], []
        def serve(request):
            requests.append(request)
            return response(delta(PCM), widths=(65536,), fail=httpx.ReadError('PRIVATE network details'))
        provider = LocalProviders(DummySTT(), voice_playback='streaming',
                                  transport=httpx.MockTransport(serve))
        try:
            with self.assertLogs('live_voice.providers', 'WARNING') as logs:
                with self.assertRaises(VoiceServiceUnavailable) as raised:
                    async for part in provider.audio(STYLED, REFERENCE): heard.append(part)
            self.assertEqual(raised.exception.reason, 'transport_network')
            self.assertTrue(raised.exception.metadata['audio_delivered'])
            self.assertEqual(len(requests), 1)
            self.assertEqual(b''.join(heard), PCM)
            self.assertNotIn('PRIVATE', '\n'.join(logs.output))
        finally:
            await provider.close()

    async def test_invalid_pcm_rate_and_wav_headers_never_reach_playback_or_retry(self):
        for payload in (delta(sample_rate=24000) + done(),
                        delta(b'RI') + delta(b'FF012345') + done()):
            requests, heard = [], []
            def serve(request):
                requests.append(request)
                return response(payload, widths=(65536,))
            provider = LocalProviders(DummySTT(), voice_playback='streaming',
                                      transport=httpx.MockTransport(serve))
            try:
                with self.assertLogs('live_voice.providers', 'WARNING'):
                    with self.assertRaises(VoiceProtocolError):
                        async for part in provider.audio(STYLED, REFERENCE): heard.append(part)
                self.assertEqual(heard, [])
                self.assertEqual(len(requests), 1)
            finally:
                await provider.close()

    async def test_cancelling_playback_closes_upstream_without_a_second_request(self):
        stream, requests, heard = GatedSpeech(), [], []
        first = asyncio.Event()
        def serve(request):
            requests.append(request)
            return httpx.Response(200, headers={'content-type':'text/event-stream'}, stream=stream)
        provider = LocalProviders(DummySTT(), voice_playback='streaming',
                                  transport=httpx.MockTransport(serve))
        async def consume():
            async for part in provider.audio(STYLED, REFERENCE):
                heard.append(part)
                first.set()
        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(first.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            self.assertTrue(stream.closed)
            self.assertEqual(len(requests), 1)
            self.assertEqual(b''.join(heard), PCM)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await provider.close()

    async def test_invalid_playback_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            LocalProviders(DummySTT(), voice_playback='unchecked')

    async def test_closing_consumer_after_first_packet_releases_backpressured_producer(self):
        requests = []
        # More packets than the bounded queue can hold without a consumer.
        audio = response(delta(PCM * 40) + done(), widths=(65536,))
        def serve(request):
            requests.append(request)
            return audio
        provider = LocalProviders(DummySTT(), voice_playback='streaming',
                                  transport=httpx.MockTransport(serve))
        stream = provider.audio(STYLED, REFERENCE)
        try:
            first = await asyncio.wait_for(anext(stream), 1)
            self.assertEqual(first, (PCM * 40)[:16384])
            await asyncio.wait_for(stream.aclose(), 1)
            self.assertTrue(audio.is_closed)
            self.assertEqual(len(requests), 1)
        finally:
            await stream.aclose()
            await provider.close()
