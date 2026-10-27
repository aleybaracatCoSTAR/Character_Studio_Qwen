"""The inspector must expose submitted prompts, never reconstructed guesses."""
import asyncio
import base64
import copy
import json
import unittest

import httpx

from live_voice.inspection import RequestInspection, record_request
from live_voice.director import DirectorSettings
from live_voice.providers import LocalProviders
from live_voice.reactions import EmotionalTrigger
from live_voice.voice import VoiceSettings
from studio.characters import example


class DummySTT:
    def close(self): pass


def voice_response(output_tokens=1):
    delta = {'type':'speech.audio.delta', 'sample_rate':44100, 'channels':1,
             'response_format':'pcm', 'audio':base64.b64encode(b'\x01\x02\x03\x04').decode()}
    done = {'type':'speech.audio.done', 'usage':{'output_tokens':output_tokens}}
    wire = ''.join('data: ' + json.dumps(item) + '\n\n' for item in (delta, done))
    return httpx.Response(200, headers={'content-type':'text/event-stream'}, text=wire)


class ModelInspectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []
        async def send_json(event): self.events.append(event)
        self.inspection = RequestInspection(send_json)

    async def snapshot(self):
        await self.inspection.subscribe(True)
        self.assertEqual(self.events[-1]['type'], 'model_inspection')
        return self.events[-1]['requests']

    async def test_scope_records_without_unsolicited_trace_messages(self):
        payload = {'model':'actor-name', 'messages':[{'role':'user','content':'Hello, Anna.'}]}
        with self.inspection.scope(3):
            await record_request('actor', payload)
        self.assertEqual(self.events, [])
        requests = await self.snapshot()
        self.assertEqual(len(requests), 1)
        self.assertEqual((requests[0]['turn_id'], requests[0]['stage'], requests[0]['model']),
                         (3, 'actor', 'actor-name'))
        self.assertEqual(requests[0]['payload'], payload)

    async def test_subscribing_receives_updates_and_unsubscribing_stops_them(self):
        self.assertEqual(await self.snapshot(), [])
        with self.inspection.scope(1):
            await record_request('actor', {'model':'actor', 'messages':[]})
        self.assertEqual(len(self.events[-1]['requests']), 1)
        await self.inspection.subscribe(False)
        previous = len(self.events)
        with self.inspection.scope(2):
            await record_request('director', {'model':'director', 'messages':[]})
        self.assertEqual(len(self.events), previous)
        self.assertEqual(len(await self.snapshot()), 2)

    async def test_capture_is_detached_from_mutable_payload_and_emitted_snapshot(self):
        payload = {'model':'actor', 'messages':[{'role':'user','content':'My name is Aley.'}]}
        with self.inspection.scope(1): await record_request('actor', payload)
        payload['messages'][0]['content'] = 'Changed after the request.'
        shown = await self.snapshot()
        self.assertEqual(shown[0]['payload']['messages'][0]['content'], 'My name is Aley.')
        shown[0]['payload']['messages'][0]['content'] = 'Mutated browser-side snapshot.'
        again = await self.snapshot()
        self.assertEqual(again[0]['payload']['messages'][0]['content'], 'My name is Aley.')

    async def test_no_scope_cannot_record_into_a_finished_or_other_call(self):
        with self.inspection.scope(1):
            await record_request('actor', {'model':'actor', 'messages':[]})
        await record_request('actor', {'model':'not-this-call', 'messages':[]})
        self.assertEqual([item['model'] for item in await self.snapshot()], ['actor'])

    async def test_concurrent_sessions_and_nested_scopes_stay_separate(self):
        other_events = []
        async def send_other(event): other_events.append(event)
        other = RequestInspection(send_other)
        async def capture(target, turn, model):
            with target.scope(turn):
                await asyncio.sleep(0)
                await record_request('actor', {'model':model, 'messages':[]})
        await asyncio.gather(capture(self.inspection, 2, 'first-call'), capture(other, 9, 'second-call'))
        with self.inspection.scope(3):
            with other.scope(10, phase='between_turns'):
                await record_request('director', {'model':'second-director', 'messages':[]})
            await record_request('actor', {'model':'first-again', 'messages':[]})
        first = await self.snapshot()
        await other.subscribe(True)
        self.assertEqual([item['model'] for item in first], ['first-call', 'first-again'])
        self.assertEqual([item['model'] for item in other_events[-1]['requests']], ['second-call', 'second-director'])
        self.assertEqual(other_events[-1]['requests'][-1]['phase'], 'between_turns')

    async def test_request_count_and_turn_retention_are_bounded(self):
        for turn in range(1, 11):
            with self.inspection.scope(turn):
                for index in range(6):
                    await record_request('actor', {'model':'actor', 'messages':[{'role':'user','content':f'{turn}/{index}'}]})
        requests = await self.snapshot()
        self.assertLessEqual(len(requests), 24)
        self.assertLessEqual(len({item['turn_id'] for item in requests}), 5)
        self.assertTrue(all(item['turn_id'] >= 6 for item in requests))
        self.assertEqual(requests[-1]['payload']['messages'][0]['content'], '10/5')
        ids = [item['id'] for item in requests]
        self.assertEqual(ids, sorted(set(ids)))

    async def test_character_budget_drops_old_requests_without_silently_truncating_latest(self):
        for index in range(14):
            with self.inspection.scope(1 + index // 4):
                await record_request('actor', {'model':'actor', 'messages':[{'role':'user','content':str(index) + ':' + 'x'*30000}]})
        requests = await self.snapshot()
        self.assertLess(len(requests), 14)
        self.assertLessEqual(sum(len(json.dumps(item, ensure_ascii=False, separators=(',',':'))) for item in requests), 256000)
        self.assertEqual(requests[-1]['payload']['messages'][0]['content'], '13:' + 'x'*30000)

    async def test_oversized_request_is_explicitly_omitted_instead_of_presented_as_complete(self):
        with self.inspection.scope(1):
            await record_request('actor', {'model':'actor', 'messages':[{'role':'user','content':'private-long-input '*10000}]})
        requests = await self.snapshot()
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]['payload'], {})
        self.assertIn('size limit', requests[0]['notice'])
        self.assertNotIn('private-long-input', json.dumps(requests))

    async def test_clear_and_close_remove_old_inputs_and_closed_scope_cannot_recapture(self):
        with self.inspection.scope(1): await record_request('actor', {'model':'old-model','messages':[]})
        self.inspection.clear()
        self.assertEqual(await self.snapshot(), [])
        with self.inspection.scope(2): await record_request('actor', {'model':'fresh-model','messages':[]})
        self.assertEqual([item['model'] for item in await self.snapshot()], ['fresh-model'])
        self.inspection.close()
        self.assertEqual(self.inspection.snapshot()['requests'], [])
        count = len(self.events)
        with self.inspection.scope(3): await record_request('actor', {'model':'closed-model','messages':[]})
        await self.inspection.subscribe(True)
        self.assertEqual(len(self.events), count)
        self.assertEqual(self.inspection.snapshot()['requests'], [])

    async def test_cancelled_task_restores_scope_and_does_not_capture_later_unscoped_work(self):
        started = asyncio.Event()
        async def waiting():
            with self.inspection.scope(1):
                await record_request('actor', {'model':'before-cancel','messages':[]})
                started.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(waiting())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        await record_request('actor', {'model':'after-cancel-unscoped','messages':[]})
        self.assertEqual([item['model'] for item in await self.snapshot()], ['before-cancel'])

    async def test_subscription_requires_a_boolean(self):
        for invalid in ('true', 1, None, {}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                await self.inspection.subscribe(invalid)


class ProviderInspectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []
        self.sent = []
        async def send_json(event): self.events.append(event)
        self.inspection = RequestInspection(send_json)

    async def captured(self):
        await self.inspection.subscribe(True)
        return self.events[-1]['requests']

    async def test_actual_actor_director_and_trigger_payloads_match_transmitted_json(self):
        plan = {'intention':'Answer the visitor.', 'emotion':'calm', 'delivery':'Warm and measured.',
                'event':'none', 'trigger_action':'hold', 'evidence':'', 'beat_done':False, 'beat_evidence':''}
        def serve(request):
            payload = json.loads(request.content); self.sent.append(payload)
            if payload.get('stream'):
                return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"Hello."}}]}\n\ndata: [DONE]\n\n')
            value = {'action':'hold','evidence':''} if payload['max_tokens']==110 else plan
            return httpx.Response(200, json={'choices':[{'message':{'content':json.dumps(value)}}]})
        providers = LocalProviders(DummySTT(), llm_url='http://user:private-connection-secret@actor',
                                   director_model='director-name', llm_model='actor-name', transport=httpx.MockTransport(serve))
        try:
            with self.inspection.scope(7):
                words = [part async for part in providers.text(example(), [{'role':'user','content':'Hello.'}])]
                await providers.direct(example(), [], 'Hello.', DirectorSettings(), VoiceSettings())
                await providers.assess_trigger(example(), EmotionalTrigger(), [], 'Hello.', False)
            captured = await self.captured()
            self.assertEqual(words, ['Hello.'])
            self.assertEqual([item['stage'] for item in captured], ['actor','director','trigger'])
            self.assertEqual([item['payload'] for item in captured], self.sent)
            self.assertIn('Anna', captured[0]['payload']['messages'][0]['content'])
            self.assertNotIn('private-connection-secret', json.dumps(captured))
            self.assertTrue(all('headers' not in item and 'endpoint' not in item for item in captured))
        finally: await providers.close()

    async def test_voice_inspection_omits_audio_but_retains_exact_styled_input_reference_words_and_retry(self):
        def serve(request):
            payload = json.loads(request.content); self.sent.append(payload)
            return voice_response(payload['max_new_tokens'] if len(self.sent)==1 else 1)
        reference = ('data:audio/wav;base64,VERY_PRIVATE_REFERENCE_BYTES', 'Reference words spoken aloud.')
        styled = '<|speaker:0|> [subdued, fragile voice] I feel alone.'
        providers = LocalProviders(DummySTT(), transport=httpx.MockTransport(serve))
        try:
            with self.inspection.scope(8):
                chunks = [chunk async for chunk in providers.audio(styled, reference)]
            captured = await self.captured()
            self.assertEqual(b''.join(chunks), b'\x01\x02\x03\x04')
            self.assertEqual([item['stage'] for item in captured], ['tts','tts'])
            self.assertEqual(self.sent[0], self.sent[1])
            for item, transmitted in zip(captured, self.sent):
                expected = copy.deepcopy(transmitted); expected.pop('ref_audio')
                self.assertEqual(item['payload'], expected)
                self.assertIn('ref_audio', item['omitted_fields'])
                self.assertTrue(item['reference_audio']['present'])
                self.assertEqual(item['payload']['input'], styled)
                self.assertEqual(item['payload']['ref_text'], reference[1])
            self.assertNotIn('VERY_PRIVATE_REFERENCE_BYTES', json.dumps(captured))
        finally: await providers.close()

    async def test_failed_model_request_still_has_the_actual_attempted_payload(self):
        def serve(request):
            self.sent.append(json.loads(request.content))
            return httpx.Response(503, json={'error':'unavailable'})
        providers = LocalProviders(DummySTT(), transport=httpx.MockTransport(serve))
        try:
            with self.inspection.scope(1):
                with self.assertRaises(httpx.HTTPStatusError):
                    _ = [part async for part in providers.text(example(), [{'role':'user','content':'Hello.'}])]
            self.assertEqual([item['payload'] for item in await self.captured()], self.sent)
        finally: await providers.close()

    async def test_inspector_delivery_failure_does_not_break_valid_model_generation_or_log_prompt(self):
        async def fail_to_send(event): raise RuntimeError('Browser unavailable')
        inspection = RequestInspection(fail_to_send)
        # Subscribe without publishing an initial snapshot into the failed sink.
        inspection.subscribed = True
        def serve(request):
            self.sent.append(json.loads(request.content))
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"Hello."}}]}\n\ndata: [DONE]\n\n')
        providers = LocalProviders(DummySTT(), transport=httpx.MockTransport(serve))
        try:
            with self.assertLogs('live_voice.inspection', level='WARNING') as captured:
                with inspection.scope(1):
                    words = [part async for part in providers.text(example(), [{'role':'user','content':'private-test-input'}])]
            self.assertEqual(words, ['Hello.'])
            self.assertEqual(len(self.sent), 1)
            self.assertNotIn('private-test-input', '\n'.join(captured.output))
            self.assertNotIn('Anna', '\n'.join(captured.output))
        finally: await providers.close()

    async def test_inspector_cancellation_propagates_before_submitting_model_request(self):
        async def cancelled_send(event): raise asyncio.CancelledError()
        inspection = RequestInspection(cancelled_send)
        inspection.subscribed = True
        providers = LocalProviders(DummySTT(), transport=httpx.MockTransport(lambda request:self.fail('Request must remain cancelled')))
        try:
            with inspection.scope(1):
                with self.assertRaises(asyncio.CancelledError):
                    _ = [part async for part in providers.text(example(), [{'role':'user','content':'Hello.'}])]
        finally: await providers.close()


if __name__ == '__main__':
    unittest.main()
