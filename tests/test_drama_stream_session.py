"""Live Drama playback starts early without claiming partial speech was heard."""
import asyncio
import copy
import json
import struct
import unittest

import httpx

from live_voice.director import DirectorSettings
from live_voice.providers import LocalProviders
from live_voice.session import LiveSession
from live_voice.voice import VoiceSettings
from studio.characters import example
from test_voice_generation import REFERENCE, delta, done


REPLY = 'The rain is easing. Would you like to see another painting?'
FIRST_PCM = b'\xe8\x03' * 22050
LAST_PCM = b'\x18\xfc' * 4410


class GatedRecognition:
    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.words = REPLY
        self.failure = None

    async def transcribe(self, pcm):
        self.calls.append(pcm)
        self.started.set()
        await self.release.wait()
        if self.failure is not None: raise self.failure
        return self.words

    def close(self): pass


class GatedTake(httpx.AsyncByteStream):
    def __init__(self):
        self.finish = asyncio.Event()
        self.closed = False
        self.terminal = 'complete'
        self.budget = None

    async def __aiter__(self):
        yield delta(FIRST_PCM)
        await self.finish.wait()
        yield delta(LAST_PCM)
        if self.terminal != 'missing_done':
            yield done(self.budget if self.terminal == 'token_limit' else 50)

    async def aclose(self): self.closed = True


class ActorAndVoice(LocalProviders):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.actor_calls = 0

    async def text(self, character, history):
        self.actor_calls += 1
        yield REPLY[:20]
        yield REPLY[20:]


class DramaStreamSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.recognition = GatedRecognition()
        self.take = GatedTake()
        self.requests = []
        self.events = []
        self.packets = []
        self.first_audio = asyncio.Event()

        def serve(request):
            values = json.loads(request.content)
            self.requests.append(values)
            self.take.budget = values['max_new_tokens']
            return httpx.Response(200, headers={'content-type':'text/event-stream'}, stream=self.take)

        self.provider = ActorAndVoice(self.recognition, verify_voice=True, voice_playback='streaming',
                                     transport=httpx.MockTransport(serve))

        async def send_json(value): self.events.append(copy.deepcopy(value))
        async def send_bytes(value):
            self.packets.append(value)
            self.first_audio.set()

        self.session = LiveSession(self.provider, example(), send_json, send_bytes,
            director=DirectorSettings(enabled=False),
            voice=VoiceSettings(emotion='calm', direction='One gentle, measured performance.'))
        self.session.reference = REFERENCE

    async def asyncTearDown(self):
        await self.session.close()
        await self.provider.close()

    async def begin_and_hear_prefix(self):
        await self.session.begin(text='What should we look at next?')
        await asyncio.wait_for(self.first_audio.wait(), 1)
        self.assertTrue(self.packets)
        self.assertFalse(self.session.task.done())
        self.assertFalse(self.session.generation_complete)
        self.assertFalse(any(value['type']=='response_done' for value in self.events))
        self.assertEqual(self.recognition.calls, [])
        self.assertEqual(struct.unpack('!II', self.packets[0][:8]), (self.session.turn_id, 0))
        self.assertFalse(self.take.closed)
        metrics = next(value for value in self.events if value['type']=='metrics')
        self.assertFalse(metrics['voice_verification'])
        self.assertEqual(metrics['voice_pipeline'], 'drama-stream-v1')

    async def test_audio_arrives_before_done_and_check_but_memory_waits_for_complete_playback(self):
        await self.begin_and_hear_prefix()
        turn = self.session.turn_id
        self.assertFalse(self.session.history.acknowledge(turn, 0))
        self.assertFalse(self.session.playback_done(turn))
        self.take.finish.set()
        await asyncio.wait_for(self.recognition.started.wait(), 1)
        self.assertGreater(len(self.packets), 1)
        self.assertFalse(self.session.task.done())
        self.assertFalse(self.session.history.acknowledge(turn, 0))
        self.assertFalse(any(value['type']=='phrase_done' for value in self.events))
        self.recognition.release.set()
        await asyncio.wait_for(self.session.task, 1)
        self.assertEqual(self.events[-1]['type'], 'response_done')
        self.assertEqual(b''.join(packet[8:] for packet in self.packets), FIRST_PCM + LAST_PCM)
        self.assertEqual(self.session.history.messages[-1]['content'], '')
        self.assertFalse(self.session.playback_done(turn))
        self.assertTrue(self.session.history.acknowledge(turn, 0))
        self.assertTrue(self.session.playback_done(turn))
        self.assertEqual(self.session.history.messages[-1]['content'], REPLY)
        self.assertEqual(self.session.history.archive[-1]['assistant'], REPLY)
        self.assertEqual(self.provider.actor_calls, 1)
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual((request['ref_audio'], request['ref_text']), REFERENCE)
        self.assertIn('One gentle, measured performance.', request['input'])
        self.assertIn(REPLY, request['input'])
        self.assertEqual(request['input'].count('<|speaker:0|>'), 1)

    async def test_a_live_emotion_change_cannot_rewrite_the_streaming_take(self):
        await self.begin_and_hear_prefix()
        submitted = copy.deepcopy(self.requests[0])
        self.session.voice = VoiceSettings(emotion='angry', direction='A different next-turn direction.')
        self.session.reference = ('changed voice', 'Changed reference words.')
        self.take.finish.set()
        self.recognition.release.set()
        await asyncio.wait_for(self.session.task, 1)
        self.assertEqual(self.requests, [submitted])
        self.assertIn('One gentle, measured performance.', submitted['input'])
        self.assertNotIn('different next-turn', submitted['input'])
        self.assertEqual((submitted['ref_audio'], submitted['ref_text']), REFERENCE)

    async def test_interruption_cancels_the_take_and_does_not_invent_heard_memory(self):
        await self.begin_and_hear_prefix()
        turn = self.session.turn_id
        await self.session.interrupt()
        self.assertTrue(self.take.closed)
        self.assertFalse(self.session.reply_pending)
        self.assertFalse(self.session.generation_complete)
        self.assertIsNone(self.session.retryable_voice)
        self.assertFalse(self.session.history.acknowledge(turn, 0))
        self.assertEqual(self.session.history.messages[-1]['content'], '')
        self.assertEqual(self.session.history.archive[-1]['assistant'], '')
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.recognition.calls, [])
        self.assertFalse(any(value['type']=='response_done' for value in self.events))

    async def test_interruption_while_websocket_send_waits_closes_upstream_before_return(self):
        async def blocked_send(packet):
            self.packets.append(packet)
            self.first_audio.set()
            await asyncio.Event().wait()
        self.session.send_bytes = blocked_send
        await self.begin_and_hear_prefix()
        await self.session.interrupt()
        # Interruption owns cancellation to completion; a GC finalizer on a
        # later loop tick must not leave a previous GPU request alive when the
        # caller immediately starts another turn.
        self.assertTrue(self.take.closed)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.session.history.messages[-1]['content'], '')

    async def test_failure_after_some_audio_cannot_replay_the_whole_take_or_commit_it(self):
        self.take.terminal = 'missing_done'
        await self.begin_and_hear_prefix()
        turn = self.session.turn_id
        self.take.finish.set()
        await asyncio.wait_for(self.session.task, 1)
        self.assertTrue(self.packets)
        self.assertEqual(len(self.requests), 1)
        error = self.events[-1]
        self.assertEqual(error['type'], 'error')
        self.assertFalse(error['can_retry_voice'])
        self.assertFalse(await self.session.retry_voice(turn))
        self.assertIsNone(self.session.retryable_voice)
        self.assertFalse(self.session.generation_complete)
        self.assertFalse(self.session.history.acknowledge(turn, 0))
        self.assertFalse(self.session.playback_done(turn))
        self.assertEqual(self.session.history.messages[-1]['content'], '')
        self.assertEqual(self.session.history.archive[-1]['assistant'], '')
        self.assertFalse(any(value['type']=='response_done' for value in self.events))
        self.assertFalse(any(value['type']=='phrase_done' for value in self.events))

    async def test_postdelivery_word_mismatch_is_explicit_without_a_second_performance(self):
        self.recognition.words = 'The rain.'
        await self.begin_and_hear_prefix()
        self.take.finish.set()
        self.recognition.release.set()
        await asyncio.wait_for(self.session.task, 1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.provider.actor_calls, 1)
        self.assertEqual(len(self.recognition.calls), 1)
        error = self.events[-1]
        self.assertEqual(error['type'], 'error')
        self.assertFalse(error['can_retry_voice'])
        self.assertIn('speech_mismatch', error['message'])
        self.assertFalse(self.session.history.acknowledge(self.session.turn_id, 0))
        self.assertEqual(self.session.history.messages[-1]['content'], '')

    async def test_token_limit_after_audio_delivery_has_no_automatic_retry_or_false_completion(self):
        self.take.terminal = 'token_limit'
        await self.begin_and_hear_prefix()
        self.take.finish.set()
        await asyncio.wait_for(self.session.task, 1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.recognition.calls, [])
        error = self.events[-1]
        self.assertEqual(error['type'], 'error')
        self.assertIn('token_limit', error['message'])
        self.assertFalse(error['can_retry_voice'])
        self.assertFalse(self.session.generation_complete)
        self.assertFalse(self.session.history.acknowledge(self.session.turn_id, 0))
        self.assertEqual(self.session.history.messages[-1]['content'], '')
        self.assertFalse(any(value['type']=='phrase_done' for value in self.events))

    async def test_unavailable_postdelivery_check_does_not_claim_audio_was_withheld(self):
        self.recognition.failure = RuntimeError('Synthetic recognizer failure')
        await self.begin_and_hear_prefix()
        self.take.finish.set()
        self.recognition.release.set()
        await asyncio.wait_for(self.session.task, 1)
        self.assertEqual(len(self.requests), 1)
        error = self.events[-1]
        self.assertEqual(error['type'], 'error')
        self.assertFalse(error['can_retry_voice'])
        self.assertNotIn('no unchecked audio was played', error['message'].lower())
        self.assertIn('already', error['message'].lower())
        self.assertEqual(self.session.history.messages[-1]['content'], '')

