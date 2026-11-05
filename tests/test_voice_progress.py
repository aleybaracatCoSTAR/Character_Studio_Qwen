"""Voice timing/progress is observable before playback, without model calls."""
import asyncio
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from live_voice import providers as provider_module
from live_voice.director import DirectorSettings
from live_voice.providers import LocalProviders, VoiceTakeIncomplete
from live_voice.session import LiveSession
from live_voice.voice import VoiceSettings
from live_voice.voice_coverage import compare_speech, spoken_text
from live_voice.voice_progress import observe_voice_progress, report_voice_progress, voice_timing_metrics
from studio.characters import example
from test_spoken_text import PASSAGE, PLAIN_PASSAGE
from test_voice_generation import REFERENCE, delta, done, response


PCM = b'\x19\x00' * 4410


class Clock:
    def __init__(self): self.value = 0
    def __call__(self): return self.value
    def advance(self, seconds): self.value += seconds


class Recognition:
    def __init__(self, replies=(), clock=None):
        self.replies = list(replies)
        self.calls = []
        self.clock = clock

    async def transcribe(self, pcm):
        self.calls.append(pcm)
        result = self.replies.pop(0)
        if self.clock: self.clock.advance(.03 if len(self.calls) == 1 else .05)
        if isinstance(result, BaseException): raise result
        return result

    def close(self): pass


class GatedSpeech(httpx.AsyncByteStream):
    def __init__(self):
        self.entered = asyncio.Event()
        self.first = asyncio.Event()
        self.first_consumed = asyncio.Event()
        self.finish = asyncio.Event()

    async def __aiter__(self):
        self.entered.set()
        await self.first.wait()
        yield delta(PCM)
        self.first_consumed.set()
        await self.finish.wait()
        yield done()


class ProgressScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_safe_timing_counters_reach_the_observer_and_nested_scopes_restore_it(self):
        outer, inner = [], []
        async def capture(event): outer.append(event)
        await report_voice_progress('generating', attempt=9)
        with observe_voice_progress(capture):
            await report_voice_progress('generating', attempt=1, audio='PRIVATE PCM', text=PASSAGE,
                                        reference=REFERENCE, first_audio_ms=True, synthesis_ms=-1,
                                        verification_ms=3_600_001)
            with observe_voice_progress(inner.append):
                await report_voice_progress('checking', attempt=1, synthesis_ms=24)
            await report_voice_progress('ready', attempt=1, synthesis_ms=24, verification_ms=38,
                                        first_audio_ms=12)
            await report_voice_progress('PRIVATE STAGE', attempt=2)
        await report_voice_progress('ready', attempt=10)
        self.assertEqual(outer, [{'stage':'generating','attempt':1},
            {'stage':'ready','attempt':1,'synthesis_ms':24,'verification_ms':38,'first_audio_ms':12}])
        self.assertEqual(inner, [{'stage':'checking','attempt':1,'synthesis_ms':24}])
        self.assertEqual(voice_timing_metrics({**outer[-1], 'raw':PASSAGE, 'elapsed_ms':999}),
            {'voice_attempts':1,'voice_synthesis_ms':24,'voice_verification_ms':38,
             'voice_model_first_audio_ms':12})
        self.assertEqual(voice_timing_metrics({'attempt':True,'synthesis_ms':-1,
                                               'verification_ms':3_600_001}), {})

    async def test_concurrent_turn_observers_and_cancelled_scope_cannot_receive_each_others_progress(self):
        received = {'first':[], 'second':[]}
        started = asyncio.Event()
        release = asyncio.Event()
        async def cancelled_turn():
            try:
                with observe_voice_progress(received['first'].append):
                    await report_voice_progress('generating', attempt=1)
                    started.set()
                    await asyncio.Event().wait()
            finally:
                # The cancelled scope must already have restored its parent.
                await report_voice_progress('ready', attempt=99)
        async def other_turn():
            with observe_voice_progress(received['second'].append):
                await report_voice_progress('generating', attempt=2)
                await release.wait()
                await report_voice_progress('checking', attempt=2)
        first = asyncio.create_task(cancelled_turn())
        second = asyncio.create_task(other_turn())
        await asyncio.wait_for(started.wait(), 1)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError): await first
        release.set()
        await asyncio.wait_for(second, 1)
        await report_voice_progress('ready', attempt=100)
        self.assertEqual(received['first'], [{'stage':'generating','attempt':1}])
        self.assertEqual(received['second'], [{'stage':'generating','attempt':2},
                                             {'stage':'checking','attempt':2}])


class ProviderProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_first_audio_and_verification_wait_are_distinct_and_no_pcm_escapes_the_check(self):
        clock = Clock()
        stream = GatedSpeech()
        requests, progress, released = [], [], []
        checking, permit_check = asyncio.Event(), asyncio.Event()
        stt = Recognition()
        async def transcribe(pcm):
            stt.calls.append(pcm)
            checking.set()
            await permit_check.wait()
            return PLAIN_PASSAGE
        stt.transcribe = transcribe
        def serve(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, headers={'content-type':'text/event-stream'}, stream=stream)
        provider = LocalProviders(stt, verify_voice=True, transport=httpx.MockTransport(serve))
        async def consume():
            with observe_voice_progress(progress.append):
                async for chunk in provider.audio(PLAIN_PASSAGE, REFERENCE): released.append(chunk)
        try:
            with patch.object(provider_module, 'time', SimpleNamespace(perf_counter=clock)):
                task = asyncio.create_task(consume())
                await asyncio.wait_for(stream.entered.wait(), 1)
                self.assertEqual(progress, [{'stage':'generating','attempt':1}])
                clock.advance(.012); stream.first.set()
                await asyncio.wait_for(stream.first_consumed.wait(), 1)
                self.assertEqual(released, [])
                clock.advance(.013); stream.finish.set()
                await asyncio.wait_for(checking.wait(), 1)
                self.assertEqual([item['stage'] for item in progress], ['generating','checking'])
                self.assertEqual(progress[-1]['synthesis_ms'], 25)
                self.assertEqual(progress[-1]['first_audio_ms'], 12)
                self.assertEqual(released, [])
                clock.advance(.036); permit_check.set()
                await asyncio.wait_for(task, 1)
            self.assertEqual(b''.join(released), PCM)
            self.assertEqual(provider.last_voice_metrics,
                {'attempt':1,'synthesis_ms':25,'verification_ms':36,'first_audio_ms':12})
            self.assertEqual(progress[-1], {'stage':'ready', **provider.last_voice_metrics})
            self.assertEqual(len(requests), 1)
            visible = json.dumps(progress) + json.dumps(voice_timing_metrics(provider.last_voice_metrics))
            for secret in (PLAIN_PASSAGE, REFERENCE[0], REFERENCE[1]): self.assertNotIn(secret, visible)
        finally:
            if 'task' in locals():
                task.cancel(); await asyncio.gather(task, return_exceptions=True)
            await provider.close()

    async def test_retry_accumulates_separate_synthesis_and_check_times_without_releasing_the_bad_take(self):
        await self.retry_case(success=True)

    async def test_exhausted_retry_retains_counters_and_reports_no_ready_stage_or_audio(self):
        await self.retry_case(success=False)

    async def retry_case(self, *, success):
        clock = Clock()
        stt = Recognition(['They wrote', PLAIN_PASSAGE if success else 'They wrote'], clock)
        requests, progress, released = [], [], []
        def serve(request):
            requests.append(json.loads(request.content))
            clock.advance(.02 if len(requests) == 1 else .04)
            return response(delta(PCM) + done(), widths=(65536,))
        provider = LocalProviders(stt, verify_voice=True, transport=httpx.MockTransport(serve))
        async def consume():
            with observe_voice_progress(progress.append):
                async for chunk in provider.audio(PLAIN_PASSAGE, REFERENCE): released.append(chunk)
        try:
            with patch.object(provider_module, 'time', SimpleNamespace(perf_counter=clock)), \
                    self.assertLogs('live_voice.providers', level='WARNING') as logs:
                if success: await consume()
                else:
                    with self.assertRaises(VoiceTakeIncomplete) as raised: await consume()
                    self.assertEqual(raised.exception.reason, 'speech_mismatch')
                    self.assertEqual(raised.exception.metadata['first_audio_ms'], 40)
                    self.assertEqual(raised.exception.metadata['elapsed_ms'], 40)
            expected_stages = ['generating','checking','retrying','generating','checking']
            if success: expected_stages.append('ready')
            self.assertEqual([item['stage'] for item in progress], expected_stages)
            self.assertEqual(provider.last_voice_metrics,
                {'attempt':2,'synthesis_ms':60,'verification_ms':80,'first_audio_ms':40})
            self.assertEqual(released, [PCM] if success else [])
            self.assertEqual(len(requests), 2); self.assertEqual(requests[0], requests[1])
            self.assertEqual(progress[2]['synthesis_ms'], 20)
            self.assertEqual(progress[2]['verification_ms'], 30)
            self.assertEqual(progress[2]['attempt'], 2)
            visible = json.dumps(progress) + '\n'.join(logs.output)
            for secret in (PLAIN_PASSAGE, REFERENCE[0], REFERENCE[1]): self.assertNotIn(secret, visible)
        finally: await provider.close()

    async def test_cancellation_during_check_restores_observer_and_never_releases_audio_or_retries(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        requests, progress, released = [], [], []
        stt = Recognition()
        async def transcribe(pcm):
            entered.set()
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        stt.transcribe = transcribe
        def serve(request):
            requests.append(request)
            return response(delta(PCM) + done(), widths=(65536,))
        provider = LocalProviders(stt, verify_voice=True, transport=httpx.MockTransport(serve))
        async def consume():
            try:
                with observe_voice_progress(progress.append):
                    async for chunk in provider.audio(PLAIN_PASSAGE): released.append(chunk)
            finally: await report_voice_progress('ready', attempt=99)
        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(entered.wait(), 1); task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            self.assertTrue(cancelled.is_set()); self.assertEqual(released, [])
            self.assertEqual(len(requests), 1)
            self.assertEqual([item['stage'] for item in progress], ['generating','checking'])
        finally:
            task.cancel(); await asyncio.gather(task, return_exceptions=True)
            await provider.close()


class SessionProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_canonical_reply_failure_metrics_and_manual_retry_keep_all_61_words_reference_and_heard_history(self):
        class ActorAndVoice(LocalProviders):
            actor_calls = 0
            async def text(self, character, history):
                self.actor_calls += 1
                # Actor deltas can split presentation delimiters across packets.
                for part in (PASSAGE[:32], PASSAGE[32:181], PASSAGE[181:]): yield part
        stt = Recognition(['They wrote','They wrote',PLAIN_PASSAGE])
        requests, events, packets = [], [], []
        def serve(request):
            requests.append(json.loads(request.content))
            return response(delta(PCM) + done(), widths=(65536,))
        provider = ActorAndVoice(stt, verify_voice=True, transport=httpx.MockTransport(serve))
        async def send_json(event): events.append(copy.deepcopy(event))
        async def send_bytes(packet): packets.append(packet)
        session = LiveSession(provider, example(), send_json, send_bytes,
                              voice=VoiceSettings(emotion='sad', direction='Quiet and measured.'),
                              director=DirectorSettings(enabled=False))
        session.reference = REFERENCE
        try:
            with self.assertLogs(level='WARNING'):
                await session.begin(text='Tell me about a small kindness.')
                await asyncio.wait_for(session.task, 1)
            turn = session.turn_id
            text_done = next(item for item in events if item['type']=='text_done')
            self.assertEqual(text_done['text'], PLAIN_PASSAGE)
            self.assertEqual(compare_speech(PASSAGE, text_done['text']).matched_words, 61)
            kinds = [item['type'] for item in events]
            self.assertLess(kinds.index('text_done'), kinds.index('phrase_start'))
            failed_metrics = next(item for item in events if item['type']=='metrics' and item.get('voice_failed'))
            self.assertEqual(failed_metrics['voice_attempts'], 2)
            for key in ('voice_synthesis_ms','voice_verification_ms','voice_model_first_audio_ms'):
                self.assertIs(type(failed_metrics[key]), int)
                self.assertGreaterEqual(failed_metrics[key], 0)
            self.assertLess(events.index(failed_metrics), kinds.index('error'))
            self.assertTrue(events[-1]['can_retry_voice']); self.assertEqual(packets, [])
            self.assertEqual(session.history.messages[-1]['content'], '')
            before = len(events)
            self.assertTrue(await session.retry_voice(turn))
            await asyncio.wait_for(session.task, 1)
            retry_events = events[before:]
            self.assertEqual(provider.actor_calls, 1)
            self.assertFalse(any(item['type'] in ('text_delta','text_done','transcript','director_note') for item in retry_events))
            self.assertEqual(len(requests), 3)
            self.assertTrue(all(request == requests[0] for request in requests))
            self.assertEqual(spoken_text(requests[0]['input']), PLAIN_PASSAGE)
            self.assertEqual((requests[0]['ref_audio'], requests[0]['ref_text']), REFERENCE)
            self.assertEqual(retry_events[-1]['text'], PLAIN_PASSAGE)
            self.assertEqual(session.history.messages[-1]['content'], '')
            self.assertTrue(session.history.acknowledge(turn, 0)); self.assertTrue(session.playback_done(turn))
            self.assertEqual(session.history.messages[-1]['content'], PLAIN_PASSAGE)
            self.assertEqual(session.history.archive[-1]['assistant'], PLAIN_PASSAGE)
            for item in events:
                if item['type'] in ('voice_progress','metrics'):
                    for secret in (PASSAGE, PLAIN_PASSAGE, REFERENCE[0], REFERENCE[1]):
                        self.assertNotIn(secret, json.dumps(item))
            self.assertEqual({item['turn_id'] for item in events if item['type']=='voice_progress'}, {turn})
        finally:
            await session.close(); await provider.close()


if __name__ == '__main__': unittest.main()
