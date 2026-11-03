"""Portal integration uses the real listening controller and fake local models."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from live_voice.app import create_app
from live_voice.archive import ArchiveStore
from live_voice.listening import ListeningError, TurnDecision
from live_voice.turns import TurnDetector
from studio.characters import example
from test_live_voice import FakeProviders, frames


class ActorProviders(FakeProviders):
    def __init__(self):
        super().__init__();self.histories=[];self.whisper_calls=[]
        async def forbidden(pcm):
            self.whisper_calls.append(pcm)
            raise AssertionError('Input must not be decoded again by Whisper.')
        self.stt.transcribe=forbidden
    async def text(self,character,history,**kwargs):
        self.histories.append(deepcopy(history))
        async for word in super().text(character,history):yield word


class StreamingInput:
    def __init__(self, owner):
        self.owner=owner;self.sent=[];self.on_partial=None
        self.closed=threading.Event();self.finishing=threading.Event();self.cancelled=threading.Event()
    async def send(self,pcm):self.sent.append(pcm)
    async def finish(self):
        self.finishing.set()
        try:
            while not self.owner.finish_release.is_set():await asyncio.sleep(.001)
            if self.owner.finish_error:raise self.owner.finish_error
            return self.owner.final_text
        except asyncio.CancelledError:
            self.cancelled.set();raise
    async def close(self):self.closed.set()


class StreamingClient:
    def __init__(self, *, ready=True, blocked_finish=False, blocked_endpoint=False):
        self.ready=ready;self.streams=[];self.checks=[];self.loop=None;self.closed=False;self.health_calls=0
        self.opened=threading.Event();self.endpoint_started=threading.Event();self.endpoint_cancelled=threading.Event()
        self.finish_release=threading.Event();self.endpoint_release=threading.Event()
        self.final_text='I remember a blue door near the sea.';self.finish_error=None
        if not blocked_finish:self.finish_release.set()
        if not blocked_endpoint:self.endpoint_release.set()
    async def health(self):
        self.health_calls+=1;return {'ready':self.ready,'asr':True,'turn':True}
    async def open_stream(self,on_partial=None):
        self.loop=asyncio.get_running_loop();stream=StreamingInput(self);stream.on_partial=on_partial
        self.streams.append(stream);self.opened.set();return stream
    async def classify(self,pcm):
        self.checks.append(pcm);self.endpoint_started.set()
        try:
            while not self.endpoint_release.is_set():await asyncio.sleep(.001)
            return TurnDecision(.98,True)
        except asyncio.CancelledError:
            self.endpoint_cancelled.set();raise
    async def close(self):self.closed=True
    def partial(self,text,index=-1):
        assert self.opened.wait(1),'Recognition stream did not open.'
        callback=self.streams[index].on_partial
        asyncio.run_coroutine_threadsafe(callback(text),self.loop).result(timeout=1)


def receive(socket):
    """Bound every read so a broken integration fails instead of hanging."""
    async def bounded():return await asyncio.wait_for(socket._send_rx.receive(),2)
    message=socket.portal.call(bounded)
    if message['type']=='websocket.close':raise WebSocketDisconnect(message.get('code',1000))
    return json.loads(message['text']) if message.get('text') else None


def until(socket,predicate):
    events=[]
    for _ in range(80):
        event=receive(socket)
        if event is None:continue
        events.append(event)
        if predicate(event):return events
    raise AssertionError('Expected event did not arrive within the bounded event list.')


class ListeningAppTests(unittest.TestCase):
    def setUp(self):
        self.environment=patch.dict(os.environ,{'LIVE_LISTENING_MODE':'nemotron-smart-turn','LIVE_PROFILE':'quality-3gpu'})
        self.environment.start();self.directory=tempfile.TemporaryDirectory()
    def tearDown(self):self.directory.cleanup();self.environment.stop()
    def app(self,providers,listening):
        return create_app(providers=providers,listening_client=listening,
            vad_factory=lambda:TurnDetector(lambda frame:frame[:2]==b'\x01\x00'),
            archive_store=ArchiveStore(Path(self.directory.name)))
    def start(self,socket):
        socket.send_json({'type':'start','character':example().model_dump(),'director':{'enabled':False}})
        event=receive(socket);self.assertEqual(event['type'],'ready')
        self.assertEqual(event['listening_mode'],'nemotron-smart-turn');return event
    def speech(self,socket):
        socket.send_bytes(frames(True,10))
        return until(socket,lambda event:event['type']=='listening_state' and event['phase']=='listening')
    def pause(self,socket):
        for _ in range(3):socket.send_bytes(frames(False,5))
    def barrier(self,socket):
        socket.send_json({'type':'inspect','enabled':True})
        return until(socket,lambda event:event['type']=='model_inspection')

    def test_ready_false_blocks_the_call_even_when_asr_and_turn_individually_report_true(self):
        providers=ActorProviders();listening=StreamingClient(ready=False)
        with TestClient(self.app(providers,listening)) as client:
            status=client.get('/api/status').json()
            self.assertFalse(status['ready']);self.assertFalse(status['services']['streaming_asr']);self.assertFalse(status['services']['turn_detector'])
            with client.websocket_connect('/api/live',headers={'origin':'http://testserver'}) as socket:
                event=receive(socket);self.assertEqual(event['type'],'error')
                with self.assertRaises(WebSocketDisconnect):receive(socket)
            self.assertEqual(listening.streams,[]);self.assertEqual(providers.histories,[])
        self.assertTrue(listening.closed);self.assertTrue(providers.closed)

    def test_partials_remain_out_of_history_until_the_stream_finalizes_once_with_streaming_metrics(self):
        providers=ActorProviders();listening=StreamingClient(blocked_finish=True)
        with TestClient(self.app(providers,listening)) as client:
            with client.websocket_connect('/api/live',headers={'origin':'http://testserver'}) as socket:
                self.start(socket);self.speech(socket)
                listening.partial('A draft beginning')
                draft=until(socket,lambda event:event['type']=='listening_partial')[-1]
                self.assertEqual(draft['text'],'A draft beginning');self.assertEqual(providers.histories,[])
                listening.partial('A draft beginning with revised words')
                until(socket,lambda event:event['type']=='listening_partial')
                self.pause(socket)
                finalizing=until(socket,lambda event:event['type']=='listening_state' and event['phase']=='finalizing')
                self.assertFalse(any(event['type']=='transcript' for event in finalizing));self.assertEqual(providers.histories,[])
                self.assertTrue(listening.streams[0].finishing.wait(1));listening.finish_release.set()
                events=until(socket,lambda event:event['type']=='response_done')
                self.assertEqual([event['turn_id'] for event in events if event['type']=='turn_start'],[1])
                self.assertEqual([event['text'] for event in events if event['type']=='transcript'],[listening.final_text])
                metrics=next(event for event in events if event['type']=='metrics')
                self.assertTrue(metrics['streaming_asr']);self.assertEqual(metrics['endpoint_reason'],'smart_turn')
                self.assertEqual(metrics['endpoint_ms'],300);self.assertEqual(metrics['turn_probability'],.98)
                self.assertIs(type(metrics['stream_finalize_ms']),int);self.assertGreaterEqual(metrics['stream_finalize_ms'],0)
                self.assertEqual(providers.whisper_calls,[]);self.assertEqual(len(providers.histories),1)
                self.assertEqual([item['content'] for item in providers.histories[0] if item['role']=='user'],[listening.final_text])
                self.assertNotIn('draft beginning',json.dumps(providers.histories));self.assertEqual(len(listening.streams),1)
                self.assertEqual(b''.join(listening.streams[0].sent),frames(True,10)+frames(False,15))
                socket.send_json({'type':'stop'})

    def test_typed_message_cancels_unfinished_speech_before_starting_the_actor(self):
        providers=ActorProviders();listening=StreamingClient(blocked_finish=True)
        with TestClient(self.app(providers,listening)) as client:
            with client.websocket_connect('/api/live',headers={'origin':'http://testserver'}) as socket:
                self.start(socket);self.speech(socket);listening.partial('Do not commit this partial')
                until(socket,lambda event:event['type']=='listening_partial')
                socket.send_json({'type':'text','text':'Please answer this typed question.'})
                events=until(socket,lambda event:event['type']=='response_done')
                self.assertTrue(listening.streams[0].closed.wait(1));self.assertEqual(listening.streams[0].finishing.is_set(),False)
                self.assertEqual([event['text'] for event in events if event['type']=='transcript'],['Please answer this typed question.'])
                listening.finish_release.set();listening.partial('PRIVATE_LATE_PARTIAL')
                later=self.barrier(socket)
                self.assertFalse(any(event['type'] in ('transcript','listening_partial') for event in later))
                self.assertEqual(len(providers.histories),1);self.assertNotIn('Do not commit',json.dumps(providers.histories));self.assertEqual(providers.whisper_calls,[])
                socket.send_json({'type':'stop'})

    def test_mute_closes_the_live_recognition_stream_and_does_not_commit_its_partial(self):
        providers=ActorProviders();listening=StreamingClient()
        with TestClient(self.app(providers,listening)) as client:
            with client.websocket_connect('/api/live',headers={'origin':'http://testserver'}) as socket:
                self.start(socket);self.speech(socket);listening.partial('An unfinished thought')
                until(socket,lambda event:event['type']=='listening_partial')
                socket.send_json({'type':'mute'})
                idle=until(socket,lambda event:event['type']=='listening_state' and event['phase']=='idle')[-1]
                self.assertEqual(idle['input_id'],2);self.assertTrue(listening.streams[0].closed.wait(1))
                self.assertEqual(providers.histories,[]);self.assertEqual(providers.whisper_calls,[])
                socket.send_json({'type':'stop'})

    def test_muting_while_endpoint_inference_is_pending_cannot_later_pause_capture_or_create_a_turn(self):
        providers=ActorProviders();listening=StreamingClient(blocked_endpoint=True)
        with TestClient(self.app(providers,listening)) as client:
            with client.websocket_connect('/api/live',headers={'origin':'http://testserver'}) as socket:
                self.start(socket);self.speech(socket);self.pause(socket)
                self.assertTrue(listening.endpoint_started.wait(1));socket.send_json({'type':'mute'})
                until(socket,lambda event:event['type']=='listening_state' and event['phase']=='idle')
                self.assertTrue(listening.endpoint_cancelled.wait(1));listening.endpoint_release.set()
                later=self.barrier(socket)
                self.assertFalse(any(event['type']=='turn_start' or event['type']=='listening_state' and event['phase']=='finalizing' for event in later))
                self.assertEqual(providers.histories,[]);self.assertEqual(providers.whisper_calls,[])
                socket.send_json({'type':'stop'})

    def test_disconnect_while_finalizing_cancels_recognition_releases_the_call_and_closes_the_client(self):
        providers=ActorProviders();listening=StreamingClient(blocked_finish=True)
        with TestClient(self.app(providers,listening)) as client:
            with client.websocket_connect('/api/live',headers={'origin':'http://testserver'}) as socket:
                self.start(socket);self.speech(socket);self.pause(socket)
                until(socket,lambda event:event['type']=='listening_state' and event['phase']=='finalizing')
                self.assertTrue(listening.streams[0].finishing.wait(1))
            self.assertTrue(listening.streams[0].closed.wait(1));self.assertTrue(listening.streams[0].cancelled.is_set())
            self.assertEqual(providers.histories,[])
            deadline=time.monotonic()+1
            while client.get('/api/status').json()['busy'] and time.monotonic()<deadline:time.sleep(.005)
            self.assertFalse(client.get('/api/status').json()['busy'])
            with client.websocket_connect('/api/live',headers={'origin':'http://testserver'}) as socket:
                self.start(socket);socket.send_json({'type':'stop'})
        self.assertTrue(listening.closed);self.assertTrue(providers.closed)

    def test_recognition_failure_is_visible_and_does_not_silently_use_whisper_or_start_an_actor_turn(self):
        providers=ActorProviders();listening=StreamingClient();listening.finish_error=ListeningError('PRIVATE_ASR_DETAILS')
        with TestClient(self.app(providers,listening)) as client:
            with client.websocket_connect('/api/live',headers={'origin':'http://testserver'}) as socket:
                self.start(socket);self.speech(socket);self.pause(socket)
                error=until(socket,lambda event:event['type']=='listening_state' and event['phase']=='error')[-1]
                self.assertNotIn('PRIVATE_ASR_DETAILS',error['message']);self.assertTrue(listening.streams[0].closed.wait(1))
                self.assertEqual(providers.histories,[]);self.assertEqual(providers.whisper_calls,[])
                socket.send_json({'type':'stop'})

    def test_protected_reply_microphone_reset_does_not_cancel_the_finalized_actor_turn(self):
        providers=ActorProviders();providers.finish=asyncio.Event();listening=StreamingClient()
        with TestClient(self.app(providers,listening)) as client:
            with client.websocket_connect('/api/live',headers={'origin':'http://testserver'}) as socket:
                self.start(socket);self.speech(socket);self.pause(socket)
                first=until(socket,lambda event:event['type']=='text_delta')
                self.assertEqual([event['text'] for event in first if event['type']=='transcript'],[listening.final_text])
                # The app discards speaker echo and resets the input controller
                # while the actor is still generating this finalized turn.
                socket.send_bytes(frames(True,10));self.barrier(socket)
                client.portal.call(providers.finish.set)
                rest=until(socket,lambda event:event['type']=='response_done')
                self.assertEqual([event['turn_id'] for event in first+rest if event['type']=='turn_start'],[1])
                self.assertEqual(len(providers.characters),1);self.assertEqual(len(providers.histories),1)
                self.assertEqual(len(listening.streams),1);self.assertEqual(providers.whisper_calls,[])
                self.assertTrue(listening.streams[0].closed.wait(1));socket.send_json({'type':'stop'})

    def test_baseline_ignores_the_injected_experiment_and_keeps_whisper_and_health_unchanged(self):
        providers=FakeProviders();listening=StreamingClient(ready=False);whisper=[]
        async def transcribe(pcm):
            whisper.append(pcm);return 'Baseline recognition still works.'
        providers.stt.transcribe=transcribe
        with patch.dict(os.environ,{'LIVE_LISTENING_MODE':'baseline','LIVE_PROFILE':'shared'}):
            with TestClient(self.app(providers,listening)) as client:
                status=client.get('/api/status').json()
                self.assertTrue(status['ready']);self.assertEqual(status['services'],{'stt':True,'llm':True,'tts':True})
                self.assertEqual(status['listening']['mode'],'baseline');self.assertFalse(status['listening']['experimental'])
                with client.websocket_connect('/api/live',headers={'origin':'http://testserver'}) as socket:
                    socket.send_json({'type':'start','character':example().model_dump(),'director':{'enabled':False}})
                    ready=receive(socket);self.assertEqual(ready['type'],'ready');self.assertEqual(ready['listening_mode'],'baseline')
                    self.assertEqual(ready['endpoint_ms'],400)
                    socket.send_bytes(frames(True,10));until(socket,lambda event:event['type']=='speech_start')
                    for _ in range(4):socket.send_bytes(frames(False,5))
                    events=until(socket,lambda event:event['type']=='response_done')
                    self.assertEqual([event['text'] for event in events if event['type']=='transcript'],['Baseline recognition still works.'])
                    metrics=next(event for event in events if event['type']=='metrics')
                    self.assertNotIn('streaming_asr',metrics);self.assertEqual(metrics['endpoint_ms'],400)
                    self.assertTrue(whisper);self.assertEqual(len(providers.characters),1)
                    socket.send_json({'type':'stop'})
        self.assertEqual(listening.health_calls,0);self.assertEqual(listening.streams,[]);self.assertEqual(listening.checks,[])
        self.assertFalse(listening.closed)


if __name__=='__main__':unittest.main()
