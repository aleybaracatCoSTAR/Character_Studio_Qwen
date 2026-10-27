"""ASR preparation and recalled facts must reach a real session safely."""
import asyncio
import unittest

from live_voice.director import DirectorPlan, DirectorSettings
from live_voice.session import LiveSession
from studio.characters import example


PCM = b'\x01\x00' * 120
RESUMED_PCM = PCM + b'\x02\x00' * 60


class ControlledSTT:
    def __init__(self):
        self.calls = []
        self.cancelled = []
        self.started = asyncio.Event()
        self.block_original = None
        self.fail_first = False

    async def transcribe(self, pcm):
        self.calls.append(pcm)
        self.started.set()
        try:
            if pcm == PCM and self.block_original is not None:
                await self.block_original.wait()
            if self.fail_first and len(self.calls) == 1:
                raise RuntimeError('Provisional recognition failed')
            return ('My name is Aley.' if pcm == PCM
                    else 'My name is Aley and my favorite object is a blue teacup.')
        except asyncio.CancelledError:
            self.cancelled.append(pcm)
            raise


class RecordingProviders:
    def __init__(self):
        self.stt = ControlledSTT()
        self.director_calls = []
        self.actor_calls = []
        self.audio_calls = []

    async def direct(self, character, history, latest, settings, voice, trigger=None,
                     active=False, previous=None, stage_action=False, narrative=None):
        self.director_calls.append({'history':[dict(message) for message in history],
                                    'latest':latest, 'stage_action':stage_action})
        return DirectorPlan(intention='Answer the visitor using the heard conversation.',
                            emotion='neutral', delivery='Steady conversational delivery.')

    async def text(self, character, history):
        self.actor_calls.append([dict(message) for message in history])
        yield 'I remember what you told me.'

    async def audio(self, text, reference=None, on_notice=None):
        self.audio_calls.append(text)
        yield b'\x01\x00' * 100


def heard_turn(history, turn, participant, assistant):
    history.begin(turn, participant)
    history.add_phrase(0, assistant)
    assert history.acknowledge(turn, 0)
    history.interrupt()


def contents(messages):
    return '\n'.join(message['content'] for message in messages)


class ResponsiveSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.providers = RecordingProviders()
        self.events = []
        self.packets = []
        async def send_json(value): self.events.append(value)
        async def send_bytes(value): self.packets.append(value)
        self.session = LiveSession(self.providers, example(), send_json, send_bytes,
                                   director=DirectorSettings(enabled=True))

    async def asyncTearDown(self):
        await self.session.close()

    async def finish(self):
        await asyncio.wait_for(self.session.task, 1)
        self.assertEqual(self.events[-1]['type'], 'response_done')

    async def test_preparation_starts_asr_but_cannot_create_a_conversation_turn(self):
        self.providers.stt.block_original = asyncio.Event()
        self.session.prepare_recognition(PCM)
        await self.providers.stt.started.wait()
        self.assertEqual(self.providers.stt.calls, [PCM])
        self.assertEqual(self.events, [])
        self.assertEqual(self.providers.director_calls, [])
        self.assertEqual(self.providers.actor_calls, [])
        self.assertEqual(self.providers.audio_calls, [])
        self.assertEqual(self.session.history.messages, [])
        self.assertEqual(self.session.history.archive, [])
        self.assertFalse(self.session.reply_pending)
        self.assertEqual(self.session.turn_id, 0)

    async def test_exact_final_pcm_reuses_preparation_once_then_enters_normal_turn(self):
        self.providers.stt.block_original = asyncio.Event()
        self.session.prepare_recognition(PCM)
        await self.providers.stt.started.wait()
        await self.session.begin(pcm=PCM, endpoint_ms=400)
        await asyncio.sleep(0)
        self.assertEqual(self.providers.stt.calls, [PCM])
        self.assertFalse(any(event['type']=='transcript' for event in self.events))
        self.assertEqual(self.providers.director_calls, [])
        self.providers.stt.block_original.set()
        await self.finish()
        self.assertTrue(self.session.prepared_recognition.last_reused)
        self.assertEqual(self.providers.stt.calls, [PCM])
        self.assertEqual([event['text'] for event in self.events if event['type']=='transcript'],
                         ['My name is Aley.'])
        self.assertEqual(self.providers.director_calls[0]['latest'], 'My name is Aley.')
        self.assertEqual(self.providers.actor_calls[0][-1],
                         {'role':'user', 'content':'My name is Aley.'})

    async def test_resumed_speech_uses_final_audio_and_never_inserts_provisional_words(self):
        self.providers.stt.block_original = asyncio.Event()
        self.session.prepare_recognition(PCM)
        await self.providers.stt.started.wait()
        await self.session.begin(pcm=RESUMED_PCM, endpoint_ms=400)
        await self.finish()
        final = 'My name is Aley and my favorite object is a blue teacup.'
        self.assertEqual(self.providers.stt.calls, [PCM, RESUMED_PCM])
        self.assertEqual(self.providers.stt.cancelled, [PCM])
        self.assertFalse(self.session.prepared_recognition.last_reused)
        self.assertEqual([event['text'] for event in self.events if event['type']=='transcript'], [final])
        self.assertEqual(self.providers.director_calls[0]['latest'], final)
        self.assertEqual(self.session.history.archive[0]['user'], final)

    async def test_typed_begin_clears_old_preparation_without_using_its_transcript(self):
        self.providers.stt.block_original = asyncio.Event()
        self.session.prepare_recognition(PCM)
        await self.providers.stt.started.wait()
        await self.session.begin(text='Please tell me about your work.')
        await self.finish()
        self.assertEqual(self.providers.stt.calls, [PCM])
        self.assertEqual(self.providers.stt.cancelled, [PCM])
        self.assertEqual([event['text'] for event in self.events if event['type']=='transcript'],
                         ['Please tell me about your work.'])
        self.assertNotIn('My name is Aley.', contents(self.session.history.messages))

    async def test_private_stage_action_clears_preparation_and_stays_out_of_personal_memory(self):
        self.providers.stt.block_original = asyncio.Event()
        self.session.prepare_recognition(PCM)
        await self.providers.stt.started.wait()
        action = '[Private story cue: the telephone has started ringing.]'
        await self.session.begin(text=action, action=True)
        await self.finish()
        self.assertEqual(self.providers.stt.cancelled, [PCM])
        transcript = next(event for event in self.events if event['type']=='transcript')
        self.assertEqual((transcript['source'], transcript['text']), ('action', action))
        self.assertTrue(self.providers.director_calls[0]['stage_action'])
        self.assertTrue(self.session.history.acknowledge(self.session.turn_id, 0))
        self.assertTrue(self.session.playback_done(self.session.turn_id))
        self.assertEqual(len(self.session.history.archive), 1)
        remembered = self.session.history.archive[0]
        self.assertEqual(remembered['user'], '')
        self.assertEqual(remembered['assistant'], 'I remember what you told me.')
        self.assertEqual(remembered['source'], 'stage')
        self.assertNotIn(action, str(self.session.history.archive))

    async def test_close_cancels_preparation_without_publishing_text_or_scheduling_more_work(self):
        self.providers.stt.block_original = asyncio.Event()
        self.session.prepare_recognition(PCM)
        await self.providers.stt.started.wait()
        await self.session.close()
        self.assertEqual(self.providers.stt.cancelled, [PCM])
        self.assertEqual(self.events, [])
        self.assertEqual(self.session.history.messages, [])
        self.assertFalse(self.session.prepared_recognition.prepare(RESUMED_PCM))
        self.assertEqual(self.providers.stt.calls, [PCM])

    async def test_failed_preparation_can_recover_at_final_recognition_without_a_false_turn(self):
        self.providers.stt.fail_first = True
        self.session.prepare_recognition(PCM)
        await self.providers.stt.started.wait(); await asyncio.sleep(0)
        self.assertEqual(self.events, [])
        await self.session.begin(pcm=PCM)
        await self.finish()
        self.assertEqual(self.providers.stt.calls, [PCM, PCM])
        self.assertFalse(self.session.prepared_recognition.last_reused)
        self.assertFalse(any(event['type']=='error' for event in self.events))
        self.assertEqual(len(self.providers.director_calls), 1)
        self.assertEqual(len(self.providers.actor_calls), 1)

    async def test_final_participant_words_survive_interruption_during_director_planning(self):
        planning = asyncio.Event()
        async def blocked_director(*args):
            planning.set()
            await asyncio.Event().wait()
        self.providers.direct = blocked_director
        await self.session.begin(text='My favorite flower is lavender.')
        await asyncio.wait_for(planning.wait(), 1)
        await self.session.interrupt()
        self.assertEqual(self.session.history.archive[0]['user'], 'My favorite flower is lavender.')
        self.assertEqual(self.session.history.archive[0]['assistant'], '')
        self.assertEqual(self.providers.actor_calls, [])
        self.assertFalse(self.session.reply_pending)

    async def test_actor_and_director_receive_older_heard_facts_but_no_unheard_answer(self):
        history = self.session.history
        heard_turn(history, 1, 'My name is Aley.', 'Hello, Aley.')
        history.begin(2, 'My favorite object is a blue teacup.')
        history.add_phrase(0, 'Your favorite object is a secret yellow suitcase.')
        history.interrupt()  # Generated audio was never acknowledged as played.
        for turn in range(3, 27):
            heard_turn(history, turn, f'This is small talk round {turn}.', f'I heard round {turn}.')
        self.assertNotIn('My name is Aley.', contents(history.messages))
        self.assertNotIn('blue teacup', contents(history.messages))
        question = 'Do you remember my name and my favorite object?'
        await self.session.begin(text=question)
        await self.finish()
        director_context = self.providers.director_calls[0]['history']
        actor_context = self.providers.actor_calls[0]
        for context in (director_context, actor_context):
            self.assertIn('My name is Aley.', contents(context))
            self.assertIn('blue teacup', contents(context))
            self.assertNotIn('yellow suitcase', contents(context))
        self.assertEqual(self.providers.director_calls[0]['latest'], question)
        self.assertNotIn(question, contents(director_context))
        self.assertEqual(actor_context[-1], {'role':'user', 'content':question})
