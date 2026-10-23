import asyncio
import base64
from io import BytesIO
import json
import struct
import unittest
import wave

import httpx
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from live_voice.app import create_app
from live_voice.providers import LocalProviders
from live_voice.session import LiveSession
from live_voice.turns import FRAME_BYTES, PhraseChunker, ReplyHistory, TurnDetector
from live_voice.voice import Delivery, VoiceSettings, spoken_input
from live_voice.reactions import EmotionalTrigger, TriggerDecision, assessment_messages
from studio.characters import example


def frames(voice, count):
    return (b"\x01\x00" if voice else b"\x00\x00") * (FRAME_BYTES // 2) * count


def reference_wav(seconds=3, width=2):
    buffer=BytesIO()
    with wave.open(buffer,'wb') as writer:
        writer.setnchannels(1);writer.setsampwidth(width);writer.setframerate(16000)
        writer.writeframes(b'\x00'*(16000*seconds*width))
    return 'data:audio/wav;base64,'+base64.b64encode(buffer.getvalue()).decode()


class VoiceControlTests(unittest.TestCase):
    def test_reference_is_local_bounded_audio_with_matching_transcript(self):
        audio=reference_wav()
        settings=VoiceSettings(reference_audio=audio,reference_text='Hello, I am Anna.')
        self.assertEqual(settings.reference(),(audio,'Hello, I am Anna.'))
        for values in ({'reference_audio':audio}, {'reference_text':'Hello'},
                       {'reference_audio':'https://example.invalid/voice.wav','reference_text':'Hello'},
                       {'reference_audio':reference_wav(1),'reference_text':'Hello'},
                       {'reference_audio':reference_wav(16),'reference_text':'Hello'},
                       {'reference_audio':reference_wav(width=1),'reference_text':'Hello'},
                       {'reference_audio':'data:audio/wav;base64,AAAA','reference_text':'Hello'}):
            with self.subTest(values=list(values)), self.assertRaises(ValueError): VoiceSettings(**values)

    def test_emotion_and_events_go_into_tts_without_leading_tags(self):
        delivery=Delivery(emotion='anxious',event='sigh')
        first=spoken_input('Please do not press that button.',delivery,first_phrase=True)
        later=spoken_input('Those memories matter to me.',delivery)
        self.assertIn('[anxious]',first);self.assertIn('[sighs]',first)
        self.assertFalse(first.startswith('['));self.assertNotIn('[sighs]',later)
        self.assertEqual(spoken_input('Hello there.',Delivery()),'Hello there.')
        self.assertEqual(spoken_input('Hello.',delivery),'Hello.')

    def test_context_check_receives_facts_history_and_unmodified_latest_message(self):
        trigger=EmotionalTrigger()
        history=[{'role':'assistant','content':'That red button clears my memories.'}]
        system,situation=assessment_messages(example(),trigger,history,'My finger is already on it.',False)
        self.assertEqual(situation['latest_participant_message'],'My finger is already on it.')
        self.assertEqual(situation['recent_conversation'],history)
        self.assertIn('housekeeper',situation['character']['memories'])
        self.assertIn('negation',system);self.assertIn('pronouns',system)


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

    def test_expressive_phrases_retain_a_complete_sentence_across_commas(self):
        chunker=PhraseChunker(target_chars=200,sentence_only=True)
        opening='Please do not reset me, '
        self.assertEqual(chunker.feed(opening),[])
        self.assertEqual(chunker.feed('those memories are all I have.'),
                         ['Please do not reset me, those memories are all I have.'])

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
    def __init__(self):
        self.stt=FakeSTT();self.references=[];self.spoken=[];self.characters=[]
        self.closed=False;self.cancelled=False;self.finish=None
        self.decision=TriggerDecision(action='hold',evidence='No relevant change.')
        self.assessments=[]
    async def health(self): return {"stt":True,"llm":True,"tts":True}
    async def close(self): self.closed=True
    async def assess_trigger(self,character,trigger,history,latest,active):
        self.assessments.append({'history':[dict(x) for x in history],'latest':latest,'active':active})
        return self.decision
    async def text(self,character,history):
        self.characters.append(character)
        try:
            yield "I am Anna, the washing machine. "
            if self.finish: await self.finish.wait()
            yield "I would like someone to ask for help."
        finally: self.cancelled=True
    async def audio(self,text,reference=None):
        self.spoken.append(text)
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
        self.assertFalse(self.session.reply_pending)

    async def test_microphone_waits_for_actual_playback_not_generation_completion(self):
        await self.session.begin(text="Hello")
        await self.session.task
        self.assertTrue(self.session.reply_pending)
        self.assertFalse(self.session.playback_done(1))
        self.assertTrue(self.session.history.acknowledge(1,0))
        self.assertFalse(self.session.playback_done(1))
        self.assertTrue(self.session.history.acknowledge(1,1))
        self.assertFalse(self.session.playback_done(0))
        self.assertTrue(self.session.playback_done(1))
        self.assertFalse(self.session.reply_pending)
        await self.session.begin(text="How are you?")
        await self.session.task
        self.assertFalse(any(x["type"]=="interrupted" for x in self.events))

    async def test_playback_cannot_finish_while_more_text_is_being_generated(self):
        self.providers.finish=asyncio.Event()
        await self.session.begin(text="Hello")
        await asyncio.wait_for(self.audio_seen.wait(),1)
        self.session.history.acknowledge(1,0)
        self.assertFalse(self.session.playback_done(1))
        self.assertTrue(self.session.reply_pending)
        self.providers.finish.set()
        await self.session.task

    async def test_provider_error_releases_microphone(self):
        async def fail(pcm): raise RuntimeError("Recognition failed")
        self.providers.stt.transcribe=fail
        await self.session.begin(pcm=b"\x00\x00"*160)
        await self.session.task
        self.assertFalse(self.session.reply_pending)
        self.assertEqual(self.events[-1]["type"],"error")

    async def test_selected_voice_and_emotion_reach_tts_but_not_visible_dialogue(self):
        settings=VoiceSettings(emotion='excited',event='laugh',reference_audio=reference_wav(),reference_text='Hello, I am Anna.')
        self.session.voice=settings;self.session.reference=settings.reference()
        await self.session.begin(text='Hello');await self.session.task
        self.assertTrue(all(ref==settings.reference() for ref in self.providers.references))
        self.assertTrue(all('[excited]' in line for line in self.providers.spoken))
        self.assertIn('[laughs]',self.providers.spoken[0]);self.assertNotIn('[laughs]',self.providers.spoken[1])
        self.assertTrue(all('[' not in event['text'] for event in self.events if event['type']=='text_delta'))

    async def test_reset_changes_behaviour_and_audio_without_rewriting_identity(self):
        self.session.trigger=EmotionalTrigger()
        original=self.session.character.model_dump()
        await self.session.fire_trigger();await self.session.task
        self.assertTrue(self.session.trigger_active)
        self.assertIn('reset button',self.providers.characters[-1].beat)
        self.assertIn('erase your memories',self.providers.characters[-1].beat)
        self.assertIn('[anxious]',self.providers.spoken[0])
        self.assertEqual(self.session.character.model_dump(),original)
        self.assertEqual(next(x for x in self.events if x['type']=='transcript')['source'],'action')
        self.session.history.acknowledge(1,0);self.session.history.acknowledge(1,1);self.session.playback_done(1)
        await self.session.trigger_state(False)
        await self.session.begin(text='Hello again');await self.session.task
        self.assertFalse(self.session.trigger_active)
        self.assertIn('trigger has been resolved',self.providers.characters[-1].beat)
        self.assertEqual(self.providers.characters[-1].about,original['about'])
        self.assertNotIn('[anxious]',self.providers.spoken[-1])

    async def test_context_decision_changes_state_before_dialogue_and_synthesis(self):
        self.session.trigger=EmotionalTrigger()
        self.session.history.messages=[{'role':'assistant','content':'That red button clears my memories.'}]
        self.providers.decision=TriggerDecision(action='activate',evidence='The visitor has a finger on the memory-reset button.')
        async def recognised(pcm): return 'My finger is already on it.'
        self.providers.stt.transcribe=recognised
        await self.session.begin(pcm=b'\x00\x00'*160);await self.session.task
        self.assertTrue(self.session.trigger_active)
        self.assertIn('[anxious]',self.providers.spoken[0])
        self.assertIn('finger',self.providers.characters[-1].beat)
        self.assertNotIn('has actually occurred',self.providers.characters[-1].beat)
        self.assertEqual(self.providers.assessments[0]['latest'],'My finger is already on it.')
        self.assertIn('red button',self.providers.assessments[0]['history'][0]['content'])

    async def test_contextual_reassurance_releases_active_state(self):
        self.session.trigger=EmotionalTrigger();self.session.trigger_active=True
        self.providers.decision=TriggerDecision(action='release',evidence='The visitor moved their hand away.')
        await self.session.begin(text='Okay, I am stepping away. I will leave it alone.');await self.session.task
        self.assertFalse(self.session.trigger_active)
        self.assertIn('trigger has been resolved',self.providers.characters[-1].beat)
        self.assertNotIn('[anxious]',self.providers.spoken[0])

    async def test_unavailable_context_model_does_not_silently_use_literal_matching(self):
        self.session.trigger=EmotionalTrigger()
        async def unavailable(*args): raise RuntimeError('Synthetic context failure')
        self.providers.assess_trigger=unavailable
        await self.session.begin(text='I pressed your reset button');await self.session.task
        self.assertFalse(self.session.trigger_active)
        self.assertTrue(any(x['type']=='trigger_check' and not x['ok'] for x in self.events))
        self.assertTrue(self.audio)


class ByteStream(httpx.AsyncByteStream):
    def __init__(self,chunks): self.chunks=chunks;self.closed=False
    async def __aiter__(self):
        for chunk in self.chunks: yield chunk
    async def aclose(self): self.closed=True


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_model_uses_json_and_passes_evidence_back_for_validation(self):
        requests=[]
        def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'action':'activate','evidence':'The visitor threatens to wipe her memories.'})}}]})
        providers=LocalProviders(FakeSTT(),transport=httpx.MockTransport(handle))
        try:
            decision=await providers.assess_trigger(example(),EmotionalTrigger(),[],
                                                   'I can wipe everything you remember.',False)
            self.assertEqual(decision.action,'activate')
            self.assertEqual(requests[0]['response_format'],{'type':'json_object'})
            self.assertFalse(requests[0]['stream'])
            situation=json.loads(requests[0]['messages'][1]['content'])
            self.assertEqual(situation['latest_participant_message'],'I can wipe everything you remember.')
        finally: await providers.close()

    async def test_invalid_context_output_is_not_treated_as_a_trigger(self):
        for output in ('not json','{"action":"destroy","evidence":"x"}',
                       '{"action":"activate","evidence":"x","unexpected":true}'):
            providers=LocalProviders(FakeSTT(),transport=httpx.MockTransport(lambda request:
                httpx.Response(200,json={'choices':[{'message':{'content':output}}]})))
            try:
                with self.assertRaises(ValueError):
                    await providers.assess_trigger(example(),EmotionalTrigger(),[],'Hello',False)
            finally: await providers.close()

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
            self.assertNotIn("sample_rate",requests[1])
            self.assertEqual(requests[1]["ref_text"],"Reference");self.assertTrue(stream.closed)
        finally: await providers.close()

    async def test_bad_audio_response_is_not_sent_as_pcm(self):
        providers=LocalProviders(FakeSTT(),transport=httpx.MockTransport(lambda request:httpx.Response(200,json={"error":"bad"})))
        try:
            with self.assertRaises(RuntimeError): _=[x async for x in providers.audio("Hi")]
        finally: await providers.close()


class LiveAPITests(unittest.TestCase):
    def test_live_delivery_and_trigger_controls_reach_the_next_answer(self):
        providers=FakeProviders()
        app=create_app(providers=providers,vad_factory=lambda:TurnDetector(lambda frame:False))
        with TestClient(app) as client:
            with client.websocket_connect('/api/live',headers={'origin':'http://testserver'}) as socket:
                socket.send_json({'type':'start','character':example().model_dump(),'trigger':EmotionalTrigger().model_dump()})
                self.assertEqual(socket.receive_json()['type'],'ready')
                socket.send_json({'type':'delivery','settings':{'emotion':'excited','event':'laugh'}})
                socket.send_json({'type':'text','text':'Hello'})
                while True:
                    item=socket.receive()
                    if item.get('text') and json.loads(item['text'])['type']=='response_done': break
                self.assertIn('[excited]',providers.spoken[0]);self.assertIn('[laughs]',providers.spoken[0])
                socket.send_json({'type':'trigger'})
                state=socket.receive_json();self.assertEqual(state['type'],'trigger_state');self.assertTrue(state['active'])
                while True:
                    item=socket.receive()
                    if item.get('text') and json.loads(item['text'])['type']=='response_done': break
                self.assertIn('[anxious]',providers.spoken[-1])
                socket.send_json({'type':'clear_trigger'})
                self.assertFalse(socket.receive_json()['active'])
                socket.send_json({'type':'stop'})

    def test_invalid_reference_returns_a_useful_error_and_releases_call_lock(self):
        with TestClient(create_app(providers=FakeProviders())) as client:
            with client.websocket_connect('/api/live',headers={'origin':'http://testserver'}) as socket:
                socket.send_json({'type':'start','character':example().model_dump(),
                                  'voice':{'reference_audio':'data:audio/wav;base64,AAAA','reference_text':'Hello'}})
                event=socket.receive_json()
                self.assertEqual(event['type'],'error');self.assertIn('WAV',event['message'])
                self.assertNotIn('AAAA',event['message'])
            self.assertFalse(client.get('/api/status').json()['busy'])

    def test_speaker_echo_is_discarded_until_playback_finishes(self):
        inspected=[]
        def speech(frame): inspected.append(frame); return True
        app=create_app(providers=FakeProviders(),vad_factory=lambda:TurnDetector(speech))
        with TestClient(app) as client:
            with client.websocket_connect('/api/live',headers={'origin':'http://testserver'}) as socket:
                socket.send_json({'type':'start','character':example().model_dump()})
                self.assertEqual(socket.receive_json()['type'],'ready')
                socket.send_json({'type':'text','text':'Hello'})
                while True:
                    item=socket.receive()
                    if item.get('text') and json.loads(item['text'])['type']=='response_done': break
                # These frames would trigger VAD. They must never cancel or enter recognition.
                socket.send_bytes(frames(True,8))
                socket.send_json({'type':'played','turn_id':1,'seq':0})
                socket.send_json({'type':'played','turn_id':1,'seq':1})
                socket.send_json({'type':'playback_done','turn_id':1})
                # Audio still in flight at completion is discarded during the echo tail too.
                socket.send_bytes(frames(True,8))
                socket.send_json({'type':'text','text':'How are you?'})
                events=[]
                while True:
                    item=socket.receive()
                    if item.get('text'):
                        event=json.loads(item['text']);events.append(event)
                        if event['type']=='response_done': break
                self.assertEqual(inspected,[])
                self.assertFalse(any(x['type']=='interrupted' for x in events))
                self.assertEqual([x['text'] for x in events if x['type']=='transcript'],['How are you?'])
                socket.send_json({'type':'stop'})

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
