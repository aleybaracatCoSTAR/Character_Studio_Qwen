"""CPU-only source-shaped fixtures for the pinned native Omni SSE protocol.

These verify the adapter, not GPU speed, acoustic quality or model support.
The top-level modality and per-choice audio_metadata match serving_chat.py
at vLLM-Omni 798fef99a; these are not recordings of a live model call.
"""
import asyncio
import base64
import copy
from io import BytesIO
import json
import struct
import unittest
from unittest.mock import patch
import wave

import httpx

from live_voice.inspection import RequestInspection
from live_voice.omni import OmniProvider, OmniStreamError, native_voice, response_messages
from live_voice.reactions import EmotionalTrigger
from live_voice.voice import Delivery, VoiceSettings
from studio.characters import example


def sse(value):
    data = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return ('data: ' + data + '\r\n\r\n').encode('utf8')


def text(value='Hello, I’m Anna.', finish=None):
    return {'modality':'text', 'choices':[{'index':0, 'delta':{'content':value}, 'finish_reason':finish}]}


def audio(pcm=b'\x01\x00\xfe\xff', finish=None, **metadata):
    values = {'format':'pcm', 'sample_rate_hz':24000, 'channels':1, 'frame_count':len(pcm)//2}
    values.update(metadata)
    return {'modality':'audio', 'choices':[{'index':0,
        'delta':{'content':base64.b64encode(pcm).decode()}, 'audio_metadata':values, 'finish_reason':finish}]}


def terminal(finish='stop'):
    # The pinned runtime's fallback terminal and usage events omit modality.
    return {'choices':[{'index':0, 'delta':{}, 'finish_reason':finish}]}


def complete(pcm=b'\x01\x00\xfe\xff'):
    return b''.join(map(sse, [text(), audio(pcm), terminal(),
        {'choices':[], 'usage':{'prompt_tokens':99, 'completion_tokens':12, 'total_tokens':111}}, '[DONE]']))


class Fragmented(httpx.AsyncByteStream):
    def __init__(self, payload, widths=(1, 3, 17, 2, 101)):
        self.payload = payload
        self.widths = widths
        self.closed = False

    async def __aiter__(self):
        offset = index = 0
        while offset < len(self.payload):
            width = self.widths[index % len(self.widths)]
            yield self.payload[offset:offset+width]
            offset += width
            index += 1

    async def aclose(self): self.closed = True


class Gated(httpx.AsyncByteStream):
    def __init__(self):
        self.release = asyncio.Event()
        self.waiting = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        yield sse(text())
        yield sse(audio(b'\x01\x00'))
        self.waiting.set()
        await self.release.wait()
        yield sse(audio(b'\x02\x00', finish='stop'))
        yield sse('[DONE]')

    async def aclose(self): self.closed = True


class FakeSTT:
    model = object()
    def close(self): raise AssertionError('The Omni wrapper does not own shared STT.')


class Local:
    stt = FakeSTT()
    director_url = 'http://director:18094'
    director_model = 'backstage-model'
    llm_url = 'http://baseline-actor:18092'
    tts_url = 'http://baseline-tts:18093'

    async def direct(self, *args, **kwargs): return args, kwargs
    async def close(self): raise AssertionError('The app closes the base provider separately.')


def response(payload):
    return httpx.Response(200, headers={'content-type':'text/event-stream; charset=utf-8'}, stream=Fragmented(payload))


class OmniPromptTests(unittest.TestCase):
    def setUp(self): self.character = example()

    def test_native_input_is_exact_mono_wav_replacing_only_latest_transcript(self):
        history = [{'role':'user','content':'Earlier question'}, {'role':'assistant','content':'Earlier heard answer'},
                   {'role':'user','content':'This transcript is used backstage, not substituted for speech.'}]
        archive = [{'role':'system','content':'Keep source facts grounded.'},
                   {'role':'user','content':'SOURCE RECORD: Monet, image https://images.example/work.jpg'}]
        original = copy.deepcopy((history, archive))
        pcm = struct.pack('<hhhh', -32768, -5, 5, 32767)
        self.character.beat = 'Current director moment: Anna is anxious about reset.'
        messages = response_messages(self.character, history, pcm=pcm, archive=archive,
                                    delivery=Delivery(emotion='anxious', direction='Speak clearly throughout.'))
        self.assertIn(self.character.name, messages[0]['content'])
        self.assertIn(self.character.beat, messages[0]['content'])
        self.assertIn('shaky voice', messages[0]['content'])
        self.assertIn('Speak clearly throughout.', messages[0]['content'])
        self.assertNotIn('<|speaker:', messages[0]['content'])
        self.assertEqual(messages[1:3], archive)
        self.assertEqual(messages[3:5], history[:2])
        content = messages[-1]['content']
        self.assertEqual([part['type'] for part in content], ['audio_url'])
        raw = base64.b64decode(content[0]['audio_url']['url'].split(',', 1)[1])
        with wave.open(BytesIO(raw), 'rb') as reader:
            self.assertEqual((reader.getframerate(), reader.getnchannels(), reader.getsampwidth()), (16000, 1, 2))
            self.assertEqual(reader.readframes(reader.getnframes()), pcm)
        self.assertEqual((history, archive), original)

    def test_typed_stage_action_stays_text_and_no_native_audio_is_invented(self):
        history = [{'role':'user','content':'[Stage action] A visitor presses reset.'}]
        messages = response_messages(self.character, history, delivery=Delivery(emotion='sad'))
        self.assertEqual(messages[-1], history[-1])
        self.assertIn('subdued', messages[0]['content'])
        self.assertNotIn('[sad]', messages[0]['content'])

    def test_rejects_bad_or_overlong_recording_and_does_not_replace_old_user(self):
        history = [{'role':'user','content':'Hello'}]
        for pcm in (b'', b'\x00', 'not PCM', b'\x00\x00'*480001):
            with self.subTest(kind=type(pcm), length=len(pcm)):
                with self.assertRaises(ValueError): response_messages(self.character, history, pcm=pcm)
        with self.assertRaises(ValueError):
            response_messages(self.character, [{'role':'assistant','content':'Old answer'}], pcm=b'\x00\x00')

    def test_native_voice_mapping_keeps_custom_references_out(self):
        self.character.speaker = 'Aiden'
        self.assertEqual(native_voice(self.character, VoiceSettings()), 'Aiden')
        self.assertEqual(native_voice(self.character, VoiceSettings(preset='female')), 'Chelsie')
        self.assertEqual(native_voice(self.character, VoiceSettings(preset='male')), 'Ethan')
        custom = VoiceSettings.model_construct(preset='custom', reference_audio='private audio', reference_text='private words')
        with self.assertRaisesRegex(ValueError, 'custom WAV'): native_voice(self.character, custom)


class OmniProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.character = example()
        self.history = [{'role':'user', 'content':'Hello, Anna.'}]

    async def collect(self, payload):
        provider = OmniProvider(Local(), transport=httpx.MockTransport(lambda request:response(payload)))
        try: return [item async for item in provider.stream_response(self.character, self.history)]
        finally: await provider.close()

    async def test_fragmented_packets_preserve_unicode_order_pcm_and_final_tail(self):
        first = bytes(range(256))*80
        last = b'\x19\x20'
        payload = b': keepalive\r\n\r\n' + b''.join(map(sse, [
            {'modality':'text', 'choices':[{'index':0,'delta':{'role':'assistant'}}]},
            text('Hello, '), audio(first), text('I’m Anna.'), audio(last, finish='stop'),
            {'choices':[], 'usage':{'completion_tokens':9}}, '[DONE]']))
        events = await self.collect(payload)
        self.assertEqual(''.join(event['text'] for event in events if event['type']=='text'), 'Hello, I’m Anna.')
        self.assertEqual(b''.join(event['pcm'] for event in events if event['type']=='audio'), first+last)
        self.assertTrue(all(len(event['pcm'])<=16384 and len(event['pcm'])%2==0
                            and event['sample_rate']==24000 for event in events if event['type']=='audio'))
        self.assertEqual([event['type'] for event in events], ['text','audio','audio','text','audio','done'])
        self.assertEqual(events[-1]['metrics']['audio_bytes'], len(first)+2)
        self.assertEqual(events[-1]['metrics']['completion_tokens'], 9)

    async def test_first_audio_is_yielded_before_the_take_or_text_finishes(self):
        stream = Gated()
        provider = OmniProvider(Local(), transport=httpx.MockTransport(lambda request:
            httpx.Response(200, headers={'content-type':'text/event-stream'}, stream=stream)))
        generator = provider.stream_response(self.character, self.history)
        try:
            self.assertEqual((await anext(generator))['type'], 'text')
            first = await asyncio.wait_for(anext(generator), 1)
            self.assertEqual(first['pcm'], b'\x01\x00')
            self.assertFalse(stream.closed)
            stream.release.set()
            remaining = [event async for event in generator]
            self.assertEqual([event['type'] for event in remaining], ['audio','done'])
            self.assertEqual(remaining[0]['pcm'], b'\x02\x00')
            self.assertTrue(stream.closed)
        finally:
            await generator.aclose()
            await provider.close()

    async def test_per_modality_stop_does_not_drop_later_audio(self):
        # Text can finish while the Talker still has packets to generate.
        events = await self.collect(b''.join(map(sse, [text(finish='stop'), audio(b'\x01\x00'),
            {'modality':'audio','choices':[{'index':0,'delta':{'content':''},'finish_reason':'stop'}]}, '[DONE]'])))
        self.assertEqual(b''.join(event['pcm'] for event in events if event['type']=='audio'), b'\x01\x00')
        self.assertEqual(events[-1]['type'], 'done')

    async def test_request_has_real_audio_and_inspection_explicitly_omits_recording(self):
        requests = []; published = []
        def serve(request):
            requests.append(json.loads(request.content))
            return response(complete())
        async def send(value): published.append(value)
        inspector = RequestInspection(send)
        provider = OmniProvider(Local(), transport=httpx.MockTransport(serve))
        pcm = b'\x01\x00'*20
        try:
            with inspector.scope(3):
                events = [event async for event in provider.stream_response(self.character, self.history, pcm=pcm,
                    delivery=Delivery(emotion='calm'), voice='Ethan')]
            self.assertEqual(events[-1]['type'], 'done')
            request = requests[0]
            self.assertEqual(request['modalities'], ['text','audio'])
            self.assertEqual(request['audio'], {'format':'pcm16'})
            self.assertEqual(request['stream_options'], {'include_usage':True})
            self.assertEqual(request['speaker'], 'Ethan')
            self.assertNotIn('voice', request['audio'])
            self.assertTrue(request['messages'][-1]['content'][0]['audio_url']['url'].startswith('data:audio/wav;base64,'))
            inspected = inspector.requests[0]['payload']
            self.assertEqual(inspected['messages'][:-1], request['messages'][:-1])
            self.assertIn('Native microphone WAV omitted', inspected['messages'][-1]['content'][0]['text'])
            self.assertNotIn('base64', json.dumps(inspected))
            self.assertEqual(inspector.requests[0]['stage'], 'actor')
        finally: await provider.close()

    async def test_audio_format_metadata_is_required_and_invalid_packet_is_never_emitted(self):
        invalid = [audio(sample_rate_hz=44100), audio(channels=2), audio(format='wav'),
                   audio(frame_count=3), audio(frame_count=True), audio(sample_rate_hz=True)]
        missing = audio(); del missing['choices'][0]['audio_metadata']; invalid.append(missing)
        for event in invalid:
            with self.subTest(event=event):
                provider = OmniProvider(Local(), transport=httpx.MockTransport(lambda request:
                    response(b''.join(map(sse, [text(), event, terminal(), '[DONE]'])))))
                emitted = []
                try:
                    with self.assertRaises(OmniStreamError):
                        async for value in provider.stream_response(self.character, self.history): emitted.append(value)
                    self.assertEqual([value['type'] for value in emitted], ['text'])
                finally: await provider.close()

    async def test_bad_base64_and_modality_have_fixed_safe_errors(self):
        malformed = audio(); malformed['choices'][0]['delta']['content'] = 'PRIVATE_NOT_BASE64!'
        missing = text('PRIVATE REPLY'); del missing['modality']
        for event in (malformed, missing, {'error':{'message':'PRIVATE SERVER PROMPT'}}):
            provider = OmniProvider(Local(), transport=httpx.MockTransport(lambda request:
                response(sse(event)+sse('[DONE]'))))
            try:
                with self.assertRaises(OmniStreamError) as caught:
                    _ = [value async for value in provider.stream_response(self.character, self.history)]
                self.assertNotIn('PRIVATE', str(caught.exception))
            finally: await provider.close()

    async def test_no_done_no_stop_length_empty_or_content_after_last_stop_cannot_complete(self):
        payloads = [sse(text())+sse(audio())+sse(terminal()),
                    sse(text())+sse(audio())+sse('[DONE]'),
                    sse(text())+sse(audio())+sse(terminal('length'))+sse('[DONE]'),
                    sse(text())+sse(terminal())+sse('[DONE]'),
                    sse(audio())+sse(terminal())+sse('[DONE]'),
                    sse(text())+sse(terminal())+sse(audio())+sse('[DONE]')]
        for payload in payloads:
            requests=[]; emitted=[]
            def serve(request): requests.append(request); return response(payload)
            provider = OmniProvider(Local(), transport=httpx.MockTransport(serve))
            try:
                with self.subTest(payload=payload):
                    with self.assertRaises(OmniStreamError):
                        async for event in provider.stream_response(self.character, self.history): emitted.append(event)
                    self.assertNotIn('done', [event['type'] for event in emitted])
                    self.assertEqual(len(requests), 1)
            finally: await provider.close()

    async def test_streaming_error_after_partial_audio_is_visible_without_retry_or_done(self):
        requests=[]; emitted=[]
        payload = sse(text())+sse(audio())+sse({'error':{'message':'private'}})+sse('[DONE]')
        def serve(request): requests.append(request); return response(payload)
        provider = OmniProvider(Local(), transport=httpx.MockTransport(serve))
        try:
            with self.assertRaises(OmniStreamError):
                async for event in provider.stream_response(self.character, self.history): emitted.append(event)
            self.assertEqual([event['type'] for event in emitted], ['text','audio'])
            self.assertEqual(len(requests), 1)
        finally: await provider.close()

    async def test_bad_later_audio_cannot_complete_or_replace_already_streamed_samples(self):
        bad_base64 = audio(); bad_base64['choices'][0]['delta']['content'] = 'not base64!'
        for invalid in (bad_base64, audio(sample_rate_hz=44100)):
            requests=[]; emitted=[]
            payload = b''.join(map(sse, [text(), audio(b'\x07\x00'), invalid, terminal(), '[DONE]']))
            def serve(request): requests.append(request); return response(payload)
            provider = OmniProvider(Local(), transport=httpx.MockTransport(serve))
            try:
                with self.subTest(invalid=invalid):
                    with self.assertRaises(OmniStreamError):
                        async for event in provider.stream_response(self.character, self.history): emitted.append(event)
                    self.assertEqual([event['type'] for event in emitted], ['text','audio'])
                    self.assertEqual(emitted[-1]['pcm'], b'\x07\x00')
                    self.assertEqual(len(requests), 1)
            finally: await provider.close()

    async def test_cancellation_closes_stream_and_never_retries_a_partly_heard_reply(self):
        stream = Gated(); requests=[]
        def serve(request):
            requests.append(request)
            return httpx.Response(200, headers={'content-type':'text/event-stream'}, stream=stream)
        provider = OmniProvider(Local(), transport=httpx.MockTransport(serve))
        generator = provider.stream_response(self.character, self.history)
        try:
            await anext(generator); await anext(generator)
            pending = asyncio.create_task(anext(generator))
            await asyncio.wait_for(stream.waiting.wait(), 1)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError): await pending
            self.assertTrue(stream.closed)
            self.assertEqual(len(requests), 1)
        finally:
            await generator.aclose(); await provider.close()

    async def test_limits_and_incomplete_sse_never_produce_done(self):
        with patch('live_voice.omni.MAX_TEXT_CHARS', 2):
            with self.assertRaisesRegex(OmniStreamError, 'text limit'): await self.collect(complete())
        with patch('live_voice.omni.MAX_OUTPUT_PCM_BYTES', 2):
            with self.assertRaisesRegex(OmniStreamError, 'audio limit'): await self.collect(complete())
        with patch('live_voice.omni.MAX_EVENT_CHARS', 10):
            with self.assertRaisesRegex(OmniStreamError, 'oversized'): await self.collect(complete())
        with self.assertRaisesRegex(OmniStreamError, 'during a streaming event'):
            await self.collect(sse(text())+b'data: {"choices":')

    async def test_http_or_nonstream_response_is_not_retried(self):
        for code, content_type in ((400,'application/json'), (200,'application/json')):
            requests=[]
            def serve(request):
                requests.append(request)
                return httpx.Response(code, headers={'content-type':content_type}, content=b'{}')
            provider = OmniProvider(Local(), transport=httpx.MockTransport(serve))
            try:
                with self.assertRaises((httpx.HTTPStatusError, OmniStreamError)):
                    _ = [event async for event in provider.stream_response(self.character, self.history)]
                self.assertEqual(len(requests), 1)
            finally: await provider.close()

    async def test_health_requires_only_native_model_shared_stt_and_director(self):
        urls=[]
        def serve(request):
            urls.append(str(request.url))
            return httpx.Response(200, json={'healthy':True})
        provider = OmniProvider(Local(), base_url='http://native:18095', transport=httpx.MockTransport(serve))
        try:
            self.assertEqual(await provider.health(), {'stt':True,'omni':True,'director':True})
            self.assertEqual(set(urls), {'http://native:18095/health','http://director:18094/health'})
            self.assertIs(provider.stt, Local.stt)
        finally: await provider.close()

    async def test_direct_delegates_but_trigger_posts_to_actual_director_model(self):
        requests=[]
        def serve(request):
            requests.append((str(request.url), json.loads(request.content)))
            return httpx.Response(200, json={'choices':[{'message':{'content':json.dumps({'action':'hold','evidence':'No threat.'})}}]})
        provider = OmniProvider(Local(), transport=httpx.MockTransport(serve))
        try:
            self.assertEqual(await provider.direct('character', test='context'), (('character',), {'test':'context'}))
            decision = await provider.assess_trigger(self.character, EmotionalTrigger(), self.history, 'Hello', False)
            self.assertEqual(decision.action, 'hold')
            self.assertEqual(requests[0][0], 'http://director:18094/v1/chat/completions')
            self.assertEqual(requests[0][1]['model'], 'backstage-model')
            self.assertEqual(requests[0][1]['response_format'], {'type':'json_object'})
        finally: await provider.close()


if __name__ == '__main__': unittest.main()
