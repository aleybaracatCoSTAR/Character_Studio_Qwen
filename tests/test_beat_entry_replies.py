"""A real beat entry should produce one reply, without cutting off a speaker."""
import asyncio
import unittest

from live_voice.director import DirectorPlan
from live_voice.providers import VoiceTakeIncomplete
from live_voice.session import LiveSession
from studio.characters import example
from test_live_voice import FakeProviders
from test_narrative import Clock, script


class BeatEntryReplyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.providers = FakeProviders(); self.events = []; self.clock = Clock()
        async def send_json(value): self.events.append(value)
        async def send_bytes(value): pass
        self.session = LiveSession(self.providers, example(), send_json, send_bytes,
                                   narrative=script('manual'), clock=self.clock)

    async def asyncTearDown(self): await self.session.close()

    async def finish(self):
        await asyncio.wait_for(self.session.task, 1)

    def played(self):
        turn = self.session.turn_id
        self.assertTrue(self.session.history.acknowledge(turn, 0))
        self.assertTrue(self.session.playback_done(turn))

    async def test_next_speaks_once_in_the_new_beat_without_a_participant_prompt(self):
        await self.session.story_control('next'); await self.finish()
        self.assertEqual(self.session.story.index, 1)
        self.assertEqual(len(self.providers.spoken), 1)
        self.assertIn('Invite one small safe choice.', self.providers.characters[-1].beat)
        self.assertEqual(next(item for item in self.events if item['type']=='transcript')['source'], 'action')
        self.assertEqual(next(item for item in self.events if item['type']=='transcript')['display_text'], 'Story cue: Trust')
        self.assertIsNone(self.session.pending_beat_reply)
        self.played()
        await self.session.story_boundary(); await self.session.story_boundary()
        self.assertEqual(len(self.providers.spoken), 1)
        self.assertNotIn('Private story cue', str(self.session.history.context_messages()))

    async def test_previous_and_restart_are_real_entries_and_each_prompt_a_reply(self):
        await self.session.story_control('next'); await self.finish(); self.played()
        await self.session.story_control('previous'); await self.finish(); self.played()
        await self.session.story_control('restart'); await self.finish()
        self.assertEqual(self.session.story.index, 0)
        self.assertEqual(len(self.providers.spoken), 3)

    async def test_invalid_moves_hold_resume_and_final_completion_do_not_invent_a_new_entry(self):
        await self.session.story_control('previous')
        await self.session.story_control('pause'); await self.session.story_control('resume')
        self.assertEqual(self.providers.spoken, [])
        await self.session.story_control('next'); await self.finish(); self.played()
        await self.session.story_control('next')
        self.assertTrue(self.session.story.finished)
        await self.session.story_control('next'); await self.session.story_boundary()
        self.assertEqual(len(self.providers.spoken), 1)

    async def test_deliberate_next_while_held_speaks_without_resuming_the_story_clock(self):
        await self.session.story_control('pause')
        await self.session.story_control('next'); await self.finish()
        self.assertIsNotNone(self.session.story.paused_at)
        self.assertEqual(len(self.providers.spoken), 1)

    async def test_timer_moves_and_speaks_when_idle(self):
        self.session.story.settings = script('timed')
        self.clock.now = 31
        self.assertTrue(await self.session.story_boundary()); await self.finish()
        self.assertEqual(self.session.story.index, 1)
        self.assertEqual(len(self.providers.spoken), 1)

    async def test_queued_move_waits_for_actual_playback_then_speaks(self):
        await self.session.begin(text='Hello'); await self.finish()
        await self.session.story_control('next')
        self.assertFalse(await self.session.story_boundary())
        self.assertEqual(self.session.story.index, 0)
        self.assertEqual(len(self.providers.spoken), 1)
        self.played()
        self.assertTrue(await self.session.story_boundary()); await self.finish()
        self.assertEqual(self.session.story.index, 1)
        self.assertEqual(len(self.providers.spoken), 2)

    async def test_participant_speech_defers_automatic_entry_and_its_final_turn_serves_the_new_beat(self):
        self.session.input_active = True
        await self.session.story_control('next')
        self.assertEqual(self.session.story.pending, 'next')
        self.assertFalse(await self.session.story_boundary())
        self.assertEqual(self.session.story.index, 0)
        await self.session.begin(text='I can help with the washing.'); await self.finish()
        self.assertEqual(self.session.story.index, 1)
        self.assertEqual(len(self.providers.spoken), 1)
        self.assertIsNone(self.session.pending_beat_reply)
        self.played(); await self.session.story_boundary()
        self.assertEqual(len(self.providers.spoken), 1)

    async def test_timer_cannot_cut_off_active_microphone_speech(self):
        self.session.story.settings = script('timed')
        self.session.input_active = True; self.clock.now = 31
        self.assertFalse(await self.session.story_boundary())
        self.assertEqual(self.session.story.index, 0)
        self.assertEqual(self.providers.spoken, [])

    async def test_background_director_can_speak_without_cancelling_its_own_task(self):
        self.session.story.settings = script('director'); self.clock.now = 12
        self.session.story.remember(0, 'I can help with the washing.', 'Thank you.')
        async def direct(*args):
            return DirectorPlan(intention='Accept help.', emotion='calm', delivery='Warm.',
                                beat_done=True, beat_evidence='I can help with the washing.')
        self.providers.direct = direct
        self.session.story_director_task = asyncio.create_task(self.session.check_story((0, ())))
        await asyncio.wait_for(self.session.story_director_task, 1); await self.finish()
        self.assertFalse(self.session.story_director_task.cancelled())
        self.assertEqual(self.session.story.index, 1)
        self.assertEqual(len(self.providers.spoken), 1)

    async def test_competing_dispatches_cannot_start_two_replies_for_one_revision(self):
        self.session.story.move(); await self.session.story_state()
        results = await asyncio.gather(self.session.reply_to_beat(), self.session.reply_to_beat())
        await self.finish()
        self.assertEqual(results.count(True), 1)
        self.assertEqual(len(self.providers.spoken), 1)

    async def test_failed_entry_voice_does_not_create_an_endless_automatic_retry_loop(self):
        attempts = []
        async def fail(text, reference=None, on_notice=None):
            attempts.append(text)
            raise VoiceTakeIncomplete('Unfinished fixture')
            yield b''
        self.providers.audio = fail
        await self.session.story_control('next'); await self.finish()
        self.assertTrue(self.events[-1]['can_retry_voice'])
        await self.session.story_boundary(); await self.session.story_boundary()
        self.assertEqual(len(attempts), 1)
        self.assertIsNone(self.session.pending_beat_reply)

    async def test_closed_session_cannot_dispatch_a_pending_entry(self):
        self.session.story.move(); await self.session.story_state()
        await self.session.close()
        self.assertFalse(await self.session.reply_to_beat())
        self.assertEqual(self.providers.spoken, [])


if __name__ == '__main__': unittest.main()
