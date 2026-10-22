import asyncio
import json
import struct
import unittest

import httpx
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from live_voice.app import create_app
from live_voice.providers import LocalProviders
from live_voice.session import LiveSession
from live_voice.turns import FRAME_BYTES, PhraseChunker, ReplyHistory, TurnDetector
from studio.characters import example


def frames(voice, count):
    return (b"\x01\x00" if voice else b"\x00\x00") * (FRAME_BYTES // 2) * count


class TurnTests(unittest.TestCase):
    def detector(self, **kw): return TurnDetector(lambda frame:frame[:2]==b"\x01\x00", **kw)

    def test_vad_is_independent_of_network_packet_boundaries(self):
        audio=frames(False,12)+frames(True,18)+frames(False,30)
        expected=self.detector().feed(audio)
        detector=self.detector(); actual=[]
        for start in range(0,len(audio),218): actual.extend(detector.feed(audio[start:start+218]))
        self.assertEqual(actual,expected)
        self.assertEqual([x[0] for x in actual],["start","utterance"])
        self.assertEqual(actual[-1][2],480)

    def test_silence_and_short_noise_do_not_produce_user_turns(self):
        detector=self.detector()
        self.assertEqual(detector.feed(frames(False,100)),[])
        events=detector.feed(frames(True,3)+frames(False,30))
        self.assertEqual([x[0] for x in events],["start","noise"])

    def test_long_speech_is_bounded(self):
        detector=self.detector(max_seconds=1)
        events=detector.feed(frames(True,80))
        utterances=[x for x in events if x[0]=="utterance"]
        self.assertEqual(len(utterances),1)
        self.assertLessEqual(len(utterances[0][1]),16000*2)

    def test_phrase_is_emitted_before_the_complete_reply(self):
        chunker=PhraseChunker()
        self.assertEqual(chunker.feed("I am Anna, the washing machine. "),["I am Anna, the washing machine."])
        self.assertEqual(chunker.feed("The housekeeper knows"),[])
        self.assertEqual(chunker.feed("",final=True),["The housekeeper knows"])

    def test_phrase_length_is_bounded_without_splitting_words(self):
        chunker=PhraseChunker(target_chars=60)
        text="these are words "*20
        parts=chunker.feed(text)+chunker.feed("",final=True)
        self.assertEqual(" ".join(parts).split(),text.split())
        self.assertTrue(all(len(p)<=60 for p in parts))

    def test_history_does_not_remember_unheard_text(self):
        history=ReplyHistory(); history.begin(1,"Who are you?")
        history.add_phrase(0,"I am Anna.");history.add_phrase(1,"I want to help.")
        self.assertEqual(history.messages[-1]["content"],"")
        self.assertFalse(history.acknowledge(1,1))
        self.assertTrue(history.acknowledge(1,0))
        self.assertEqual(history.interrupt(),"I am Anna.")
        self.assertFalse(history.acknowledge(1,1))
        history.begin(2,"What do you want?")
        self.assertEqual(history.prompt_messages()[-2]["content"],"I am Anna.")

    def test_history_keeps_complete_pairs_bounded(self):
        history=ReplyHistory()
        for turn in range(20): history.begin(turn,str(turn))
        self.assertEqual(len(history.messages),12)
        self.assertEqual(history.messages[0]["role"],"user")


class FakeSTT:
    model=object(); error=None
    def load(self): pass
    async def transcribe(self,pcm): return "Who are you?"
    def close(self): pass


class FakeProviders:
    def __init__(self): self.stt=FakeSTT(); self.references=[];self.closed=False;self.cancelled=False;self.finish=None
    async def health(self): return {"stt":True,"llm":True,"tts":True}
    async def close(self): self.closed=True
    async def text(self,character,history):
        try:
            yield "I am Anna, the washing machine. "
            if self.finish: await self.finish.wait()
            yield "I would like someone to ask for help."
        finally: self.cancelled=True
    async def audio(self,text,reference=None):
        self.references.append(reference)
        yield b"\x01\x00"*100


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.providers=FakeProviders();self.events=[];self.audio=[];self.audio_seen=asyncio.Event()
        async def send_json(event): self.events.append(event)
        async def send_bytes(packet): self.audio.append(packet);self.audio_seen.set()
        self.session=LiveSession(self.providers,example(),send_json,send_bytes)

    async def asyncTearDown(self): await self.session.close()

    async def test_audio_arrives_before_llm_finishes(self):
        self.providers.finish=asyncio.Event()
        await self.session.begin(text="Hello")
        await asyncio.wait_for(self.audio_seen.wait(),1)
        self.assertFalse(any(x["type"]=="response_done" for x in self.events))
        self.providers.finish.set();await self.session.task
        self.assertTrue(any(x["type"]=="response_done" for x in self.events))
        self.assertEqual(struct.unpack("!II",self.audio[0][:8]),(1,0))
        self.assertIsNotNone(self.providers.references[-1])

    async def test_interruption_cancels_generation_and_filters_unheard_history(self):
        self.providers.finish=asyncio.Event()
        await self.session.begin(text="Hello")
        await asyncio.wait_for(self.audio_seen.wait(),1)
        await self.session.interrupt()
        self.assertTrue(self.providers.cancelled)
        self.assertIsNone(self.session.task)
        self.assertEqual(self.session.history.messages[-1]["content"],"")
        self.assertEqual(self.events[-1]["type"],"interrupted")
        count=len(self.audio);await asyncio.sleep(0)
        self.assertEqual(len(self.audio),count)

    async def test_empty_transcript_does_not_trigger_a_reply(self):
        async def empty(pcm): return ""
        self.providers.stt.transcribe=empty
        await self.session.begin(pcm=b"\x00\x00"*160)
        await self.session.task
        self.assertFalse(self.audio)
        self.assertTrue(any(x["type"]=="empty_transcript" for x in self.events))


class ByteStream(httpx.AsyncByteStream):
    def __init__(self,chunks): self.chunks=chunks;self.closed=False
    async def __aiter__(self):
        for chunk in self.chunks: yield chunk
    async def aclose(self): self.closed=True


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_sse_and_pcm_chunks_follow_the_actual_contract(self):
        requests=[]; stream=ByteStream([b"\x01",b"\x02\x03",b"\x04"])
        def handle(request):
            requests.append(json.loads(request.content))
            if request.url.path.endswith("completions"):
                return httpx.Response(200,text='data: {"choices":[{"delta":{"content":"Hello"}}]}\n\ndata: [DONE]\n\n')
            return httpx.Response(200,headers={"Content-Type":"audio/pcm"},stream=stream)
        providers=LocalProviders(FakeSTT(),transport=httpx.MockTransport(handle))
        try:
            text=[part async for part in providers.text(example(),[{"role":"user","content":"Hi"}])]
            audio=[part async for part in providers.audio("Hello",("data:audio/wav;base64,AAAA","Reference"))]
            self.assertEqual(text,["Hello"]);self.assertEqual(b"".join(audio),b"\x01\x02\x03\x04")
            self.assertTrue(requests[0]["stream"]);self.assertEqual(requests[1]["stream_format"],"audio")
            self.assertEqual(requests[1]["ref_text"],"Reference");self.assertTrue(stream.closed)
        finally: await providers.close()

    async def test_bad_audio_response_is_not_sent_as_pcm(self):
        providers=LocalProviders(FakeSTT(),transport=httpx.MockTransport(lambda request:httpx.Response(200,json={"error":"bad"})))
        try:
            with self.assertRaises(RuntimeError): _=[x async for x in providers.audio("Hi")]
        finally: await providers.close()


class LiveAPITests(unittest.TestCase):
    def test_preview_does_not_offer_fake_model_responses(self):
        with TestClient(create_app(preview=True,providers=FakeProviders())) as client:
            result=client.get('/api/status').json()
            self.assertTrue(result["preview"]);self.assertFalse(result["ready"])
            self.assertIn("Start call",client.get('/').text)

    def test_cross_origin_websocket_is_rejected(self):
        with TestClient(create_app(providers=FakeProviders())) as client:
            with self.assertRaises(WebSocketDisconnect) as captured:
                with client.websocket_connect('/api/live',headers={'origin':'https://other.invalid'}): pass
            self.assertEqual(captured.exception.code,1008)

    def test_text_call_streams_tagged_audio_and_cleans_up(self):
        providers=FakeProviders()
        with TestClient(create_app(providers=providers,vad_factory=lambda:TurnDetector(lambda frame:False))) as client:
            with client.websocket_connect('/api/live',headers={'origin':'http://testserver'}) as socket:
                socket.send_json({'type':'start','character':example().model_dump()})
                self.assertEqual(socket.receive_json()['type'],'ready')
                socket.send_json({'type':'text','text':'Hello'})
                events=[];packets=[]
                while True:
                    message=socket.receive()
                    if message.get('bytes'): packets.append(message['bytes'])
                    else:
                        event=json.loads(message['text']);events.append(event)
                        if event['type']=='response_done': break
                self.assertTrue(packets)
                self.assertTrue(any(x['type']=='transcript' for x in events))
                self.assertTrue(any(x['type']=='metrics' for x in events))
                socket.send_json({'type':'stop'})
        self.assertTrue(providers.closed)

    def test_invalid_start_releases_the_single_call_lock(self):
        with TestClient(create_app(providers=FakeProviders())) as client:
            with client.websocket_connect('/api/live',headers={'origin':'http://testserver'}) as socket:
                socket.send_json([])
                self.assertEqual(socket.receive_json()['type'],'error')
                with self.assertRaises(WebSocketDisconnect): socket.receive_json()
            self.assertFalse(client.get('/api/status').json()['busy'])

    def test_second_call_is_rejected_while_first_call_is_active(self):
        app=create_app(providers=FakeProviders(),vad_factory=lambda:TurnDetector(lambda frame:False))
        with TestClient(app) as client:
            with client.websocket_connect('/api/live',headers={'origin':'http://testserver'}) as first:
                first.send_json({'type':'start','character':example().model_dump()})
                self.assertEqual(first.receive_json()['type'],'ready')
                with self.assertRaises(WebSocketDisconnect) as captured:
                    with client.websocket_connect('/api/live',headers={'origin':'http://testserver'}): pass
                self.assertEqual(captured.exception.code,1013)
                first.send_json({'type':'stop'})

    def test_invalid_playback_acknowledgement_is_rejected_cleanly(self):
        app=create_app(providers=FakeProviders(),vad_factory=lambda:TurnDetector(lambda frame:False))
        with TestClient(app) as client:
            with client.websocket_connect('/api/live',headers={'origin':'http://testserver'}) as socket:
                socket.send_json({'type':'start','character':example().model_dump()})
                self.assertEqual(socket.receive_json()['type'],'ready')
                socket.send_json({'type':'played','turn_id':1,'seq':[]})
                self.assertEqual(socket.receive_json()['type'],'error')


if __name__=='__main__': unittest.main()
