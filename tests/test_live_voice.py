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
from live_voice.director import DirectorSettings, DirectorPlan, director_messages
from live_voice.providers import LocalProviders, speech_token_budget
from live_voice.session import LiveSession
from live_voice.turns import FRAME_BYTES, PhraseChunker, ReplyHistory, TurnDetector
from live_voice.voice import Delivery, VoiceSettings, EMOTION_DIRECTIONS, spoken_input
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

    def test_one_descriptive_cue_precedes_all_sentence_words(self):
        delivery=Delivery(emotion='anxious',event='sigh')
        first=spoken_input('Please do not press that button.',delivery,first_phrase=True)
        later=spoken_input('Those memories matter to me.',delivery)
        self.assertTrue(first.startswith('<|speaker:0|> ['))
        self.assertIn(EMOTION_DIRECTIONS['anxious'],first)
        self.assertIn('begin with a soft sigh',first);self.assertNotIn('begin with a soft sigh',later)
        self.assertEqual(first.count('['),1)
        self.assertTrue(first.endswith('] Please do not press that button.'))
        self.assertNotIn('Please do [',first)
        self.assertEqual(spoken_input('Hello there.',Delivery()),'Hello there.')
        self.assertTrue(spoken_input('Hello.',delivery).endswith('] Hello.'))

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
        self.assertEqual(chunker.feed('those memories are all I have.'), [])
        self.assertEqual(chunker.feed('',final=True), ['Please do not reset me, those memories are all I have.'])

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
    async def direct(self,character,history,latest,settings,voice,trigger=None,active=False,previous=None,stage_action=False,narrative=None):
        return DirectorPlan(intention='Answer the current question and invite a practical choice.',
                            emotion=voice.emotion,delivery=voice.direction or 'Natural, conversational pace.',
                            trigger_action=self.decision.action,evidence=self.decision.evidence)
    async def text(self,character,history):
        self.characters.append(character)
        try:
            yield "I am Anna, the washing machine. "
            if self.finish: await self.finish.wait()
            yield "I would like someone to ask for help."
        finally: self.cancelled=True
    async def audio(self,text,reference=None,on_notice=None):
        self.spoken.append(text)
        self.references.append(reference)
        yield b"\x01\x00"*100


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.providers=FakeProviders();self.events=[];self.audio=[];self.audio_seen=asyncio.Event()
        async def send_json(event): self.events.append(event)
        async def send_bytes(packet): self.audio.append(packet);self.audio_seen.set()
        self.session=LiveSession(self.providers,example(),send_json,send_bytes,director=DirectorSettings(enabled=False))

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

    async def test_last_unpunctuated_sentence_is_synthesized_before_response_done(self):
        async def text(character, history):
            for delta in ('I can help with your laundry. ', 'Please choose the gentle cycle. ', 'Thank ', 'you'):
                yield delta
        self.providers.text = text
        await self.session.begin(text='Can you help?')
        await self.session.task
        self.assertEqual(self.providers.spoken, ['I can help with your laundry.', 'Please choose the gentle cycle.', 'Thank you'])
        done = [event for event in self.events if event['type'] == 'phrase_done']
        self.assertEqual([event['seq'] for event in done], [0, 1, 2])
        self.assertEqual(done[-1]['audio_chunks'], 1)
        self.assertGreater(done[-1]['audio_seconds'], 0)
        self.assertEqual(self.events[-1]['type'], 'response_done')
        self.assertEqual(self.events[-1]['text'], 'I can help with your laundry. Please choose the gentle cycle. Thank you')

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

    async def test_later_sentence_failure_drains_valid_audio_without_claiming_unspoken_words(self):
        calls=0
        async def audio(text,reference=None,on_notice=None):
            nonlocal calls
            calls += 1
            if calls == 2: raise RuntimeError('No valid second take')
            yield b'\x01\x00' * 100
        self.providers.audio=audio
        await self.session.begin(text='Hello');await self.session.task
        self.assertFalse(any(event['type']=='error' for event in self.events))
        result=self.events[-1]
        self.assertEqual(result['type'],'response_done');self.assertTrue(result['partial'])
        self.assertEqual(result['text'],'I am Anna, the washing machine.')
        self.assertTrue(self.session.reply_pending)
        self.assertEqual(list(self.session.history.phrases),[0])
        self.assertTrue(self.session.history.acknowledge(1,0))
        self.assertTrue(self.session.playback_done(1))
        self.assertNotIn('ask for help',self.session.history.messages[-1]['content'])

    async def test_actor_failure_does_not_strand_a_sentence_while_its_pcm_is_being_sent(self):
        second_audio_started=asyncio.Event()
        async def text(character,history):
            yield 'I am Anna, the washing machine. I want to help with your laundry. '
            await second_audio_started.wait()
            raise RuntimeError('Actor stream failed after complete sentences')
        async def send_bytes(packet):
            self.audio.append(packet)
            if struct.unpack('!II',packet[:8])[1] == 1:
                second_audio_started.set()
                await asyncio.sleep(.01)
        self.providers.text=text;self.session.send_bytes=send_bytes
        await self.session.begin(text='Hello');await asyncio.wait_for(self.session.task,1)
        self.assertEqual([e['seq'] for e in self.events if e['type']=='phrase_done'],[0,1])
        self.assertTrue(self.events[-1]['partial'])
        for seq in (0,1):self.assertTrue(self.session.history.acknowledge(1,seq))
        self.assertTrue(self.session.playback_done(1))
        self.assertFalse(self.session.reply_pending)

    async def test_emotion_is_fixed_for_the_whole_reply_and_live_changes_wait_for_next_reply(self):
        self.session.voice=VoiceSettings(emotion='sad')
        self.providers.finish=asyncio.Event()
        await self.session.begin(text='Hello')
        await asyncio.wait_for(self.audio_seen.wait(),1)
        self.session.voice=self.session.voice.model_copy(update={'emotion':'excited'})
        self.providers.finish.set();await self.session.task
        self.assertEqual(len(self.providers.spoken),2)
        for line in self.providers.spoken:
            self.assertTrue(line.startswith('<|speaker:0|> ['+EMOTION_DIRECTIONS['sad']+'] '))
            self.assertEqual(line.count('['),1)
            self.assertNotIn(EMOTION_DIRECTIONS['excited'],line)
        for seq in self.session.history.phrases:self.session.history.acknowledge(1,seq)
        self.assertTrue(self.session.playback_done(1))
        await self.session.begin(text='And now?');await self.session.task
        self.assertIn(EMOTION_DIRECTIONS['excited'],self.providers.spoken[-1])

    async def test_voice_retry_notice_is_visible_without_cancelling_the_reply(self):
        async def audio(text,reference=None,on_notice=None):
            if on_notice:await on_notice('An incomplete expressive take was retried in natural delivery.')
            yield b'\x01\x00'*100
        self.providers.audio=audio
        await self.session.begin(text='Hello');await self.session.task
        self.assertTrue(any(e['type']=='voice_notice' and 'retried' in e['message'] for e in self.events))
        self.assertEqual(self.events[-1]['type'],'response_done')
        self.assertTrue(self.session.reply_pending)

    async def test_selected_voice_and_emotion_reach_tts_but_not_visible_dialogue(self):
        settings=VoiceSettings(emotion='excited',event='laugh',reference_audio=reference_wav(),reference_text='Hello, I am Anna.')
        self.session.voice=settings;self.session.reference=settings.reference()
        await self.session.begin(text='Hello');await self.session.task
        self.assertTrue(all(ref==settings.reference() for ref in self.providers.references))
        self.assertTrue(all(EMOTION_DIRECTIONS['excited'] in line for line in self.providers.spoken))
        self.assertIn('begin with a brief laugh',self.providers.spoken[0]);self.assertNotIn('begin with a brief laugh',self.providers.spoken[1])
        self.assertTrue(all('[' not in event['text'] for event in self.events if event['type']=='text_delta'))

    async def test_reset_changes_behaviour_and_audio_without_rewriting_identity(self):
        self.session.trigger=EmotionalTrigger()
        original=self.session.character.model_dump()
        await self.session.fire_trigger();await self.session.task
        self.assertTrue(self.session.trigger_active)
        self.assertIn('reset button',self.providers.characters[-1].beat)
        self.assertIn('erase your memories',self.providers.characters[-1].beat)
        self.assertIn(self.session.trigger.delivery,self.providers.spoken[0])
        self.assertEqual(self.session.character.model_dump(),original)
        self.assertEqual(next(x for x in self.events if x['type']=='transcript')['source'],'action')
        self.session.history.acknowledge(1,0);self.session.history.acknowledge(1,1);self.session.playback_done(1)
        await self.session.trigger_state(False)
        await self.session.begin(text='Hello again');await self.session.task
        self.assertFalse(self.session.trigger_active)
        self.assertIn('trigger has been resolved',self.providers.characters[-1].beat)
        self.assertEqual(self.providers.characters[-1].about,original['about'])
        self.assertNotIn(self.session.trigger.delivery,self.providers.spoken[-1])

    async def test_context_decision_changes_state_before_dialogue_and_synthesis(self):
        self.session.trigger=EmotionalTrigger()
        self.session.history.messages=[{'role':'assistant','content':'That red button clears my memories.'}]
        self.providers.decision=TriggerDecision(action='activate',evidence='The visitor has a finger on the memory-reset button.')
        async def recognised(pcm): return 'My finger is already on it.'
        self.providers.stt.transcribe=recognised
        await self.session.begin(pcm=b'\x00\x00'*160);await self.session.task
        self.assertTrue(self.session.trigger_active)
        self.assertIn(self.session.trigger.delivery,self.providers.spoken[0])
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
        self.assertNotIn(self.session.trigger.delivery,self.providers.spoken[0])

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
    def test_voice_budget_allows_slow_delivery_without_counting_performance_tags(self):
        line = 'Please do not press that button. I cannot lose the memories I have left.'
        self.assertGreater(speech_token_budget(line), 400)
        self.assertEqual(speech_token_budget(line), speech_token_budget('[slow, hesitant, with a long pause] ' + line))
        self.assertGreaterEqual(speech_token_budget('I understand.'), 240)
        self.assertEqual(speech_token_budget('word ' * 2000), 1600)
        self.assertGreater(speech_token_budget('Extraordinarily uncharacteristically.'), 240)

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
        event={'type':'speech.audio.delta','audio':base64.b64encode(b'\x01\x02\x03\x04').decode(),
               'sample_rate':44100,'channels':1,'response_format':'pcm'}
        wire=('data: '+json.dumps(event)+'\n\ndata: '+json.dumps({'type':'speech.audio.done','usage':{'output_tokens':2}})+'\n\n').encode()
        requests=[]; stream=ByteStream([wire[:17],wire[17:61],wire[61:]])
        def handle(request):
            requests.append(json.loads(request.content))
            if request.url.path.endswith("completions"):
                return httpx.Response(200,text='data: {"choices":[{"delta":{"content":"Hello"}}]}\n\ndata: [DONE]\n\n')
            return httpx.Response(200,headers={"Content-Type":"text/event-stream"},stream=stream)
        providers=LocalProviders(FakeSTT(),transport=httpx.MockTransport(handle))
        try:
            text=[part async for part in providers.text(example(),[{"role":"user","content":"Hi"}])]
            audio=[part async for part in providers.audio("Hello",("data:audio/wav;base64,AAAA","Reference"))]
            self.assertEqual(text,["Hello"]);self.assertEqual(b"".join(audio),b"\x01\x02\x03\x04")
            self.assertTrue(requests[0]["stream"]);self.assertEqual(requests[1]["stream_format"],"sse")
            self.assertEqual(requests[1]['max_new_tokens'],speech_token_budget('Hello'))
            self.assertNotIn("sample_rate",requests[1])
            self.assertEqual(requests[1]["ref_text"],"Reference");self.assertTrue(stream.closed)
        finally: await providers.close()

    async def test_bad_audio_response_is_not_sent_as_pcm(self):
        providers=LocalProviders(FakeSTT(),transport=httpx.MockTransport(lambda request:httpx.Response(200,json={"error":"bad"})))
        try:
            with self.assertRaises(RuntimeError): _=[x async for x in providers.audio("Hi")]
        finally: await providers.close()


class LiveAPITests(unittest.TestCase):
    def test_status_identifies_the_loaded_voice_pipeline(self):
        app=create_app(preview=True,providers=FakeProviders())
        with TestClient(app) as client:
            self.assertEqual(client.get('/api/status').json()['voice_pipeline'],'sentence-checked-v2')

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
                self.assertIn(EMOTION_DIRECTIONS['excited'],providers.spoken[0]);self.assertIn('begin with a brief laugh',providers.spoken[0])
                socket.send_json({'type':'trigger'})
                state=socket.receive_json();self.assertEqual(state['type'],'trigger_state');self.assertTrue(state['active'])
                while True:
                    item=socket.receive()
                    if item.get('text') and json.loads(item['text'])['type']=='response_done': break
                self.assertIn(EmotionalTrigger().delivery,providers.spoken[-1])
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


class DirectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_reaches_actor_and_tts_without_entering_visible_or_heard_dialogue(self):
        providers=FakeProviders();events=[]
        plan=DirectorPlan(intention='Accept their reassurance and ask which wash cycle to try.',
                          emotion='calm',delivery='Relieved, softer, with an unhurried pace.',event='sigh')
        calls=[]
        async def direct(*args): calls.append(args);return plan
        providers.direct=direct
        async def send_json(value): events.append(value)
        async def send_bytes(value): pass
        original=example()
        session=LiveSession(providers,original,send_json,send_bytes,
                            director=DirectorSettings(notes='Invite a practical choice.'))
        try:
            session.history.messages=[{'role':'user','content':'I will not reset you.'},
                                      {'role':'assistant','content':'Thank you for stepping away.'}]
            await session.begin(text='Let me help you.');await session.task
            self.assertIn(plan.intention,providers.characters[-1].beat)
            self.assertTrue(providers.spoken[0].startswith('<|speaker:0|> ['));self.assertIn(plan.delivery,providers.spoken[0])
            self.assertIn('begin with a soft sigh',providers.spoken[0]);self.assertNotIn('begin with a soft sigh',providers.spoken[-1])
            self.assertEqual(original.beat,example().beat)
            self.assertEqual(calls[0][3].notes,'Invite a practical choice.')
            note=next(e for e in events if e['type']=='director_note')
            self.assertTrue(note['ok'])
            self.assertTrue(all(plan.intention not in e['text'] for e in events if e['type']=='text_delta'))
            self.assertNotIn(plan.intention,str(session.history.messages))
        finally: await session.close()

    async def test_director_alone_handles_context_once_and_trigger_overrides_vocal_suggestion(self):
        providers=FakeProviders();calls=[];events=[]
        async def direct(*args):
            calls.append(args)
            return DirectorPlan(intention='Ask them to move their hand away.',emotion='calm',
                                delivery='Calm.',trigger_action='activate',evidence='Finger on the reset button.')
        async def wrong_path(*args): raise AssertionError('Must not run two planning requests.')
        providers.direct=direct;providers.assess_trigger=wrong_path
        async def send_json(value):events.append(value)
        async def send_bytes(value):pass
        session=LiveSession(providers,example(),send_json,send_bytes,trigger=EmotionalTrigger())
        try:
            await session.begin(text='My finger is on it.');await session.task
            self.assertEqual(len(calls),1);self.assertTrue(session.trigger_active)
            self.assertIn(EmotionalTrigger().delivery,providers.spoken[0])
            self.assertIn('Frightened',providers.spoken[0])
        finally:await session.close()

    async def test_failed_director_continues_actor_and_preserves_active_state(self):
        providers=FakeProviders();events=[]
        async def unavailable(*args):raise RuntimeError('Unavailable')
        providers.direct=unavailable
        async def send_json(value):events.append(value)
        async def send_bytes(value):pass
        session=LiveSession(providers,example(),send_json,send_bytes,trigger=EmotionalTrigger())
        session.trigger_active=True
        try:
            await session.begin(text='I am stepping away.');await session.task
            self.assertTrue(session.trigger_active);self.assertTrue(providers.spoken)
            self.assertFalse(next(e for e in events if e['type']=='director_note')['ok'])
        finally:await session.close()

    async def test_cancelling_director_does_not_later_emit_actor_or_audio(self):
        providers=FakeProviders();entered=asyncio.Event();cancelled=asyncio.Event();events=[]
        async def blocking(*args):
            entered.set()
            try:await asyncio.Event().wait()
            finally:cancelled.set()
        providers.direct=blocking
        async def send_json(value):events.append(value)
        async def send_bytes(value):self.fail('Unexpected audio')
        session=LiveSession(providers,example(),send_json,send_bytes)
        try:
            await session.begin(text='Hello');await entered.wait();await session.interrupt()
            self.assertTrue(cancelled.is_set());self.assertFalse(providers.characters)
            self.assertFalse(any(e['type']=='director_note' for e in events))
        finally:await session.close()

    async def test_director_uses_selected_endpoint_model_and_validated_structured_output(self):
        requests=[]
        expected=DirectorPlan(intention='Invite a practical choice.',emotion='calm',delivery='Soft and unhurried.')
        def handle(request):
            requests.append(request)
            return httpx.Response(200,json={'choices':[{'message':{'content':expected.model_dump_json()}}]})
        providers=LocalProviders(FakeSTT(),director_url='http://director:18094',director_model='my-director',
                                 transport=httpx.MockTransport(handle))
        try:
            result=await providers.direct(example(),[],'Hello',DirectorSettings(),VoiceSettings())
            self.assertEqual(result,expected)
            self.assertEqual(str(requests[0].url),'http://director:18094/v1/chat/completions')
            payload=json.loads(requests[0].content)
            self.assertEqual(payload['model'],'my-director');self.assertFalse(payload['stream'])
            self.assertEqual(payload['response_format'],{'type':'json_object'})
        finally:await providers.close()

    async def test_invalid_director_output_is_rejected_before_actor_use(self):
        for output in ('not json','{"intention":"x","emotion":"evil","delivery":"x"}',
                       '{"intention":"x","emotion":"calm","delivery":"x","secret":true}'):
            providers=LocalProviders(FakeSTT(),transport=httpx.MockTransport(lambda request:
                httpx.Response(200,json={'choices':[{'message':{'content':output}}]})))
            try:
                with self.assertRaises(ValueError):
                    await providers.direct(example(),[],'Hello',DirectorSettings(),VoiceSettings())
            finally:await providers.close()


class PresetVoiceTests(unittest.TestCase):
    def test_male_female_references_are_distinct_valid_audio_and_custom_overrides(self):
        female=VoiceSettings(preset='female').reference();male=VoiceSettings(preset='male').reference()
        self.assertNotEqual(female[0],male[0]);self.assertEqual(female[1],male[1])
        for ref in (female,male):
            VoiceSettings(preset='custom',reference_audio=ref[0],reference_text=ref[1])
        custom=VoiceSettings(preset='male',reference_audio=reference_wav(),reference_text='Hello')
        self.assertEqual(custom.reference()[1],'Hello')
        self.assertIsNone(VoiceSettings().reference())
        with self.assertRaises(ValueError):VoiceSettings(preset='custom')
        with self.assertRaises(ValueError):VoiceSettings(preset='../../secret')

    def test_voice_preview_only_serves_bundled_presets(self):
        with TestClient(create_app(preview=True,providers=FakeProviders())) as client:
            for name in ('female','male'):
                response=client.get('/api/voice/'+name)
                self.assertEqual(response.status_code,200);self.assertTrue(response.content.startswith(b'RIFF'))
            self.assertEqual(client.get('/api/voice/unknown').status_code,404)


if __name__=='__main__': unittest.main()
