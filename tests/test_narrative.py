import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from live_voice.deployment import assigned_devices, checkpoints, model_command, profile
from live_voice.director import DirectorPlan, director_messages, DirectorSettings
from live_voice.narrative import NarrativeSettings, NarrativeState, StoryBeat
from live_voice.session import LiveSession
from live_voice.voice import VoiceSettings
from studio.characters import example
from test_live_voice import FakeProviders


class Clock:
    now=0
    def __call__(self):return self.now


def script(mode='director'):
    return NarrativeSettings(enabled=True,mode=mode,beats=[
        StoryBeat(title='Meet',goal='Receive a practical offer of help.',note='Someone has stopped by.',min_seconds=10,max_seconds=30),
        StoryBeat(title='Trust',goal='Let them help.',note='Invite one small safe choice.',emotion='calm',min_seconds=5,max_seconds=20)])


class NarrativeTests(unittest.TestCase):
    def setUp(self):self.clock=Clock();self.state=NarrativeState(script(),self.clock)

    def test_advance_needs_minimum_time_and_real_evidence_from_current_beat(self):
        self.state.remember(0,'I can help with the washing.','That would be lovely.')
        moved,reason=self.state.completion(True,'I can help with the washing.')
        self.assertFalse(moved);self.assertIn('minimum',reason)
        self.clock.now=12
        moved,reason=self.state.completion(True,'I have already washed everything.')
        self.assertFalse(moved);self.assertIn('quotation',reason)
        moved,reason=self.state.completion(True,'I can help with the washing.')
        self.assertTrue(moved);self.assertEqual(self.state.index,1);self.assertEqual(self.state.reason,'director')
        self.clock.now=18
        moved,reason=self.state.completion(True,'I can help with the washing.')
        self.assertFalse(moved);self.assertIn('quotation',reason)

    def test_pause_freezes_clock_and_blocks_model_and_timer(self):
        self.clock.now=12;self.state.pause(True);self.clock.now=100
        self.assertEqual(self.state.age,12);self.assertFalse(self.state.timeout())
        self.assertFalse(self.state.completion(True,'I can help with the washing.','I can help with the washing.')[0])
        self.state.pause(False);self.assertEqual(self.state.age,12)
        self.clock.now=118;self.assertTrue(self.state.timeout());self.assertEqual(self.state.index,1)

    def test_manual_mode_ignores_timer_and_llm_but_performer_can_advance(self):
        state=NarrativeState(script('manual'),self.clock);self.clock.now=500
        self.assertFalse(state.timeout());self.assertFalse(state.completion(True,'Offer help.','Offer help.')[0])
        self.assertTrue(state.move());self.assertTrue(state.move());self.assertTrue(state.finished)
        self.assertFalse(state.move());state.restart();self.assertEqual(state.index,0);self.assertFalse(state.finished)

    def test_bad_bounds_empty_arc_and_unbounded_inputs_are_rejected(self):
        for kw in ({'min_seconds':30,'max_seconds':20},{'max_seconds':float('nan')},{'max_seconds':float('inf')},
                   {'title':' '},{'goal':' '},{'extra':True}):
            values={'title':'Title','goal':'Goal'};values.update(kw)
            with self.assertRaises(ValueError):StoryBeat(**values)
        with self.assertRaises(ValueError):NarrativeSettings(enabled=True)
        with self.assertRaises(ValueError):NarrativeSettings(beats=script().beats*7)

    def test_private_notes_are_bounded_and_do_not_replace_character_facts(self):
        self.assertTrue(self.state.update_note('You want to trust their practical offer.'))
        self.clock.now=10;self.assertFalse(self.state.update_note('You want a different thing.'))
        self.clock.now=25;self.assertTrue(self.state.update_note('Ask for one small safe choice.'))
        self.clock.now=50;self.assertFalse(self.state.update_note('A third push.'))
        self.assertIn('Ask for one small safe choice.',self.state.runtime_note())
        self.state.move();self.assertEqual(self.state.director_note,'')

    def test_heard_context_is_bounded_for_long_participant_messages(self):
        for index in range(20):self.state.remember(0,str(index)+'x'*1990,'y'*1900)
        self.assertLessEqual(sum(len(m['content']) for m in self.state.heard),6000)
        context=self.state.context()
        self.assertLessEqual(sum(len(m['content']) for m in context['heard_in_current_beat']),3000)

    def test_director_sees_outline_and_evidence_but_actor_gets_only_current_moment(self):
        messages=director_messages(example(),[],'Hello',DirectorSettings(),VoiceSettings(),narrative=self.state.context())
        context=json.loads(messages[1]['content'])['narrative']
        self.assertEqual(len(context['outline']),2);self.assertIn('heard_in_current_beat',context)
        self.assertNotIn('Invite one small safe choice.',self.state.runtime_note())


class NarrativeSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events=[];self.providers=FakeProviders();self.clock=Clock()
        async def send_json(value):self.events.append(value)
        async def send_bytes(value):pass
        self.session=LiveSession(self.providers,example(),send_json,send_bytes,narrative=script(),clock=self.clock)
    async def asyncTearDown(self):await self.session.close()

    async def test_beat_change_waits_for_playback_and_only_heard_words_are_evidence(self):
        await self.session.begin(text='Hello');await self.session.task
        self.clock.now=40
        self.assertFalse(await self.session.story_boundary());self.assertEqual(self.session.story.index,0)
        await self.session.story_control('next');self.assertEqual(self.session.story.pending,'next')
        self.assertEqual(self.session.story.heard,[])
        for seq in range(len(self.session.history.phrases)):self.session.history.acknowledge(1,seq)
        self.assertTrue(self.session.playback_done(1));self.assertTrue(self.session.story.heard)
        self.assertTrue(await self.session.story_boundary());self.assertEqual(self.session.story.index,1)
        self.assertEqual(self.session.story.heard,[])

    async def test_model_completion_enters_next_beat_before_acting_without_changing_saved_character(self):
        async def direct(*args):
            return DirectorPlan(intention='Finish this old goal.',emotion='angry',delivery='Loud.',
                                beat_done=True,beat_evidence='I can help with the washing.')
        self.providers.direct=direct;self.clock.now=12;original=self.session.character.model_dump()
        await self.session.begin(text='I can help with the washing.');await self.session.task
        self.assertEqual(self.session.story.index,1)
        self.assertIn('Invite one small safe choice.',self.providers.characters[-1].beat)
        self.assertNotIn('Finish this old goal.',self.providers.characters[-1].beat)
        self.assertIn('[calm]',self.providers.spoken[0]);self.assertEqual(self.session.character.model_dump(),original)

    async def test_between_turn_check_can_finish_beat_but_stale_result_cannot_skip_next_beat(self):
        self.clock.now=12;self.session.story.remember(0,'I can help with the washing.','Thank you.')
        async def direct(*args):return DirectorPlan(intention='Accept the offer.',emotion='calm',delivery='Warm.',
                                                   beat_done=True,beat_evidence='I can help with the washing.')
        self.providers.direct=direct
        await self.session.check_story((0,()))
        self.assertEqual(self.session.story.index,1)
        await self.session.check_story((0,()))
        self.assertEqual(self.session.story.index,1);self.assertFalse(self.session.story.finished)
        self.assertFalse(self.providers.spoken)

    async def test_unheard_reply_never_counts_as_beat_evidence_and_clock_is_cancelled(self):
        await self.session.start_story()
        await self.session.begin(text='Hello');await self.session.task
        self.assertEqual(self.session.story.heard,[])
        await self.session.close();self.assertTrue(self.session.story_task.done())

    async def test_new_participant_turn_cancels_a_pending_background_check(self):
        pending=asyncio.Event();cancelled=asyncio.Event()
        async def background():
            pending.set()
            try:await asyncio.Event().wait()
            finally:cancelled.set()
        self.session.story_director_task=asyncio.create_task(background());await pending.wait()
        await self.session.begin(text='Hello');await self.session.task
        self.assertTrue(cancelled.is_set());self.assertTrue(self.providers.spoken)


class DeploymentTests(unittest.TestCase):
    def test_gpu_roles_use_allocation_tokens_including_nonzero_ids_and_uuids(self):
        placement=assigned_devices(profile('quality-3gpu'),'GPU-z,GPU-x,GPU-y')
        self.assertEqual(placement,{'actor':'GPU-x','director':'GPU-y','tts':'GPU-z'})
        for visible in ('','7','7,8','7,8,8'):
            with self.assertRaises(ValueError):assigned_devices(profile('quality-3gpu'),visible)

    def test_profiles_pin_downloads_keep_old_model_and_stay_inside_per_device_budget(self):
        self.assertIn('llm',checkpoints(profile('shared')))
        for name in ('shared','quality-2gpu','quality-3gpu'):
            cfg=profile(name);budgets={}
            for role in ('actor','director'):
                if cfg[role]:
                    spec=cfg[role];budgets[spec['slot']]=budgets.get(spec['slot'],0)+spec['memory']
                    self.assertEqual(len(spec['revision']),40)
            spec=cfg['tts'];budgets[spec['slot']]=budgets.get(spec['slot'],0)+spec['main_memory']+spec['codec_memory']
            self.assertTrue(all(v<1 for v in budgets.values()))
        self.assertIn('director-32b',checkpoints(profile('quality-3gpu')))
        self.assertNotIn('director-32b',checkpoints(profile('quality-2gpu')))

    def test_actual_model_launch_commands_are_local_and_select_the_right_role(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for role in ('actor','director'):
                cfg=profile('quality-3gpu');folder=root/cfg[role]['directory'];folder.mkdir();(folder/'config.json').write_text('{}')
                command=model_command(cfg,role,root,'/venv/bin/vllm')
                self.assertEqual(command[command.index('--host')+1],'127.0.0.1')
                self.assertEqual(command[command.index('--port')+1],'18092' if role=='actor' else '18094')
                self.assertNotIn('--tensor-parallel-size',command)
                self.assertIn('bfloat16',command)

class NarrativeAPITests(unittest.TestCase):
    def test_websocket_starts_arc_holds_and_advances_and_confirms_live_note(self):
        from fastapi.testclient import TestClient
        from live_voice.app import create_app
        from live_voice.turns import TurnDetector
        providers=FakeProviders()
        app=create_app(providers=providers,vad_factory=lambda:TurnDetector(lambda frame:False))
        with TestClient(app) as client:
            with client.websocket_connect('/api/live',headers={'origin':'http://testserver'}) as socket:
                socket.send_json({'type':'start','character':example().model_dump(),'narrative':script('manual').model_dump()})
                self.assertEqual(socket.receive_json()['type'],'ready')
                state=socket.receive_json();self.assertEqual(state['type'],'story_state');self.assertEqual(state['index'],0)
                socket.send_json({'type':'story','control':{'action':'pause'}})
                self.assertTrue(socket.receive_json()['paused'])
                socket.send_json({'type':'story','control':{'action':'next'}})
                state=socket.receive_json();self.assertEqual(state['index'],1);self.assertTrue(state['paused'])
                socket.send_json({'type':'director','settings':{'enabled':False,'notes':'Ask them to choose a wash cycle.'}})
                self.assertEqual(socket.receive_json()['type'],'director_ack')
                socket.send_json({'type':'text','text':'Hello'})
                while True:
                    item=socket.receive()
                    if item.get('text') and json.loads(item['text'])['type']=='response_done':break
                self.assertIn('Ask them to choose a wash cycle.',providers.characters[-1].beat)
                self.assertIn('Invite one small safe choice.',providers.characters[-1].beat)
                socket.send_json({'type':'stop'})
            self.assertFalse(client.get('/api/status').json()['busy'])

    def test_bad_story_is_rejected_before_call_and_example_is_valid(self):
        from fastapi.testclient import TestClient
        from live_voice.app import create_app
        with TestClient(create_app(providers=FakeProviders())) as client:
            settings=NarrativeSettings.model_validate(client.get('/api/story/example').json())
            self.assertEqual(len(settings.beats),5)
            with client.websocket_connect('/api/live',headers={'origin':'http://testserver'}) as socket:
                socket.send_json({'type':'start','character':example().model_dump(),'narrative':{'enabled':True,'beats':[]}})
                self.assertEqual(socket.receive_json()['type'],'error')
            self.assertFalse(client.get('/api/status').json()['busy'])
