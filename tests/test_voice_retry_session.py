"""An explicit voice retry must not regenerate dialogue or invent heard memory."""
import asyncio
import copy
import struct
import unittest

from live_voice.director import DirectorSettings
from live_voice.providers import VoiceServiceUnavailable, VoiceTakeIncomplete
from live_voice.session import LiveSession
from live_voice.voice import VoiceSettings
from studio.characters import example
from test_live_voice import FakeProviders


class RetryProviders(FakeProviders):
    def __init__(self):
        super().__init__()
        self.actor_calls = self.director_calls = 0
        self.audio_calls = []
        self.outcomes = ['incomplete', 'ok']
        self.audio_started = asyncio.Event()
        self.audio_release = asyncio.Event()

    async def direct(self, *args, **kwargs):
        self.director_calls += 1
        return await super().direct(*args, **kwargs)

    async def text(self, character, history):
        self.actor_calls += 1
        async for delta in super().text(character, history): yield delta

    async def audio(self, text, reference=None, on_notice=None):
        self.audio_calls.append((text, reference))
        self.audio_started.set()
        outcome = self.outcomes.pop(0) if self.outcomes else 'ok'
        if outcome == 'incomplete': raise VoiceTakeIncomplete('Unfinished test take')
        if outcome == 'unavailable': raise VoiceServiceUnavailable()
        if outcome == 'blocked': await self.audio_release.wait()
        if outcome == 'partial':
            yield b'\x99\x99' * 10
            raise RuntimeError('Transport stopped after some audio was sent')
        yield b'\x01\x00' * 100


class VoiceRetrySessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.providers = RetryProviders()
        self.events = []
        self.packets = []
        async def send_json(event): self.events.append(event)
        async def send_bytes(packet): self.packets.append(packet)
        self.session = LiveSession(self.providers, example(), send_json, send_bytes,
                                   voice=VoiceSettings(emotion='sad', direction='Quiet, tired and measured.'),
                                   director=DirectorSettings(enabled=True))
        self.session.reference = ('data:audio/wav;base64,AAAA', 'The original reference voice.')

    async def asyncTearDown(self): await self.session.close()

    async def failed_turn(self):
        await self.session.begin(text='How are you feeling?')
        await asyncio.wait_for(self.session.task, 1)
        self.assertEqual(self.events[-1]['type'], 'error')
        self.assertTrue(self.events[-1]['can_retry_voice'])
        self.assertFalse(self.session.reply_pending)
        self.assertEqual(self.packets, [])
        return self.session.turn_id

    async def finish_retry(self, turn):
        self.assertTrue(await self.session.retry_voice(turn))
        await asyncio.wait_for(self.session.task, 1)

    async def test_retry_keeps_turn_dialogue_director_and_heard_memory_unchanged(self):
        turn = await self.failed_turn()
        old_history = copy.deepcopy(self.session.history.messages)
        boundary = len(self.events)
        await self.finish_retry(turn)
        retry_events = self.events[boundary:]
        self.assertEqual(self.providers.actor_calls, 1)
        self.assertEqual(self.providers.director_calls, 1)
        self.assertEqual(self.providers.audio_calls[0], self.providers.audio_calls[1])
        self.assertEqual(self.session.turn_id, turn)
        self.assertEqual(self.session.history.messages, old_history)
        self.assertFalse(any(item['type'] in ('transcript','text_delta','director_note') for item in retry_events))
        started = next(item for item in retry_events if item['type']=='turn_start')
        self.assertEqual(started['turn_id'], turn)
        self.assertTrue(started['voice_retry'])
        self.assertTrue(any(item['type']=='phrase_done' for item in retry_events))
        self.assertEqual(retry_events[-1]['type'], 'response_done')
        self.assertEqual({struct.unpack('!II',packet[:8]) for packet in self.packets}, {(turn,0)})
        self.assertEqual(self.session.history.messages[-1]['content'], '')
        self.assertFalse(self.session.playback_done(turn))
        self.assertTrue(self.session.history.acknowledge(turn,0))
        self.assertTrue(self.session.playback_done(turn))
        self.assertEqual(self.session.history.messages[-1]['content'], retry_events[-1]['text'])
        self.assertEqual(len(self.session.history.archive),1)

    async def test_changing_live_controls_cannot_rewrite_the_retry_emotion_or_reference(self):
        turn = await self.failed_turn()
        original = self.providers.audio_calls[0]
        self.session.voice = VoiceSettings(emotion='excited', direction='Bright and fast.')
        self.session.reference = ('different reference audio','Different reference words.')
        await self.finish_retry(turn)
        self.assertEqual(self.providers.audio_calls[-1], original)
        self.assertIn('Quiet, tired and measured.', original[0])

    async def test_retrying_an_unavailable_voice_uses_the_same_finished_reply(self):
        self.providers.outcomes = ['unavailable','ok']
        turn = await self.failed_turn()
        await self.finish_retry(turn)
        self.assertEqual(self.providers.actor_calls,1)
        self.assertEqual(self.providers.audio_calls[0],self.providers.audio_calls[1])
        self.assertEqual(self.events[-1]['type'],'response_done')

    async def test_failed_retry_remains_explicitly_retryable_without_duplicating_the_turn(self):
        self.providers.outcomes = ['incomplete','unavailable','ok']
        turn = await self.failed_turn()
        await self.finish_retry(turn)
        self.assertEqual(self.events[-1]['type'],'error')
        self.assertTrue(self.events[-1]['can_retry_voice'])
        self.assertEqual(self.packets,[])
        await self.finish_retry(turn)
        self.assertEqual(self.events[-1]['type'],'response_done')
        self.assertEqual(self.providers.actor_calls,1)
        self.assertEqual(self.providers.director_calls,1)
        self.assertEqual(len(self.session.history.messages),2)
        self.assertTrue(all(call==self.providers.audio_calls[0] for call in self.providers.audio_calls))

    async def test_successful_retry_is_not_available_while_playing_or_after_playback(self):
        turn = await self.failed_turn()
        await self.finish_retry(turn)
        self.assertFalse(await self.session.retry_voice(turn))
        self.assertEqual(len(self.providers.audio_calls),2)
        self.assertTrue(self.session.history.acknowledge(turn,0))
        self.assertTrue(self.session.playback_done(turn))
        self.assertFalse(await self.session.retry_voice(turn))
        self.assertEqual(len(self.providers.audio_calls),2)

    async def test_repeat_retry_click_cannot_start_two_voice_generations(self):
        self.providers.outcomes = ['incomplete','blocked']
        turn = await self.failed_turn()
        self.providers.audio_started.clear()
        self.assertTrue(await self.session.retry_voice(turn))
        await asyncio.wait_for(self.providers.audio_started.wait(),1)
        self.assertFalse(await self.session.retry_voice(turn))
        self.assertEqual(len(self.providers.audio_calls),2)
        self.providers.audio_release.set()
        await asyncio.wait_for(self.session.task,1)
        self.assertEqual(self.events[-1]['type'],'response_done')

    async def test_stale_turn_id_is_rejected_without_calling_any_model(self):
        turn = await self.failed_turn()
        self.assertFalse(await self.session.retry_voice(turn+1))
        self.assertFalse(await self.session.retry_voice(turn-1))
        self.assertEqual(self.providers.actor_calls,1)
        self.assertEqual(len(self.providers.audio_calls),1)
        await self.finish_retry(turn)
        self.assertEqual(self.events[-1]['type'],'response_done')

    async def test_new_participant_turn_invalidates_the_previous_retry(self):
        self.providers.outcomes = ['incomplete','incomplete','ok']
        previous = await self.failed_turn()
        await self.session.begin(text='Actually, can you help me choose a wash?')
        await asyncio.wait_for(self.session.task,1)
        current = self.session.turn_id
        self.assertNotEqual(current,previous)
        self.assertFalse(await self.session.retry_voice(previous))
        self.assertEqual(len(self.providers.audio_calls),2)
        await self.finish_retry(current)
        self.assertEqual(self.providers.actor_calls,2)
        self.assertEqual(self.providers.director_calls,2)

    async def test_interrupt_invalidates_a_failed_take_without_forgetting_the_participant(self):
        turn = await self.failed_turn()
        await self.session.interrupt()
        self.assertFalse(await self.session.retry_voice(turn))
        self.assertEqual(self.providers.actor_calls,1)
        self.assertEqual(len(self.providers.audio_calls),1)
        self.assertEqual(self.session.history.archive[0]['user'],'How are you feeling?')
        self.assertEqual(self.session.history.archive[0]['assistant'],'')

    async def test_close_invalidates_a_failed_take(self):
        turn = await self.failed_turn()
        await self.session.close()
        self.assertFalse(await self.session.retry_voice(turn))
        self.assertEqual(len(self.providers.audio_calls),1)

    async def test_story_revision_change_rejects_the_old_reply(self):
        turn = await self.failed_turn()
        self.session.story.revision += 1
        self.assertFalse(await self.session.retry_voice(turn))
        self.assertEqual(len(self.providers.audio_calls),1)

    async def test_microphone_speech_blocks_retry_until_input_is_no_longer_active(self):
        turn = await self.failed_turn()
        self.session.input_active = True
        self.assertFalse(await self.session.retry_voice(turn))
        self.assertEqual(len(self.providers.audio_calls),1)
        self.session.input_active = False
        await self.finish_retry(turn)
        self.assertEqual(self.events[-1]['type'],'response_done')

    async def test_actor_failure_does_not_offer_voice_retry(self):
        async def failed_text(character, history):
            yield 'This dialogue never finished. '
            raise RuntimeError('Actor stopped before completing its reply')
        self.providers.text = failed_text
        await self.session.begin(text='Hello.')
        await asyncio.wait_for(self.session.task,1)
        self.assertEqual(self.events[-1]['type'],'error')
        self.assertFalse(self.events[-1].get('can_retry_voice',False))
        self.assertFalse(await self.session.retry_voice(self.session.turn_id))
        self.assertEqual(self.providers.audio_calls,[])
        self.assertEqual(self.session.history.messages[-1]['content'],'')

    async def test_audio_already_sent_before_failure_cannot_be_replayed_as_a_whole_take(self):
        self.providers.outcomes = ['partial','ok']
        await self.session.begin(text='Hello.')
        await asyncio.wait_for(self.session.task,1)
        self.assertEqual(self.events[-1]['type'],'error')
        self.assertFalse(self.events[-1].get('can_retry_voice',False))
        self.assertTrue(self.packets)
        self.assertFalse(await self.session.retry_voice(self.session.turn_id))
        self.assertEqual(len(self.providers.audio_calls),1)

    async def test_cancelled_voice_generation_never_becomes_a_retry_candidate(self):
        self.providers.outcomes = ['blocked','ok']
        await self.session.begin(text='Hello.')
        await asyncio.wait_for(self.providers.audio_started.wait(),1)
        turn = self.session.turn_id
        await self.session.interrupt()
        self.assertFalse(await self.session.retry_voice(turn))
        self.assertEqual(self.packets,[])
        self.assertEqual(len(self.providers.audio_calls),1)


if __name__ == '__main__': unittest.main()
