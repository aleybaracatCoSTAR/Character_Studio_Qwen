"""Voice failures have actionable counters without retaining private request text."""
import asyncio
import base64
from io import BytesIO
import json
import unittest
import wave

import httpx

from live_voice.director import DirectorSettings
from live_voice.providers import (LocalProviders, OUTPUT_RATE, VoiceProtocolError,
                                  VoiceServiceUnavailable, VoiceTakeIncomplete,
                                  speech_audio_limit, speech_token_budget, synthetic_reference)
from live_voice.session import LiveSession
from studio.characters import example
from test_voice_generation import DummySTT, delta, done, event, response


class VoiceDiagnosticValueTests(unittest.TestCase):
    def test_message_only_constructor_remains_compatible_and_summary_never_uses_that_message(self):
        failure = VoiceTakeIncomplete('PRIVATE_SENTINEL original message')
        self.assertEqual(str(failure), 'PRIVATE_SENTINEL original message')
        self.assertEqual(failure.diagnostic(), {'reason':'incomplete'})
        self.assertNotIn('PRIVATE_SENTINEL', failure.safe_summary())

    def test_diagnostic_fields_are_allowlisted_even_if_exception_data_is_mutated(self):
        failure = VoiceTakeIncomplete('PRIVATE_SENTINEL', reason='PRIVATE_SENTINEL', metadata={
            'budget_tokens':1600, 'output_tokens':1600, 'audio_seconds':4.5,
            'service_code':500, 'service_type':'PRIVATE_SENTINEL', 'service_action':'discard',
            'message':'PRIVATE_SENTINEL', 'url':'https://private.invalid/',
            'audio':'PRIVATE_AUDIO', 'ref_text':'PRIVATE_REFERENCE', 'elapsed_ms':float('nan'),
            'audio_bytes':10**1000})
        failure.metadata['message'] = 'PRIVATE_SENTINEL'
        safe = failure.diagnostic()
        self.assertEqual(safe, {'reason':'incomplete', 'budget_tokens':1600, 'output_tokens':1600,
                                'audio_seconds':4.5, 'service_code':500, 'service_action':'discard'})
        self.assertNotIn('PRIVATE', json.dumps(safe) + failure.safe_summary())

    def test_audio_allowance_matches_larger_requested_token_duration_and_remains_bounded(self):
        self.assertEqual(speech_audio_limit(240), 4*1024*1024)
        self.assertGreater(speech_audio_limit(1600), 4*1024*1024)
        self.assertLessEqual(speech_audio_limit(1600), 8*1024*1024)
        self.assertEqual(speech_audio_limit(1_000_000), 8*1024*1024)
        self.assertGreaterEqual(speech_audio_limit(1600)/(2*OUTPUT_RATE), 1600/21.5)

    def test_automatic_reference_requires_a_whole_valid_duration_and_preserves_its_exact_transcript(self):
        words = 'The complete reference transcript.'
        for samples in (OUTPUT_RATE-1, OUTPUT_RATE*30+1):
            self.assertIsNone(synthetic_reference(b'\x01\x00'*samples, words))
        for samples in (OUTPUT_RATE, OUTPUT_RATE*30):
            pcm = b'\x01\x00'*samples
            audio, transcript = synthetic_reference(pcm, words)
            self.assertEqual(transcript, words)
            with wave.open(BytesIO(base64.b64decode(audio.split(',',1)[1]))) as reader:
                self.assertEqual(reader.getnframes(), samples)
                self.assertEqual(reader.readframes(samples), pcm)
        self.assertIsNone(synthetic_reference(b'\x01', words))
        self.assertIsNone(synthetic_reference(b'\x01\x00'*OUTPUT_RATE, ''))


class VoiceDiagnosticProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_uncapped_49_second_reply_is_not_rejected_by_the_old_47_second_byte_limit(self):
        spoken = 'steady ' * 50
        budget = speech_token_budget(spoken)
        pcm = b'\x01\x00' * (OUTPUT_RATE*49)
        self.assertGreater(len(pcm), 4*1024*1024)
        self.assertLess(len(pcm), speech_audio_limit(budget))
        tokens = 1054  # ~49 seconds at the pinned codec's ~21.5 Hz.
        self.assertLess(tokens, budget)
        requests = []
        def serve(request):
            requests.append(json.loads(request.content))
            return response(delta(pcm)+done(tokens), widths=(65536,))
        providers = LocalProviders(DummySTT(), transport=httpx.MockTransport(serve))
        try:
            result = b''.join([chunk async for chunk in providers.audio(spoken)])
            self.assertEqual(result, pcm)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]['max_new_tokens'], budget)
        finally: await providers.close()

    async def test_each_completion_failure_has_a_distinct_reason_and_final_attempt_counts_without_partial_audio(self):
        cases = (
            (delta(), 'missing_done'),
            (delta()+b'data: {"type":"speech.audio.done"}', 'unfinished_event'),
            (b'data: {bad JSON}\n\n', 'malformed_event'),
            (delta(audio='not base64!')+done(), 'malformed_audio'),
            (delta()+event('speech.audio.done'), 'missing_usage'),
            (delta(b'')+done(), 'empty_audio'),
            (delta(b'\x01')+done(), 'unaligned_audio'),
            (delta()+done()+done(), 'events_after_done'),
            (event('unrecognized'), 'unknown_event'),
            (b'event: mismatched\ndata: {"type":"speech.audio.delta"}\n\n', 'inconsistent_event'))
        for wire, expected in cases:
            requests = []
            def serve(request): requests.append(request); return response(wire)
            providers = LocalProviders(DummySTT(), transport=httpx.MockTransport(serve))
            yielded = []
            notices = []
            async def notice(message): notices.append(message)
            try:
                with self.subTest(reason=expected), self.assertLogs('live_voice.providers', level='WARNING'):
                    with self.assertRaises(VoiceTakeIncomplete) as raised:
                        async for chunk in providers.audio('A complete reply.', on_notice=notice): yielded.append(chunk)
                    diagnostic = raised.exception.diagnostic()
                    self.assertEqual(diagnostic['reason'], expected)
                    self.assertEqual((diagnostic['attempt'], diagnostic['attempts']), (2, 2))
                    self.assertIn('audio_bytes', diagnostic)
                    self.assertIn('audio_seconds', diagnostic)
                    self.assertEqual(len(requests), 2)
                    self.assertEqual(yielded, [])
                    self.assertIn('['+expected+']', notices[0])
            finally: await providers.close()

    async def test_token_limit_still_discards_both_takes_and_reports_output_budget_and_unknown_finish_reason(self):
        requests = []
        def serve(request):
            payload = json.loads(request.content); requests.append(payload)
            return response(delta()+done(payload['max_new_tokens']))
        providers = LocalProviders(DummySTT(), transport=httpx.MockTransport(serve))
        try:
            with self.assertLogs('live_voice.providers', level='WARNING') as logs:
                with self.assertRaises(VoiceTakeIncomplete) as raised:
                    _ = [chunk async for chunk in providers.audio('A complete reply.')]
            diagnostic = raised.exception.diagnostic()
            self.assertEqual(diagnostic['reason'], 'token_limit')
            self.assertEqual(diagnostic['output_tokens'], diagnostic['budget_tokens'])
            self.assertEqual(diagnostic['finish_reason'], 'not_reported')
            self.assertTrue(diagnostic['done_received'])
            self.assertIn('token_limit', '\n'.join(logs.output))
            self.assertEqual(requests[0], requests[1])
        finally: await providers.close()

    async def test_reported_length_abort_or_cancelled_is_rejected_even_below_the_requested_budget(self):
        for finish, reason in (('length','token_limit'), ('abort','upstream_error'), ('cancelled','upstream_error')):
            wire = delta()+event('speech.audio.done', usage={'output_tokens':10}, finish_reason=finish)
            providers = LocalProviders(DummySTT(), transport=httpx.MockTransport(lambda request:response(wire)))
            yielded = []
            try:
                with self.subTest(finish=finish), self.assertLogs('live_voice.providers', level='WARNING'):
                    with self.assertRaises(VoiceTakeIncomplete) as raised:
                        async for chunk in providers.audio('A complete reply.'): yielded.append(chunk)
                    self.assertEqual(raised.exception.diagnostic()['reason'], reason)
                    self.assertEqual(raised.exception.diagnostic()['finish_reason'], finish)
                    self.assertLess(raised.exception.diagnostic()['output_tokens'],
                                    raised.exception.diagnostic()['budget_tokens'])
                    self.assertEqual(yielded, [])
            finally: await providers.close()

    async def test_upstream_error_reports_only_safe_service_fields_and_counters(self):
        secret = 'PRIVATE_SENTINEL transcript https://private.invalid/ PRIVATE_REFERENCE PRIVATE_AUDIO'
        wire = delta(b'\x01\x00'*OUTPUT_RATE) + event('speech.audio.error', error={
            'message':secret, 'type':'server_error', 'code':500, 'partial_audio':True,
            'action':'discard', 'url':secret, 'ref_audio':secret})
        providers = LocalProviders(DummySTT(), transport=httpx.MockTransport(lambda request:response(wire, widths=(65536,))))
        notices = []
        async def notice(value): notices.append(value)
        try:
            with self.assertLogs('live_voice.providers', level='WARNING') as logs:
                with self.assertRaises(VoiceTakeIncomplete) as raised:
                    _ = [chunk async for chunk in providers.audio(secret, (secret, secret), on_notice=notice)]
            diagnostic = raised.exception.diagnostic()
            self.assertEqual(diagnostic['reason'], 'upstream_error')
            self.assertEqual(diagnostic['service_type'], 'server_error')
            self.assertEqual(diagnostic['service_code'], 500)
            self.assertEqual(diagnostic['service_action'], 'discard')
            self.assertTrue(diagnostic['service_partial_audio'])
            self.assertEqual(diagnostic['audio_seconds'], 1)
            self.assertNotIn('output_tokens', diagnostic)
            visible = '\n'.join(logs.output + notices) + json.dumps(diagnostic) + raised.exception.safe_summary()
            self.assertNotIn('PRIVATE', visible)
            self.assertNotIn('private.invalid', visible)
        finally: await providers.close()

    async def test_http_rejection_is_not_retried_and_never_copies_private_response_or_url(self):
        requests = []
        def serve(request):
            requests.append(request)
            return httpx.Response(400, json={'error':{'message':'PRIVATE_SENTINEL sensitive text'}})
        providers = LocalProviders(DummySTT(), tts_url='http://user:PRIVATE_PASSWORD@private.invalid',
                                   transport=httpx.MockTransport(serve))
        try:
            with self.assertLogs('live_voice.providers', level='WARNING') as logs:
                with self.assertRaises(httpx.HTTPStatusError) as raised:
                    _ = [chunk async for chunk in providers.audio('PRIVATE_SENTINEL')]
            self.assertEqual(len(requests), 1)
            self.assertEqual(raised.exception.voice_failure.diagnostic()['http_status'], 400)
            self.assertNotIn('PRIVATE', '\n'.join(logs.output)+raised.exception.voice_failure.safe_summary())
            self.assertNotIn('private.invalid', '\n'.join(logs.output)+raised.exception.voice_failure.safe_summary())
        finally: await providers.close()


class VoiceDiagnosticSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_automatic_voice_waits_for_a_supported_whole_take_instead_of_seeding_invalid_references(self):
        events = []
        byte_counts = {}
        class Providers:
            stt = DummySTT()
            def __init__(self):
                self.words = ['A short reply.', 'One complete longer reply.',
                              'A complete reference reply.', 'The following reply.']
                self.index = 0
                self.references = []
                self.samples = [OUTPUT_RATE//2, OUTPUT_RATE*31, OUTPUT_RATE*2, OUTPUT_RATE*3]
            async def text(self, character, history):
                yield self.words[self.index]
            async def audio(self, text, reference=None, on_notice=None):
                self.references.append(reference)
                pcm = b'\x01\x00'*self.samples[self.index]
                self.index += 1
                for start in range(0,len(pcm),16384): yield pcm[start:start+16384]
        providers = Providers()
        async def send_json(value): events.append(value)
        async def send_bytes(value):
            byte_counts[session.turn_id] = byte_counts.get(session.turn_id,0)+len(value)-8
        session = LiveSession(providers, example(), send_json, send_bytes,
                              director=DirectorSettings(enabled=False))
        try:
            for index in range(4):
                await session.begin(text=f'Question {index}.')
                await asyncio.wait_for(session.task,1)
                self.assertEqual(events[-1]['type'],'response_done')
                self.assertEqual(byte_counts[session.turn_id],providers.samples[index]*2)
                if index<2: self.assertIsNone(session.reference)
                self.assertTrue(session.history.acknowledge(session.turn_id,0))
                self.assertTrue(session.playback_done(session.turn_id))
            self.assertEqual(providers.references[:3],[None,None,None])
            audio, transcript = providers.references[3]
            self.assertEqual(transcript,providers.words[2])
            with wave.open(BytesIO(base64.b64decode(audio.split(',',1)[1]))) as reader:
                self.assertEqual(reader.getnframes(),providers.samples[2])
            notices = [value['message'] for value in events if value['type']=='voice_notice']
            self.assertEqual(len(notices),2)
            self.assertTrue(all('This reply has not been cropped.' in notice for notice in notices))
        finally: await session.close()

    async def test_portal_error_and_server_log_expose_safe_details_without_the_exception_message(self):
        events = []
        class Providers:
            stt = DummySTT()
            async def text(self, character, history): yield 'One unchanged full reply.'
            async def audio(self, text, reference=None, on_notice=None):
                raise VoiceTakeIncomplete('PRIVATE_SENTINEL unsafe message', reason='token_limit', metadata={
                    'output_tokens':1600, 'budget_tokens':1600, 'audio_seconds':40.5,
                    'audio_bytes':3572100, 'done_received':True, 'finish_reason':'not_reported',
                    'message':'PRIVATE_SENTINEL', 'ref_text':'PRIVATE_REFERENCE'})
                yield b''
        async def send_json(value): events.append(value)
        async def send_bytes(value): self.fail('An invalid take must not reach playback.')
        session = LiveSession(Providers(), example(), send_json, send_bytes,
                              director=DirectorSettings(enabled=False))
        try:
            with self.assertLogs('live_voice.session', level='WARNING') as logs:
                await session.begin(text='Hello.')
                await asyncio.wait_for(session.task, 1)
            failure = events[-1]
            self.assertEqual(failure['type'], 'error')
            self.assertTrue(failure['can_retry_voice'])
            self.assertEqual(failure['voice_diagnostic']['reason'], 'token_limit')
            self.assertIn('[token_limit]', failure['message'])
            self.assertIn('1600/1600 tokens', failure['message'])
            self.assertIn('40.5 s', failure['message'])
            self.assertNotIn('PRIVATE', json.dumps(failure)+'\n'.join(logs.output))
        finally: await session.close()

    async def test_portal_http_error_uses_attached_safe_diagnostic_instead_of_httpx_private_message(self):
        events = []
        def serve(request): return httpx.Response(400,json={'error':'PRIVATE_BODY'})
        providers = LocalProviders(DummySTT(),tts_url='http://user:PRIVATE_PASSWORD@private.invalid',
                                   transport=httpx.MockTransport(serve))
        async def text(character,history): yield 'The unchanged reply.'
        providers.text = text
        async def send_json(value): events.append(value)
        async def send_bytes(value): self.fail('Rejected requests cannot play audio.')
        session = LiveSession(providers,example(),send_json,send_bytes,director=DirectorSettings(enabled=False))
        try:
            with self.assertLogs(level='WARNING') as logs:
                await session.begin(text='Hello.')
                await asyncio.wait_for(session.task,1)
            self.assertEqual(events[-1]['voice_diagnostic']['reason'],'http_status')
            self.assertEqual(events[-1]['voice_diagnostic']['http_status'],400)
            self.assertIn('HTTP 400',events[-1]['message'])
            self.assertNotIn('PRIVATE',json.dumps(events[-1])+'\n'.join(logs.output))
            self.assertNotIn('private.invalid',json.dumps(events[-1])+'\n'.join(logs.output))
        finally:
            await session.close()
            await providers.close()


if __name__ == '__main__': unittest.main()
