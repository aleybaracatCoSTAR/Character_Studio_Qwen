"""Transient voice failures may retry once without changing the spoken take."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from live_voice.inspection import RequestInspection
from live_voice.providers import LocalProviders, VoiceServiceUnavailable, VoiceTakeIncomplete
from test_voice_generation import DummySTT, PLAIN, REFERENCE, STYLED, delta, done, response


class VoiceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, providers, text=STYLED, reference=REFERENCE, notices=None):
        notices = [] if notices is None else notices
        async def notice(message): notices.append(message)
        return b''.join([part async for part in providers.audio(text, reference, on_notice=notice)])

    async def test_transient_http_statuses_recover_with_one_short_backoff_and_identical_json(self):
        for status in (408,429,502,503,504):
            requests, notices = [], []
            def serve(request):
                requests.append(json.loads(request.content))
                if len(requests)==1:
                    return httpx.Response(status, headers={'Retry-After':'3600'}, json={'error':'private failure body'})
                return response(delta()+done())
            providers = LocalProviders(DummySTT(), transport=httpx.MockTransport(serve))
            try:
                with self.subTest(status=status), patch('live_voice.providers.asyncio.sleep',new_callable=AsyncMock) as sleep:
                    pcm = await self.collect(providers, notices=notices)
                    self.assertEqual(pcm,b'\x01\x02\x03\x04')
                    self.assertEqual(len(requests),2)
                    self.assertEqual(requests[0],requests[1])
                    self.assertEqual(requests[0]['input'],STYLED)
                    self.assertEqual((requests[0]['ref_audio'],requests[0]['ref_text']),REFERENCE)
                    sleep.assert_awaited_once_with(.25)
                    self.assertEqual(len(notices),1)
                    self.assertIn('[http_status]',notices[0])
                    self.assertIn(f'HTTP {status}',notices[0])
                    self.assertTrue(notices[0].endswith('Retrying the whole reply with the same emotion and voice.'))
            finally: await providers.close()

    async def test_transient_transport_classes_recover_without_changing_input_or_reference(self):
        errors = (httpx.ConnectError,httpx.ReadError,httpx.WriteError,httpx.CloseError,
                  httpx.ConnectTimeout,httpx.ReadTimeout,httpx.WriteTimeout,httpx.PoolTimeout,httpx.RemoteProtocolError)
        for error in errors:
            requests = []
            def serve(request):
                requests.append(json.loads(request.content))
                if len(requests)==1: raise error('private transport detail',request=request)
                return response(delta()+done())
            providers = LocalProviders(DummySTT(), transport=httpx.MockTransport(serve))
            try:
                with self.subTest(error=error.__name__), patch('live_voice.providers.asyncio.sleep',new_callable=AsyncMock) as sleep:
                    self.assertEqual(await self.collect(providers),b'\x01\x02\x03\x04')
                    self.assertEqual(len(requests),2)
                    self.assertEqual(requests[0],requests[1])
                    sleep.assert_awaited_once_with(.25)
            finally: await providers.close()

    async def test_exhausted_service_failures_raise_safe_typed_error_without_partial_audio(self):
        for kind in ('status','transport'):
            requests, yielded, notices = [], [], []
            def serve(request):
                requests.append(json.loads(request.content))
                if kind=='status': return httpx.Response(503,json={'error':'PRIVATE_RESPONSE_BODY'})
                raise httpx.ConnectError('PRIVATE_CONNECTION_URL',request=request)
            providers = LocalProviders(DummySTT(),tts_url='http://private-user:private-password@private-host',
                                       transport=httpx.MockTransport(serve))
            async def notice(message): notices.append(message)
            try:
                with self.subTest(kind=kind), patch('live_voice.providers.asyncio.sleep',new_callable=AsyncMock) as sleep:
                    with self.assertRaises(VoiceServiceUnavailable) as raised:
                        async for part in providers.audio(STYLED,REFERENCE,on_notice=notice): yielded.append(part)
                    self.assertEqual(yielded,[])
                    self.assertEqual(len(requests),2)
                    self.assertEqual(requests[0],requests[1])
                    sleep.assert_awaited_once_with(.25)
                    self.assertEqual(len(notices),1)
                    self.assertIn('Drama 3 is ready',str(raised.exception))
                    self.assertIn('retry this reply',str(raised.exception))
                    for secret in ('PRIVATE_RESPONSE_BODY','PRIVATE_CONNECTION_URL','private-password',PLAIN):
                        self.assertNotIn(secret,str(raised.exception))
            finally: await providers.close()

    async def test_partial_take_disconnected_midstream_is_discarded_before_recovery(self):
        requests = []
        def serve(request):
            requests.append(json.loads(request.content))
            if len(requests)==1:
                return response(delta(b'\x99\x99'),fail=httpx.ReadError('private read error',request=request))
            return response(delta(b'\x01\x02\x03\x04')+done())
        providers = LocalProviders(DummySTT(),transport=httpx.MockTransport(serve))
        try:
            with patch('live_voice.providers.asyncio.sleep',new_callable=AsyncMock):
                self.assertEqual(await self.collect(providers),b'\x01\x02\x03\x04')
            self.assertEqual(len(requests),2)
            self.assertEqual(requests[0],requests[1])
        finally: await providers.close()

    async def test_incomplete_then_transient_failure_share_the_two_attempt_limit(self):
        requests, notices = [], []
        def serve(request):
            requests.append(json.loads(request.content))
            if len(requests)==1: return response(delta())
            return httpx.Response(503,json={'error':'unavailable'})
        providers = LocalProviders(DummySTT(),transport=httpx.MockTransport(serve))
        try:
            with patch('live_voice.providers.asyncio.sleep',new_callable=AsyncMock) as sleep:
                with self.assertRaises(VoiceServiceUnavailable): await self.collect(providers,notices=notices)
                sleep.assert_not_awaited()
            self.assertEqual(len(requests),2)
            self.assertEqual(requests[0],requests[1])
            self.assertEqual(len(notices),1)
            self.assertIn('[missing_done]',notices[0])
            self.assertTrue(notices[0].endswith('Retrying the whole reply with the same emotion and voice.'))
        finally: await providers.close()

    async def test_transient_then_incomplete_take_share_the_two_attempt_limit(self):
        requests, notices = [], []
        def serve(request):
            requests.append(json.loads(request.content))
            if len(requests)==1: return httpx.Response(502,json={'error':'unavailable'})
            return response(delta())
        providers = LocalProviders(DummySTT(),transport=httpx.MockTransport(serve))
        try:
            with patch('live_voice.providers.asyncio.sleep',new_callable=AsyncMock) as sleep:
                with self.assertRaises(VoiceTakeIncomplete): await self.collect(providers,notices=notices)
                sleep.assert_awaited_once_with(.25)
            self.assertEqual(len(requests),2)
            self.assertEqual(requests[0],requests[1])
            self.assertEqual(len(notices),1)
            self.assertIn('temporarily unavailable',notices[0])
        finally: await providers.close()

    async def test_permanent_http_failures_are_never_retried_or_backed_off(self):
        for status in (400,401,403,404,422,500,501):
            requests, notices = [], []
            def serve(request):
                requests.append(request)
                return httpx.Response(status,json={'error':'private rejection detail'})
            providers = LocalProviders(DummySTT(),transport=httpx.MockTransport(serve))
            try:
                with self.subTest(status=status), patch('live_voice.providers.asyncio.sleep',new_callable=AsyncMock) as sleep:
                    with self.assertRaises(httpx.HTTPStatusError): await self.collect(providers,notices=notices)
                    sleep.assert_not_awaited()
                    self.assertEqual(len(requests),1)
                    self.assertEqual(notices,[])
            finally: await providers.close()

    async def test_local_protocol_and_unsupported_protocol_errors_are_not_service_retries(self):
        for error in (httpx.LocalProtocolError,httpx.UnsupportedProtocol):
            requests, notices = [], []
            def serve(request):
                requests.append(request)
                raise error('invalid local configuration',request=request)
            providers = LocalProviders(DummySTT(),transport=httpx.MockTransport(serve))
            try:
                with self.subTest(error=error.__name__), patch('live_voice.providers.asyncio.sleep',new_callable=AsyncMock) as sleep:
                    with self.assertRaises(error): await self.collect(providers,notices=notices)
                    sleep.assert_not_awaited()
                    self.assertEqual(len(requests),1)
                    self.assertEqual(notices,[])
            finally: await providers.close()

    async def test_cancellation_during_backoff_stops_before_the_second_attempt(self):
        requests, notices = [], []
        def serve(request):
            requests.append(request);return httpx.Response(503,json={'error':'unavailable'})
        providers = LocalProviders(DummySTT(),transport=httpx.MockTransport(serve))
        try:
            with patch('live_voice.providers.asyncio.sleep',new_callable=AsyncMock,side_effect=asyncio.CancelledError()) as sleep:
                with self.assertRaises(asyncio.CancelledError): await self.collect(providers,notices=notices)
                sleep.assert_awaited_once_with(.25)
            self.assertEqual(len(requests),1)
            self.assertEqual(len(notices),1)
        finally: await providers.close()

    async def test_cancelled_second_take_cannot_retry_again_or_yield_its_partial_audio(self):
        requests, yielded = [], []
        def serve(request):
            requests.append(request)
            if len(requests)==1: return httpx.Response(504,json={'error':'unavailable'})
            return response(delta(b'\x99\x99'),fail=asyncio.CancelledError())
        providers = LocalProviders(DummySTT(),transport=httpx.MockTransport(serve))
        async def notice(message): pass
        try:
            with patch('live_voice.providers.asyncio.sleep',new_callable=AsyncMock):
                with self.assertRaises(asyncio.CancelledError):
                    async for part in providers.audio(STYLED,REFERENCE,on_notice=notice):yielded.append(part)
            self.assertEqual(len(requests),2)
            self.assertEqual(yielded,[])
        finally: await providers.close()

    async def test_mutating_caller_reference_during_notice_cannot_change_the_retry_voice(self):
        requests = []
        reference = list(REFERENCE)
        def serve(request):
            requests.append(json.loads(request.content))
            if len(requests)==1: return httpx.Response(503,json={'error':'unavailable'})
            return response(delta()+done())
        async def notice(message): reference[:] = ['replacement audio','replacement words']
        providers = LocalProviders(DummySTT(),transport=httpx.MockTransport(serve))
        try:
            with patch('live_voice.providers.asyncio.sleep',new_callable=AsyncMock):
                _ = [part async for part in providers.audio(STYLED,reference,on_notice=notice)]
            self.assertEqual(requests[0],requests[1])
            self.assertEqual((requests[1]['ref_audio'],requests[1]['ref_text']),REFERENCE)
        finally: await providers.close()

    async def test_inspector_records_both_identical_attempts_without_raw_reference_audio(self):
        requests, events = [], []
        def serve(request):
            requests.append(json.loads(request.content))
            if len(requests)==1: raise httpx.ConnectError('private connection detail',request=request)
            return response(delta()+done())
        async def send_json(event): events.append(event)
        inspection = RequestInspection(send_json)
        providers = LocalProviders(DummySTT(),transport=httpx.MockTransport(serve))
        try:
            with inspection.scope(5), patch('live_voice.providers.asyncio.sleep',new_callable=AsyncMock):
                await self.collect(providers)
            await inspection.subscribe(True)
            captured = events[-1]['requests']
            self.assertEqual(len(captured),2)
            for actual,snapshot in zip(requests,captured):
                actual = dict(actual);actual.pop('ref_audio')
                self.assertEqual(snapshot['payload'],actual)
                self.assertEqual(snapshot['stage'],'tts')
                self.assertEqual(snapshot['turn_id'],5)
            self.assertEqual(captured[0]['payload'],captured[1]['payload'])
            self.assertNotIn(REFERENCE[0],json.dumps(captured))
        finally: await providers.close()

    async def test_safe_retry_logs_do_not_include_private_response_input_or_endpoint(self):
        def serve(request):return httpx.Response(503,json={'error':'PRIVATE_RESPONSE_BODY'})
        providers = LocalProviders(DummySTT(),tts_url='http://private-user:private-password@private-host',
                                   transport=httpx.MockTransport(serve))
        try:
            with patch('live_voice.providers.asyncio.sleep',new_callable=AsyncMock):
                with self.assertLogs('live_voice.providers',level='WARNING') as logs:
                    with self.assertRaises(VoiceServiceUnavailable):
                        _ = [part async for part in providers.audio(STYLED,REFERENCE)]
            output = '\n'.join(logs.output)
            self.assertIn('same emotion and voice',output)
            for secret in ('PRIVATE_RESPONSE_BODY','private-password','private-host',PLAIN,REFERENCE[0]):
                self.assertNotIn(secret,output)
        finally: await providers.close()


if __name__ == '__main__':unittest.main()
