"""Streaming listening checks use synthetic PCM and local fake transports only."""
import asyncio
import json
import unittest
from unittest.mock import patch

import httpx

from live_voice.listening import (FRAME_BYTES, MAX_PCM_BYTES, ListeningClient,
    ListeningError, ListeningSession, StreamingTurnDetector, TurnDecision,
    _connect_without_redirects)


SPEECH=b'\x01\x00'*320
QUIET=b'\x00\x00'*320


class Socket:
    def __init__(self, *, final='A cumulative final answer.', ready=None):
        self.events=asyncio.Queue();self.sent=[];self.closed=False;self.final=final;self.ready=ready
    async def send(self, value):
        self.sent.append(value)
        if isinstance(value,str):
            kind=json.loads(value)['type']
            if kind=='start':self.events.put_nowait(json.dumps(self.ready or {'type':'ready','chunk_ms':560,'sample_rate':16000}))
            if kind=='finish' and self.final is not None:self.events.put_nowait(json.dumps({'type':'final','text':self.final}))
    def __aiter__(self):return self
    async def __anext__(self):
        value=await self.events.get()
        if value is None:raise StopAsyncIteration
        return value
    async def close(self):self.closed=True;self.events.put_nowait(None)


class Stream:
    def __init__(self, *, text='A cumulative recognized answer.', blocked=False, error=None):
        self.sent=[];self.closed=False;self.text=text;self.error=error;self.on_partial=None
        self.finishing=asyncio.Event();self.release=asyncio.Event()
        if not blocked:self.release.set()
    async def send(self, pcm):self.sent.append(pcm)
    async def finish(self):
        self.finishing.set();await self.release.wait()
        if self.error:raise self.error
        return self.text
    async def close(self):self.closed=True


class Client:
    def __init__(self, decisions=(), stream=None):
        self.decisions=list(decisions);self.stream=stream or Stream();self.opens=0;self.checks=[]
    async def open_stream(self,on_partial=None):
        self.opens+=1;self.stream.on_partial=on_partial;return self.stream
    async def classify(self,pcm):
        self.checks.append(pcm)
        decision=self.decisions.pop(0) if self.decisions else TurnDecision(.1,False)
        if isinstance(decision,BaseException):raise decision
        if isinstance(decision,asyncio.Future):return await decision
        return decision


async def frames(listener, pcm, number):
    for _ in range(number):
        await listener.feed(pcm);await asyncio.sleep(0)


async def settle():
    for _ in range(5):await asyncio.sleep(0)


class ListeningClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_requires_both_models_and_explicit_ready_boolean(self):
        values=iter([{'ready':True,'asr':True,'turn':True},{'ready':False,'asr':True,'turn':True},
                     {'ready':True,'asr':'true','turn':True}])
        client=ListeningClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=next(values))))
        try:
            self.assertEqual(await client.health(),{'ready':True,'asr':True,'turn':True})
            self.assertFalse((await client.health())['ready']);self.assertFalse((await client.health())['asr'])
        finally:await client.close()

    async def test_health_failures_and_redirects_do_not_read_private_bodies_or_follow_locations(self):
        requests=[]
        def serve(request):
            requests.append(request);return httpx.Response(302,headers={'location':'https://elsewhere.example/private'},text='PRIVATE_BODY')
        client=ListeningClient(transport=httpx.MockTransport(serve))
        try:
            self.assertEqual(await client.health(),{'ready':False,'asr':False,'turn':False});self.assertEqual(len(requests),1)
        finally:await client.close()

    async def test_turn_request_is_exact_raw_pcm_and_decision_is_strictly_validated(self):
        sent=[]
        def serve(request):
            sent.append(request);return httpx.Response(200,json={'probability':.91,'complete':True,'prediction':1})
        client=ListeningClient(transport=httpx.MockTransport(serve))
        try:
            self.assertEqual(await client.classify(SPEECH),TurnDecision(.91,True))
            self.assertEqual(sent[0].url.path,'/turn');self.assertEqual(sent[0].content,SPEECH)
            self.assertEqual(sent[0].headers['content-type'],'application/octet-stream')
            for pcm in (b'',b'a',b'\0'*(MAX_PCM_BYTES+2)):
                with self.assertRaises(ListeningError):await client.classify(pcm)
            self.assertEqual(len(sent),1)
        finally:await client.close()

    async def test_invalid_endpoint_json_or_upstream_failure_is_fixed_and_contains_no_source_data(self):
        for value in ({'probability':2,'complete':True},{'probability':float('nan'),'complete':False},
                      {'probability':True,'complete':False},{'probability':.9,'complete':'yes'},['PRIVATE_BODY']):
            client=ListeningClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=value)))
            try:
                with self.assertRaises(ListeningError) as error:await client.classify(SPEECH)
                self.assertEqual(error.exception.reason,'endpoint_unavailable');self.assertNotIn('PRIVATE',str(error.exception))
            finally:await client.close()
        def timeout(_):raise httpx.ReadTimeout('PRIVATE_TEXT')
        client=ListeningClient(transport=httpx.MockTransport(timeout))
        try:
            with self.assertRaises(ListeningError) as error:await client.classify(SPEECH)
            self.assertEqual(error.exception.reason,'endpoint_timeout');self.assertNotIn('PRIVATE',str(error.exception))
        finally:await client.close()

    async def test_websocket_sends_one_start_streams_every_pcm_byte_once_and_uses_cumulative_final(self):
        socket=Socket();calls=[];partials=[]
        async def connect(url,**options):calls.append((url,options));return socket
        client=ListeningClient(connect_factory=connect)
        try:
            stream=await client.open_stream(on_partial=partials.append)
            pcm=QUIET*7+SPEECH*3;await stream.send(pcm)
            socket.events.put_nowait(json.dumps({'type':'partial','text':'A cumulative'}));await settle()
            self.assertEqual(await stream.finish(),'A cumulative final answer.')
            self.assertEqual(partials,['A cumulative'])
            controls=[json.loads(item) for item in socket.sent if isinstance(item,str)]
            self.assertEqual(controls,[{'type':'start','chunk_ms':560},{'type':'finish'}])
            binary=[item for item in socket.sent if isinstance(item,bytes)]
            self.assertEqual(b''.join(binary),pcm);self.assertTrue(all(640<=len(item)<=2560 for item in binary))
            self.assertEqual(calls[0][0],'ws://127.0.0.1:18095/stream');self.assertIsNone(calls[0][1]['proxy'])
            await stream.close();self.assertTrue(socket.closed)
        finally:await client.close()

    async def test_websocket_error_or_oversized_transcript_cannot_be_used_as_final_text(self):
        for event in ({'type':'error','message':'PRIVATE_TRANSCRIPT'}, {'type':'partial','text':'x'*4001},'bad json'):
            socket=Socket(final=None)
            async def connect(*_,**__):return socket
            client=ListeningClient(connect_factory=connect)
            try:
                stream=await client.open_stream();socket.events.put_nowait(json.dumps(event) if not isinstance(event,str) else event)
                await settle()
                with self.assertRaises(ListeningError) as error:await stream.finish()
                self.assertNotIn('PRIVATE_TRANSCRIPT',str(error.exception));await stream.close()
            finally:await client.close()

    async def test_stream_finish_timeout_and_cancel_close_without_any_unchecked_transcript(self):
        socket=Socket(final=None)
        async def connect(*_,**__):return socket
        client=ListeningClient(connect_factory=connect,finish_timeout=.05)
        try:
            stream=await client.open_stream();await stream.send(SPEECH)
            with self.assertRaises(ListeningError) as error:await stream.finish()
            self.assertEqual(error.exception.reason,'recognition_timeout');await stream.close()
            self.assertTrue(socket.closed);self.assertEqual(sum(json.loads(item)['type']=='cancel' for item in socket.sent if isinstance(item,str)),1)
        finally:await client.close()

    async def test_websocket_redirect_policy_returns_the_original_error_instead_of_a_new_url(self):
        class Connect:
            def __init__(self,*args,**kwargs):pass
        with patch('websockets.asyncio.client.connect',Connect):
            connection=_connect_without_redirects('ws://127.0.0.1:18095/stream')
            error=ValueError('PRIVATE_LOCATION')
            self.assertIs(connection.process_redirect(error),error)

    def test_remote_or_credentialled_listening_addresses_are_not_accepted(self):
        for url in ('https://example.com','http://127.0.0.1:18095/private','http://user:secret@localhost:18095',
                    'http://127.0.0.1:18095/?private=1','ws://localhost:18095'):
            with self.assertRaises(ValueError):ListeningClient(url)


class StreamingDetectorTests(unittest.TestCase):
    def test_fixed_frames_and_three_of_five_votes_keep_two_hundred_ms_preroll_independent_of_packets(self):
        data=QUIET*10+SPEECH*10+QUIET*25
        def detector():return StreamingTurnDetector(lambda frame:frame[:2]==b'\x01\x00')
        direct=detector();events=[]
        for offset in range(0,len(data),640):events.extend(direct.feed(data[offset:offset+640]))
        fragmented=detector();other=[]
        for offset in range(0,len(data),254):other.extend(fragmented.feed(data[offset:offset+254]))
        self.assertEqual(events,other);self.assertEqual(events[0],('start',QUIET*7+SPEECH*3))
        self.assertEqual(direct.audio(),QUIET*7+SPEECH*10+QUIET*25);self.assertTrue(direct.active)
        self.assertEqual(direct.silence_ms,500)

    def test_maximum_retained_turn_is_fifteen_seconds_and_reset_discards_partial_network_frames(self):
        detector=StreamingTurnDetector(lambda _:True);events=[]
        for _ in range(751):events.extend(detector.feed(SPEECH))
        self.assertEqual(len(detector.audio()),MAX_PCM_BYTES)
        self.assertEqual(sum(kind=='limit' for kind,_ in events),1)
        detector.feed(b'\0\0');detector.reset();self.assertEqual(detector.pending,b'');self.assertFalse(detector.active)


class ListeningSessionTests(unittest.IsolatedAsyncioTestCase):
    def listener(self,client,**options):
        self.started=[];self.partials=[];self.endpoints=[];self.finals=[];self.errors=[]
        callbacks={'on_start':lambda:self.started.append(True),'on_partial':self.partials.append,
            'on_endpoint':lambda *args:self.endpoints.append(args),'on_final':lambda *args:self.finals.append(args),
            'on_error':self.errors.append,**options}
        return ListeningSession(client,lambda frame:frame[:2]==b'\x01\x00',**callbacks)

    async def test_smart_endpoint_notifies_before_waiting_for_stream_finish_and_reuses_the_one_stream(self):
        stream=Stream(blocked=True);client=Client([TurnDecision(.95,True)],stream);listener=self.listener(client)
        try:
            await frames(listener,QUIET,10);await frames(listener,SPEECH,10);await frames(listener,QUIET,15);await settle()
            self.assertEqual(len(self.endpoints),1);self.assertTrue(stream.finishing.is_set());self.assertTrue(listener.input_active)
            self.assertEqual(self.finals,[]);self.assertEqual(await listener.feed(SPEECH),False)
            await stream.on_partial('A cumulative recognized');self.assertEqual(self.partials,['A cumulative recognized'])
            stream.release.set();await listener.task
            self.assertEqual(client.opens,1);self.assertEqual(len(self.finals),1);pcm,text,endpoint,metrics=self.finals[0]
            self.assertEqual(pcm,QUIET*7+SPEECH*10+QUIET*15);self.assertEqual(b''.join(stream.sent),pcm)
            self.assertEqual(text,stream.text);self.assertEqual(endpoint,300);self.assertEqual(metrics['endpoint_reason'],'smart_turn')
            self.assertEqual(metrics['turn_probability'],.95);self.assertTrue(metrics['streaming_asr']);self.assertFalse(listener.input_active)
            self.assertEqual(set(metrics),{'streaming_asr','stream_finalize_ms','endpoint_reason','turn_probability','endpoint_ms'})
        finally:await listener.close()

    async def test_late_complete_decision_after_speech_resumes_is_discarded_without_a_second_asr_stream(self):
        decision=asyncio.get_running_loop().create_future();client=Client([decision]);listener=self.listener(client)
        try:
            await frames(listener,SPEECH,10);await frames(listener,QUIET,15);self.assertEqual(len(client.checks),1)
            await frames(listener,SPEECH,5);decision.set_result(TurnDecision(.99,True));await settle()
            self.assertEqual(self.endpoints,[]);self.assertEqual(self.finals,[]);self.assertTrue(listener.input_active)
            await frames(listener,QUIET,20);self.assertEqual(self.endpoints,[])
            await frames(listener,QUIET,40);await listener.task
            self.assertEqual(len(self.finals),1);self.assertEqual(self.finals[0][3]['endpoint_reason'],'pause_limit')
            self.assertEqual(client.opens,1);self.assertEqual(self.errors,[])
        finally:await listener.close()

    async def test_low_confidence_complete_rechecks_and_waits_for_explicit_bounded_pause_fallback(self):
        client=Client([TurnDecision(.55,True)]*10);listener=self.listener(client)
        try:
            await frames(listener,SPEECH,10);await frames(listener,QUIET,20)
            self.assertEqual(self.endpoints,[]);self.assertTrue(listener.input_active)
            await frames(listener,QUIET,40);await listener.task
            self.assertEqual(len(self.finals),1);self.assertEqual(self.finals[0][2],1200)
            self.assertEqual(self.finals[0][3]['endpoint_reason'],'pause_limit');self.assertGreaterEqual(len(client.checks),3)
            self.assertEqual(client.opens,1)
        finally:await listener.close()

    async def test_endpoint_timeout_has_an_explicit_fallback_reason_without_changing_recognizers(self):
        timeout=ListeningError('PRIVATE_TIMEOUT',reason='endpoint_timeout');client=Client([timeout]*10);listener=self.listener(client)
        try:
            await frames(listener,SPEECH,10);await frames(listener,QUIET,60);await listener.task
            self.assertEqual(self.finals[0][3]['endpoint_reason'],'endpoint_timeout');self.assertEqual(client.opens,1)
            self.assertEqual(self.errors,[])
        finally:await listener.close()

    async def test_feed_does_not_wait_for_pending_endpoint_inference_and_reset_invalidates_its_decision(self):
        decision=asyncio.get_running_loop().create_future();client=Client([decision]);listener=self.listener(client)
        try:
            await frames(listener,SPEECH,10);await frames(listener,QUIET,15)
            await asyncio.wait_for(listener.feed(SPEECH),.05)
            await asyncio.wait_for(listener.reset(),.1)
            self.assertFalse(listener.input_active);self.assertTrue(client.stream.closed);self.assertEqual(self.finals,[])
            self.assertEqual(self.endpoints,[]);self.assertEqual(self.errors,[])
        finally:await listener.close()

    async def test_reset_while_finishing_cannot_forward_a_late_transcript_or_partial(self):
        client=Client([TurnDecision(.9,True)],Stream(blocked=True));listener=self.listener(client)
        try:
            await frames(listener,SPEECH,10);await frames(listener,QUIET,15);await settle()
            partial=client.stream.on_partial;await listener.cancel();client.stream.release.set();await partial('PRIVATE_LATE_WORDS');await settle()
            self.assertEqual(self.finals,[]);self.assertEqual(self.partials,[]);self.assertFalse(listener.input_active)
        finally:await listener.close()

    async def test_reset_from_the_final_callback_does_not_cancel_its_own_commit(self):
        client=Client([TurnDecision(.9,True)]);committed=[];listener=None
        async def final(*args):
            await listener.reset();await asyncio.sleep(0);committed.append(args[1])
        listener=self.listener(client,on_final=final)
        try:
            await frames(listener,SPEECH,10);await frames(listener,QUIET,15)
            task=listener.task
            if task:await asyncio.wait_for(task,.2)
            self.assertEqual(committed,[client.stream.text]);self.assertEqual(self.errors,[])
        finally:await listener.close()

    async def test_max_duration_ends_once_with_bounded_pcm_and_no_parallel_recognition(self):
        client=Client();listener=self.listener(client)
        try:
            await frames(listener,SPEECH,750);await listener.task
            self.assertEqual(len(self.finals),1);self.assertEqual(len(self.finals[0][0]),MAX_PCM_BYTES)
            self.assertEqual(self.finals[0][3]['endpoint_reason'],'max_duration');self.assertEqual(client.opens,1)
        finally:await listener.close()

    async def test_asr_and_non_timeout_endpoint_failures_are_visible_fail_closed_and_never_log_private_text(self):
        failures=(Client([ListeningError('PRIVATE_ENDPOINT',reason='endpoint_unavailable')]),
                  Client([TurnDecision(.9,True)],Stream(error=ListeningError('PRIVATE_ASR'))))
        for client in failures:
            listener=self.listener(client)
            try:
                await frames(listener,SPEECH,10);await frames(listener,QUIET,15);await settle()
                task=listener.task
                if task:await asyncio.wait_for(task,.2)
                self.assertEqual(self.finals,[]);self.assertEqual(len(self.errors),1);self.assertNotIn('PRIVATE',self.errors[0])
                self.assertFalse(listener.input_active);self.assertTrue(client.stream.closed)
            finally:await listener.close()


if __name__=='__main__':unittest.main()
