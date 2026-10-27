"""Awaiting old work must not resurrect a stale or closed narrative entry."""
import asyncio
import unittest

from live_voice.director import DirectorPlan
from live_voice.narrative import StoryBeat
from live_voice.session import LiveSession
from studio.characters import example
from test_live_voice import FakeProviders
from test_narrative import Clock, script


class BeatEntryRaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.providers = FakeProviders()
        self.events = []
        self.clock = Clock()
        self.releases = []
        self.dispatches = []
        self.second_revision = asyncio.Event()
        async def send_json(event):
            self.events.append(event)
            if event['type']=='story_state' and event.get('revision')==2:
                self.second_revision.set()
        async def send_bytes(packet): pass
        self.session = LiveSession(self.providers, example(), send_json, send_bytes,
                                   narrative=script('manual'), clock=self.clock)

    async def asyncTearDown(self):
        for release in self.releases: release.set()
        if self.dispatches: await asyncio.gather(*self.dispatches,return_exceptions=True)
        await self.session.close()

    async def slow_director(self):
        entered, cancelled, release = asyncio.Event(),asyncio.Event(),asyncio.Event()
        self.releases.append(release)
        async def waiting():
            entered.set()
            while not release.is_set():
                try: await release.wait()
                except asyncio.CancelledError: cancelled.set()
        self.session.story_director_task = asyncio.create_task(waiting())
        await asyncio.wait_for(entered.wait(),1)
        return cancelled,release

    async def reserved_entry(self):
        cancelled,release = await self.slow_director()
        self.session.story.move()
        await self.session.story_state()
        task = asyncio.create_task(self.session.reply_to_beat())
        self.dispatches.append(task)
        await asyncio.wait_for(cancelled.wait(),1)
        return task,release

    def add_third_beat(self):
        self.session.story.settings.beats.append(StoryBeat(
            title='A new choice',goal='Ask for a song.',note='Invite a duet about laundry.',
            emotion='excited',min_seconds=5,max_seconds=20))

    async def test_finishing_story_during_cancellation_discards_reserved_entry(self):
        pending,release = await self.reserved_entry()
        await self.session.story_control('next')
        self.assertTrue(self.session.story.finished)
        release.set()
        self.assertFalse(await asyncio.wait_for(pending,1))
        self.assertEqual(self.providers.spoken,[])
        self.assertIsNone(self.session.pending_beat_reply)
        self.assertIsNone(self.session.task)

    async def test_newer_revision_replaces_old_entry_instead_of_speaking_both(self):
        self.add_third_beat()
        pending,release = await self.reserved_entry()
        latest = asyncio.create_task(self.session.story_control('next'))
        self.dispatches.append(latest)
        await asyncio.wait_for(self.second_revision.wait(),1)
        release.set()
        self.assertFalse(await asyncio.wait_for(pending,1))
        await asyncio.wait_for(latest,1)
        await asyncio.wait_for(self.session.task,1)
        self.assertEqual(self.session.story.revision,2)
        self.assertEqual(len(self.providers.spoken),1)
        self.assertIn('Invite a duet about laundry.',self.providers.characters[0].beat)
        actions = [event['text'] for event in self.events if event['type']=='transcript']
        self.assertEqual(len(actions),1)
        self.assertIn('Invite a duet about laundry.',actions[0])
        self.assertNotIn('Invite one small safe choice.',actions[0])

    async def test_close_during_cancellation_prevents_a_new_generation_task(self):
        pending,release = await self.reserved_entry()
        closing = asyncio.create_task(self.session.close())
        await asyncio.sleep(0)
        self.assertTrue(self.session.closed)
        release.set()
        self.assertFalse(await asyncio.wait_for(pending,1))
        await asyncio.wait_for(closing,1)
        self.assertEqual(self.providers.spoken,[])
        self.assertIsNone(self.session.task)

    async def test_normal_participant_begin_also_aborts_when_closed_during_cancellation(self):
        cancelled,release = await self.slow_director()
        pending = asyncio.create_task(self.session.begin(text='Hello, Anna.'))
        self.dispatches.append(pending)
        await asyncio.wait_for(cancelled.wait(),1)
        closing = asyncio.create_task(self.session.close())
        await asyncio.sleep(0)
        self.assertTrue(self.session.closed)
        release.set()
        self.assertFalse(await asyncio.wait_for(pending,1))
        await asyncio.wait_for(closing,1)
        self.assertEqual(self.providers.spoken,[])
        self.assertIsNone(self.session.task)

    async def test_speech_start_during_cancellation_defers_same_entry_for_later(self):
        pending,release = await self.reserved_entry()
        revision = self.session.story.revision
        self.session.input_active = True
        release.set()
        self.assertFalse(await asyncio.wait_for(pending,1))
        self.assertEqual(self.providers.spoken,[])
        self.assertEqual(self.session.pending_beat_reply,revision)
        self.assertTrue(self.session.input_active)
        self.session.input_active = False
        self.assertTrue(await self.session.reply_to_beat())
        await asyncio.wait_for(self.session.task,1)
        self.assertEqual(len(self.providers.spoken),1)
        self.assertIsNone(self.session.pending_beat_reply)

    async def test_mic_abort_never_restores_an_older_pending_revision_over_a_newer_one(self):
        self.add_third_beat()
        pending,release = await self.reserved_entry()
        self.session.input_active = True
        self.session.story.move()
        await self.session.story_state()
        self.assertEqual(self.session.pending_beat_reply,2)
        release.set()
        self.assertFalse(await asyncio.wait_for(pending,1))
        self.assertEqual(self.session.pending_beat_reply,2)
        self.assertEqual(self.providers.spoken,[])
        self.session.input_active = False
        self.assertTrue(await self.session.reply_to_beat())
        await asyncio.wait_for(self.session.task,1)
        self.assertEqual(len(self.providers.spoken),1)
        self.assertIn('Invite a duet about laundry.',self.providers.characters[0].beat)

    async def test_participant_turn_that_advances_director_is_already_the_entry_reply(self):
        self.session.story.settings = script('director')
        self.clock.now = 12
        async def direct(*args):
            return DirectorPlan(intention='Accept help.',emotion='calm',delivery='Warm.',
                                beat_done=True,beat_evidence='I can help with the washing.')
        self.providers.direct = direct
        await self.session.begin(text='I can help with the washing.')
        await asyncio.wait_for(self.session.task,1)
        self.assertEqual(self.session.story.revision,1)
        self.assertEqual(len(self.providers.spoken),1)
        self.assertIsNone(self.session.pending_beat_reply)
        self.assertTrue(self.session.history.acknowledge(self.session.turn_id,0))
        self.assertTrue(self.session.playback_done(self.session.turn_id))
        await self.session.story_boundary()
        self.assertEqual(len(self.providers.spoken),1)
        transcripts = [event for event in self.events if event['type']=='transcript']
        self.assertEqual(len(transcripts),1)
        self.assertEqual(transcripts[0]['source'],'speech')


if __name__ == '__main__': unittest.main()
