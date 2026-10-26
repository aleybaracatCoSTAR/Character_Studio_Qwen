"""Only confirmed, uncapped voice takes may reach the live audio stream."""
import asyncio
import base64
import json
import unittest

import httpx

from live_voice.providers import LocalProviders, VoiceTakeIncomplete, speech_token_budget


PLAIN = 'I love them so much, but I feel so alone.'
STYLED = '<|speaker:0|> [subdued, fragile voice] ' + PLAIN
REFERENCE = ('data:audio/wav;base64,AAAA', 'These are the reference words.')


class DummySTT:
    def close(self): pass


class FragmentedStream(httpx.AsyncByteStream):
    def __init__(self, payload, widths=(1, 3, 17, 2, 101), fail=None):
        self.payload = payload
        self.widths = widths
        self.closed = False
        self.fail = fail

    async def __aiter__(self):
        start = index = 0
        while start < len(self.payload):
            width = self.widths[index % len(self.widths)]
            yield self.payload[start:start + width]
            start += width; index += 1
        if self.fail is not None: raise self.fail

    async def aclose(self): self.closed = True


def event(kind, **values):
    payload = json.dumps({'type': kind, **values}, ensure_ascii=False)
    return f'event: {kind}\r\ndata: {payload}\r\n\r\n'.encode()


def delta(pcm=b'\x01\x02\x03\x04', **metadata):
    values = {'sample_rate': 44100, 'channels': 1, 'response_format': 'pcm',
              'audio': base64.b64encode(pcm).decode()}
    values.update(metadata)
    return event('speech.audio.delta', **values)


def done(output_tokens=50):
    return event('speech.audio.done', usage={'output_tokens': output_tokens})


def response(payload, **kwargs):
    stream = FragmentedStream(payload, **kwargs)
    return httpx.Response(200, headers={'content-type': 'text/event-stream; charset=utf-8'}, stream=stream)


class VoiceGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_runaway_expressive_audio_is_discarded_before_retry(self):
        requests=[];notices=[]
        def serve(request):
            requests.append(json.loads(request.content))
            if len(requests)==1:return response(delta(b'\x01\x00'*(2*1024*1024+1)),widths=(65536,))
            return response(delta()+done())
        async def notice(message):notices.append(message)
        providers=LocalProviders(DummySTT(),transport=httpx.MockTransport(serve))
        try:
            pcm=b''.join([part async for part in providers.audio(STYLED,on_notice=notice)])
            self.assertEqual(pcm,b'\x01\x02\x03\x04')
            self.assertEqual(len(requests),2);self.assertEqual(len(notices),1)
        finally:await providers.close()

    async def test_fragmented_sse_retains_all_final_pcm_and_validates_before_first_yield(self):
        pcm = bytes(range(256)) * 80 + b'\x19\x20'
        # Audio event boundaries need not be PCM sample boundaries. The complete
        # take must remain sample-aligned and retain the last two bytes.
        audio_response = response(b': keepalive\r\n\r\n' + delta(pcm[:7]) + delta(pcm[7:]) + done())
        requests = []
        def serve(request):
            requests.append(json.loads(request.content)); return audio_response
        providers = LocalProviders(DummySTT(), transport=httpx.MockTransport(serve))
        try:
            chunks = []
            async for chunk in providers.audio(PLAIN, REFERENCE):
                self.assertTrue(audio_response.is_closed)
                self.assertLessEqual(len(chunk), 16384)
                self.assertEqual(len(chunk) % 2, 0)
                chunks.append(chunk)
            self.assertEqual(b''.join(chunks), pcm)
            self.assertEqual(len(chunks), 2)
            self.assertEqual(requests[0]['stream_format'], 'sse')
            self.assertEqual(requests[0]['response_format'], 'pcm')
            self.assertNotIn('sample_rate', requests[0])
            self.assertEqual(requests[0]['ref_text'], REFERENCE[1])
        finally: await providers.close()

    async def test_capped_styled_take_is_discarded_then_identical_reply_reuses_emotion_and_reference(self):
        requests = []; notices = []
        def serve(request):
            payload = json.loads(request.content); requests.append(payload)
            if len(requests) == 1:
                return response(delta(b'\x99\x99') + done(payload['max_new_tokens']))
            return response(delta(b'\x01\x02\x03\x04') + done())
        async def notice(message): notices.append(message)
        providers = LocalProviders(DummySTT(), transport=httpx.MockTransport(serve))
        try:
            chunks = [part async for part in providers.audio(STYLED, REFERENCE, on_notice=notice)]
            self.assertEqual(b''.join(chunks), b'\x01\x02\x03\x04')
            self.assertEqual([item['input'] for item in requests], [STYLED, STYLED])
            self.assertEqual(requests[0], requests[1])
            self.assertEqual(len(notices), 1)
            self.assertEqual(notices[0], 'Voice generation did not finish. Retrying the whole reply with the same emotion and voice.')
            self.assertNotIn(PLAIN, notices[0])
            for item in requests:
                self.assertEqual((item['ref_audio'], item['ref_text']), REFERENCE)
        finally: await providers.close()

    async def test_explicit_sse_error_can_retry_styled_reply_once_without_changing_any_parameters(self):
        requests = []
        def serve(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                return response(delta(b'\x99\x99') + event('speech.audio.error', error='private service details'))
            return response(delta() + done())
        providers = LocalProviders(DummySTT(), transport=httpx.MockTransport(serve))
        try:
            with self.assertLogs('live_voice.providers', level='WARNING') as captured:
                pcm = b''.join([part async for part in providers.audio(STYLED)])
            self.assertEqual(pcm, b'\x01\x02\x03\x04')
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[0], requests[1])
            self.assertNotIn('private service details', '\n'.join(captured.output))
            self.assertNotIn(PLAIN, '\n'.join(captured.output))
        finally: await providers.close()

    async def test_a_plain_incomplete_reply_also_retries_once_with_identical_parameters(self):
        requests = []; notices = []
        def serve(request):
            requests.append(json.loads(request.content))
            return response(delta() if len(requests) == 1 else delta() + done())
        async def notice(message): notices.append(message)
        providers = LocalProviders(DummySTT(), transport=httpx.MockTransport(serve))
        try:
            pcm = b''.join([part async for part in providers.audio(PLAIN, REFERENCE, on_notice=notice)])
            self.assertEqual(pcm, b'\x01\x02\x03\x04')
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[0], requests[1])
            self.assertEqual(requests[1]['input'], PLAIN)
            self.assertEqual(len(notices), 1)
        finally: await providers.close()

    async def test_plain_or_styled_failed_retry_surfaces_without_sending_partial_audio(self):
        for text in (PLAIN, STYLED):
            requests = []
            def serve(request):
                payload = json.loads(request.content); requests.append(payload)
                return response(delta() + done(payload['max_new_tokens']))
            providers = LocalProviders(DummySTT(), transport=httpx.MockTransport(serve))
            notices = []
            async def notice(message): notices.append(message)
            try:
                with self.assertRaises(VoiceTakeIncomplete):
                    _ = [part async for part in providers.audio(text, on_notice=notice)]
                self.assertEqual(len(requests), 2)
                self.assertEqual(requests[0], requests[1])
                self.assertEqual(len(notices), 1)
            finally: await providers.close()

    async def test_bad_or_missing_pcm_format_metadata_is_not_guessed_or_retried(self):
        for metadata in ({'sample_rate': 24000}, {'sample_rate': None}, {'sample_rate': True},
                         {'channels': 2}, {'channels': None}, {'response_format': 'wav'}):
            requests = []
            def serve(request):
                requests.append(request)
                return response(delta(**metadata) + done())
            providers = LocalProviders(DummySTT(), transport=httpx.MockTransport(serve))
            try:
                with self.subTest(metadata=metadata):
                    with self.assertRaises(RuntimeError) as raised:
                        _ = [part async for part in providers.audio(STYLED)]
                    self.assertNotIsInstance(raised.exception, VoiceTakeIncomplete)
                    self.assertEqual(len(requests), 1)
            finally: await providers.close()

    async def test_unfinished_malformed_empty_or_unaligned_plain_take_never_yields_audio(self):
        cases = [
            delta(),
            delta() + b'event: speech.audio.done\ndata: {"type":"speech.audio.done"}',
            b'event: speech.audio.delta\ndata: {bad json}\n\n',
            delta(audio='not base64!') + done(),
            delta() + event('speech.audio.done'),
            delta() + event('speech.audio.done', usage={'output_tokens': True}),
            delta(b'') + done(),
            delta(b'\x01') + done(),
            delta() + done() + done(),
            delta() + done() + delta(),
        ]
        for payload in cases:
            requests = []; yielded = []
            def serve(request): requests.append(request); return response(payload)
            async def notice(message): pass
            providers = LocalProviders(DummySTT(), transport=httpx.MockTransport(serve))
            try:
                with self.subTest(payload=payload):
                    with self.assertRaises(VoiceTakeIncomplete):
                        async for part in providers.audio(PLAIN, on_notice=notice): yielded.append(part)
                    self.assertEqual(yielded, [])
                    self.assertEqual(len(requests), 2)
            finally: await providers.close()

    async def test_http_network_and_content_type_failures_are_not_retried(self):
        for failure in ('http', 'network', 'content-type'):
            requests = []
            def serve(request):
                requests.append(request)
                if failure == 'http': return httpx.Response(503, json={'error': 'unavailable'})
                if failure == 'network': raise httpx.ConnectError('unreachable', request=request)
                return httpx.Response(200, headers={'content-type': 'audio/pcm'}, content=b'\x01\x02')
            providers = LocalProviders(DummySTT(), transport=httpx.MockTransport(serve))
            try:
                with self.subTest(failure=failure):
                    with self.assertRaises((httpx.HTTPError, RuntimeError)):
                        _ = [part async for part in providers.audio(STYLED)]
                    self.assertEqual(len(requests), 1)
            finally: await providers.close()

    async def test_cancellation_closes_take_without_retry_or_notice_or_partial_audio(self):
        requests = []; notices = []; yielded = []
        audio_response = response(delta(), fail=asyncio.CancelledError())
        def serve(request): requests.append(request); return audio_response
        async def notice(message): notices.append(message)
        providers = LocalProviders(DummySTT(), transport=httpx.MockTransport(serve))
        try:
            with self.assertRaises(asyncio.CancelledError):
                async for part in providers.audio(STYLED, on_notice=notice): yielded.append(part)
            self.assertTrue(audio_response.is_closed)
            self.assertEqual(len(requests), 1)
            self.assertEqual(yielded, [])
            self.assertEqual(notices, [])
        finally: await providers.close()

    async def test_retry_never_strips_a_cue_speaker_control_or_any_other_reply_text(self):
        for text in ('[sad] ' + PLAIN, 'I love [sad] everyone.',
                     '<|speaker:1|> [sad] ' + PLAIN, '<|speaker:0|> [sad] [quiet] ' + PLAIN):
            requests = []
            def serve(request):
                requests.append(json.loads(request.content)); return response(delta())
            providers = LocalProviders(DummySTT(), transport=httpx.MockTransport(serve))
            async def notice(message): pass
            try:
                with self.subTest(text=text):
                    with self.assertRaises(VoiceTakeIncomplete):
                        _ = [part async for part in providers.audio(text, on_notice=notice)]
                    self.assertEqual(len(requests), 2)
                    self.assertEqual(requests[0], requests[1])
                    self.assertEqual(requests[1]['input'], text)
            finally: await providers.close()


class VoiceBudgetTests(unittest.TestCase):
    def test_speaker_controls_and_delivery_cues_do_not_count_as_spoken_words(self):
        self.assertEqual(speech_token_budget(STYLED), speech_token_budget(PLAIN))
        self.assertEqual(speech_token_budget('<|speaker:15|> [sad] ' + PLAIN),
                         speech_token_budget(PLAIN))


if __name__ == '__main__': unittest.main()
