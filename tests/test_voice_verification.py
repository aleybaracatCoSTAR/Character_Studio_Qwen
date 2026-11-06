"""Confirmed SSE audio must also cover the requested words before playback.

PCM and ASR are explicit test doubles. These checks do not establish model
accuracy, spoken quality or performance on a GPU.
"""
import asyncio
import json
import os
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from live_voice.app import create_app
from live_voice.providers import LocalProviders, VoiceProtocolError, VoiceTakeIncomplete
from test_voice_generation import REFERENCE, delta, done, response


FULL_TEXT = ('That’s what I keep telling myself. But it still hurts—like I’m breaking a part of me every time I hang up. '
             'Do you think it’s okay to want both? To love them and still be me?')
FIRST_SENTENCE = 'That’s what I keep telling myself.'
STYLED = '<|speaker:0|> [calm, restrained, low voice] ' + FULL_TEXT
# Deliberately synthetic, distinguishable PCM; no real voice recording is used.
FIRST_PCM = b'\x11\x00' * 4410
SECOND_PCM = b'\x22\x00' * 8820


class DummySTT:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.closed = False

    async def transcribe(self, pcm):
        self.calls.append(pcm)
        if not self.replies: raise AssertionError('Unexpected ASR call.')
        result = self.replies.pop(0)
        if isinstance(result, BaseException): raise result
        return result

    def close(self): self.closed = True


def fixture(replies, takes=(FIRST_PCM, SECOND_PCM), **settings):
    stt = DummySTT(replies)
    requests = []
    def serve(request):
        requests.append(json.loads(request.content))
        pcm = takes[min(len(requests) - 1, len(takes) - 1)]
        return response(delta(pcm) + done(), widths=(65536,))
    providers = LocalProviders(stt, transport=httpx.MockTransport(serve), verify_voice=True, **settings)
    return providers, stt, requests


class VoiceVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_completed_first_take_is_discarded_then_identical_full_take_alone_is_released(self):
        providers, stt, requests = fixture([FIRST_SENTENCE, FULL_TEXT])
        notices = []
        async def notice(message): notices.append(message)
        try:
            with self.assertLogs('live_voice.providers', level='WARNING') as logs:
                chunks = [chunk async for chunk in providers.audio(STYLED, REFERENCE, on_notice=notice)]
            self.assertEqual(b''.join(chunks), SECOND_PCM)
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[0], requests[1])
            self.assertEqual(requests[0]['input'], STYLED)
            self.assertEqual((requests[0]['ref_audio'], requests[0]['ref_text']), REFERENCE)
            self.assertEqual(len(stt.calls), 2)
            # 4,410 / 44,100 and 8,820 / 44,100 seconds become 16 kHz PCM16.
            self.assertEqual([len(pcm) for pcm in stt.calls], [1600 * 2, 3200 * 2])
            self.assertEqual(len(notices), 1)
            self.assertIn('[speech_mismatch]', notices[0])
            self.assertTrue(notices[0].endswith('Retrying the whole reply with the same emotion and voice.'))
            check = providers.last_voice_check
            self.assertGreater(check['expected_words'], 30)
            self.assertEqual(check['expected_words'], check['matched_words'])
            self.assertEqual(check['coverage'], 1)
            self.assertNotIn(FIRST_SENTENCE, '\n'.join(logs.output) + notices[0])
            self.assertNotIn(FULL_TEXT, '\n'.join(logs.output) + notices[0])
        finally: await providers.close()

    async def test_two_completed_but_short_takes_release_no_audio_and_report_numeric_coverage_only(self):
        providers, stt, requests = fixture([FIRST_SENTENCE, FIRST_SENTENCE])
        yielded = []; notices = []
        async def notice(message): notices.append(message)
        try:
            with self.assertLogs('live_voice.providers', level='WARNING') as logs:
                with self.assertRaises(VoiceTakeIncomplete) as raised:
                    async for chunk in providers.audio(STYLED, REFERENCE, on_notice=notice): yielded.append(chunk)
            diagnostic = raised.exception.diagnostic()
            self.assertEqual(yielded, [])
            self.assertEqual(len(requests), 2); self.assertEqual(len(stt.calls), 2)
            self.assertEqual(requests[0], requests[1]); self.assertEqual(len(notices), 1)
            self.assertEqual(diagnostic['reason'], 'speech_mismatch')
            self.assertEqual(diagnostic['attempts'], 2)
            self.assertLess(diagnostic['heard_words'], diagnostic['expected_words'])
            self.assertLess(diagnostic['matched_words'], diagnostic['expected_words'])
            self.assertLess(diagnostic['coverage'], .5)
            for key, value in diagnostic.items():
                if key != 'reason': self.assertIsInstance(value, (int, float, bool))
            visible = json.dumps(diagnostic) + '\n'.join(logs.output) + ''.join(notices) + str(raised.exception)
            for private in (FULL_TEXT, FIRST_SENTENCE, 'But it still hurts', REFERENCE[0], REFERENCE[1], 'calm, restrained, low voice'):
                self.assertNotIn(private, visible)
        finally: await providers.close()

    async def test_normal_complete_reply_is_verified_once_before_any_pcm_is_released(self):
        providers, stt, requests = fixture([FULL_TEXT], takes=(SECOND_PCM,))
        notices = []
        async def notice(message): notices.append(message)
        try:
            chunks = []
            async for chunk in providers.audio(STYLED, on_notice=notice):
                self.assertEqual(len(stt.calls), 1)
                self.assertEqual(providers.last_voice_check['coverage'], 1)
                chunks.append(chunk)
            self.assertEqual(b''.join(chunks), SECOND_PCM)
            self.assertEqual(len(requests), 1); self.assertEqual(notices, [])
        finally: await providers.close()

    async def test_unchecked_pcm_cannot_escape_while_recognition_is_pending(self):
        providers, stt, requests = fixture([])
        started, release = asyncio.Event(), asyncio.Event()
        async def recognize(pcm):
            stt.calls.append(pcm); started.set(); await release.wait(); return FULL_TEXT
        stt.transcribe = recognize
        yielded = []
        async def consume():
            async for chunk in providers.audio(STYLED): yielded.append(chunk)
        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(started.wait(), 1)
            self.assertEqual(yielded, []); self.assertEqual(len(requests), 1)
            release.set(); await asyncio.wait_for(task, 1)
            self.assertEqual(b''.join(yielded), FIRST_PCM)
        finally:
            task.cancel(); await asyncio.gather(task, return_exceptions=True); await providers.close()

    async def test_asr_failure_is_nonretryable_and_never_exposes_private_exception_or_pcm(self):
        secret = 'PRIVATE ASR FAILURE WITH TRANSCRIPT AND HOST'
        providers, stt, requests = fixture([RuntimeError(secret)])
        yielded = []; notices = []
        async def notice(message): notices.append(message)
        try:
            with self.assertLogs('live_voice.providers', level='WARNING') as logs:
                with self.assertRaises(VoiceProtocolError) as raised:
                    async for chunk in providers.audio(STYLED, on_notice=notice): yielded.append(chunk)
            self.assertEqual(raised.exception.reason, 'verification_unavailable')
            self.assertEqual(yielded, []); self.assertEqual(len(requests), 1); self.assertEqual(len(stt.calls), 1)
            self.assertEqual(notices, []); self.assertIsNone(providers.last_voice_check)
            visible = '\n'.join(logs.output) + json.dumps(raised.exception.diagnostic()) + str(raised.exception)
            for private in (secret, FULL_TEXT, FIRST_SENTENCE): self.assertNotIn(private, visible)
        finally: await providers.close()

    async def test_verification_timeout_releases_no_unchecked_audio_and_does_not_generate_again(self):
        providers, stt, requests = fixture([], verification_timeout=2)
        # Tighten only the test object's deadline to exercise real cancellation
        # without adding the production minimum of two seconds to this suite.
        providers.verification_timeout = .02
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def recognize(pcm):
            stt.calls.append(pcm); started.set()
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        stt.transcribe = recognize
        yielded = []
        try:
            with self.assertLogs('live_voice.providers', level='WARNING'):
                with self.assertRaises(VoiceProtocolError) as raised:
                    async for chunk in providers.audio(STYLED): yielded.append(chunk)
            self.assertEqual(raised.exception.reason, 'verification_unavailable')
            self.assertTrue(started.is_set()); self.assertTrue(cancelled.is_set())
            self.assertEqual(yielded, []); self.assertEqual(len(requests), 1)
            self.assertEqual(raised.exception.diagnostic()['attempt'], 1)
        finally: await providers.close()

    async def test_cancel_during_verification_cannot_retry_send_a_notice_or_release_audio(self):
        providers, stt, requests = fixture([])
        started, cancelled = asyncio.Event(), asyncio.Event(); yielded = []; notices = []
        async def recognize(pcm):
            stt.calls.append(pcm); started.set()
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        stt.transcribe = recognize
        async def notice(message): notices.append(message)
        async def consume():
            async for chunk in providers.audio(STYLED, on_notice=notice): yielded.append(chunk)
        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(started.wait(), 1); task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            self.assertTrue(cancelled.is_set()); self.assertEqual(len(requests), 1)
            self.assertEqual(yielded, []); self.assertEqual(notices, [])
        finally:
            task.cancel(); await asyncio.gather(task, return_exceptions=True); await providers.close()

    async def test_explicit_opt_out_keeps_protocol_checks_but_never_calls_asr(self):
        stt = DummySTT([AssertionError('ASR must not run in explicit protocol-only mode.')]); requests = []
        def serve(request): requests.append(json.loads(request.content)); return response(delta(SECOND_PCM) + done(), widths=(65536,))
        providers = LocalProviders(stt, transport=httpx.MockTransport(serve), verify_voice=False)
        try:
            yielded = b''.join([chunk async for chunk in providers.audio(STYLED)])
            self.assertEqual(yielded, SECOND_PCM); self.assertEqual(stt.calls, []); self.assertEqual(len(requests), 1)
            self.assertIsNone(providers.last_voice_check)
        finally: await providers.close()

    async def test_incomplete_protocol_take_is_discarded_without_running_word_verification(self):
        stt = DummySTT([FULL_TEXT]); requests = []; notices = []
        def serve(request):
            requests.append(json.loads(request.content))
            payload = delta(FIRST_PCM) if len(requests) == 1 else delta(SECOND_PCM) + done()
            return response(payload, widths=(65536,))
        async def notice(message): notices.append(message)
        providers = LocalProviders(stt, transport=httpx.MockTransport(serve), verify_voice=True)
        try:
            with self.assertLogs('live_voice.providers', level='WARNING'):
                yielded = b''.join([chunk async for chunk in providers.audio(STYLED, on_notice=notice)])
            self.assertEqual(yielded, SECOND_PCM); self.assertEqual(len(requests), 2); self.assertEqual(len(stt.calls), 1)
            self.assertIn('[missing_done]', notices[0])
        finally: await providers.close()


class VoiceVerificationAppTests(unittest.TestCase):
    def test_checked_preview_reports_verification_enabled_without_loading_or_contacting_models(self):
        with patch.dict(os.environ, {'LIVE_VERIFY_VOICE':'1','LIVE_VOICE_PLAYBACK':'checked'}), patch('live_voice.providers.SpeechRecognition.load') as load:
            app = create_app(preview=True, archive_store=object(), archive_importer=object())
            with TestClient(app) as client:
                status = client.get('/api/status').json()
                self.assertTrue(status['preview']); self.assertFalse(status['ready'])
                self.assertTrue(status['voice_verification'])
            load.assert_not_called()


if __name__ == '__main__': unittest.main()
