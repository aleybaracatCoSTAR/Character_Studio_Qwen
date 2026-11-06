"""Per-call native selection never removes or silently changes the baseline."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from live_voice.app import create_app
from live_voice.archive_database import ArchiveDatabase
from live_voice.collections import CollectionDatabase
from live_voice.turns import TurnDetector
from studio.characters import example
from test_collection_integration import Providers
from test_omni_session import StreamingProviders
from test_live_voice import reference_wav
from test_live_voice import frames
from test_listening_app import StreamingClient


class Native(StreamingProviders):
    async def health(self): return {'stt':True,'omni':True,'director':True}
    async def close(self): pass


class OmniAPITests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory(); root=Path(self.folder.name)
        self.qtm=ArchiveDatabase(root/'qtm'); self.art=CollectionDatabase(root/'art')
        self.base=Providers(); self.native=Native(); self.native.stt=self.base.stt
        self.headers={'origin':'http://testserver'}
    def tearDown(self): self.qtm.close(); self.art.close(); self.folder.cleanup()
    def app(self,**kwargs):
        return create_app(providers=self.base,archive_store=self.qtm,collections_store=self.art,
            omni_provider=self.native,vad_factory=lambda:TurnDetector(lambda frame:False),**kwargs)
    def start(self,**overrides):
        return {'type':'start','character':example().model_dump(),'director':{'enabled':True},**overrides}
    def until(self,socket,kind):
        events=[]
        for _ in range(40):
            message=socket.receive()
            if 'text' not in message: continue
            event=json.loads(message['text']); events.append(event)
            if event['type']==kind: return event,events
        raise AssertionError('Expected '+kind)

    def test_status_advertises_both_modes_and_default_baseline_without_model_loading(self):
        with TestClient(self.app()) as client:
            data=client.get('/api/status').json()
            self.assertEqual(data['comparison']['default_engine'],'baseline')
            self.assertTrue(data['comparison']['engines']['baseline']['ready'])
            self.assertTrue(data['comparison']['engines']['qwen-omni']['ready'])
            self.assertEqual(data['comparison']['engines']['qwen-omni']['output_rate'],24000)

    def test_native_ready_rate_and_stream_route_preserve_the_existing_baseline_provider(self):
        with TestClient(self.app()) as client:
            with client.websocket_connect('/api/live',headers=self.headers) as socket:
                socket.send_json(self.start(engine='qwen-omni',voice={'preset':'male'}))
                ready,_=self.until(socket,'ready')
                self.assertEqual(ready['engine'],'qwen-omni'); self.assertEqual(ready['output_rate'],24000)
                self.assertFalse(ready['voice_verification']); self.assertEqual(ready['voice'],'Ethan')
                socket.send_json({'type':'text','text':'Hello.'})
                done,events=self.until(socket,'response_done')
                self.assertEqual(done['engine'],'qwen-omni')
                self.assertTrue(any(event['type']=='metrics' and 'first_audio_ms' in event for event in events))
        self.assertEqual(len(self.native.native),1); self.assertEqual(self.base.actor_contexts,[])

    def test_old_client_without_engine_field_stays_44100_even_on_native_default_server(self):
        with patch.dict('os.environ',{'LIVE_DEFAULT_ENGINE':'qwen-omni'}): app=self.app()
        with TestClient(app) as client:
            self.assertEqual(client.get('/api/status').json()['comparison']['default_engine'],'qwen-omni')
            with client.websocket_connect('/api/live',headers=self.headers) as socket:
                socket.send_json(self.start())
                ready,_=self.until(socket,'ready')
                self.assertEqual(ready['engine'],'baseline'); self.assertEqual(ready['output_rate'],44100)
        self.assertEqual(self.native.native,[])

    def test_native_custom_voice_is_rejected_clearly_before_inference(self):
        with TestClient(self.app()) as client:
            with client.websocket_connect('/api/live',headers=self.headers) as socket:
                socket.send_json(self.start(engine='qwen-omni',voice={'preset':'custom',
                    'reference_audio':reference_wav(),'reference_text':'Hello.'}))
                error,_=self.until(socket,'error')
                self.assertIn('built-in voices',error['message'])
        self.assertEqual(self.native.native,[])

    def test_native_engine_does_not_depend_on_the_stopped_baseline_actor_or_tts(self):
        async def unavailable(): return {'stt':True,'llm':False,'tts':False}
        self.base.health=unavailable
        with TestClient(self.app()) as client:
            status=client.get('/api/status').json()['comparison']['engines']
            self.assertFalse(status['baseline']['ready']); self.assertTrue(status['qwen-omni']['ready'])
            with client.websocket_connect('/api/live',headers=self.headers) as socket:
                socket.send_json(self.start(engine='qwen-omni'))
                self.assertEqual(self.until(socket,'ready')[0]['engine'],'qwen-omni')

    def test_streaming_recognition_preserves_the_recording_for_native_audio_input(self):
        async def forbidden(pcm): raise AssertionError('Streaming recognition must not run Whisper again.')
        self.base.stt.transcribe=forbidden
        listening=StreamingClient()
        with patch.dict('os.environ',{'LIVE_LISTENING_MODE':'nemotron-smart-turn','LIVE_PROFILE':'quality-3gpu'}):
            app=create_app(providers=self.base,archive_store=self.qtm,collections_store=self.art,
                omni_provider=self.native,listening_client=listening,
                vad_factory=lambda:TurnDetector(lambda frame:frame[:2]==b'\x01\x00'))
        with TestClient(app) as client:
            with client.websocket_connect('/api/live',headers=self.headers) as socket:
                socket.send_json(self.start(engine='qwen-omni')); self.until(socket,'ready')
                socket.send_bytes(frames(True,10)); self.until(socket,'listening_state')
                for _ in range(3): socket.send_bytes(frames(False,5))
                self.until(socket,'response_done')
        self.assertEqual(len(self.native.native),1)
        request=self.native.native[0]
        self.assertIsInstance(request['kwargs']['pcm'],bytes)
        self.assertGreater(len(request['kwargs']['pcm']),0)
        self.assertEqual(request['history'][-1]['content'],listening.final_text)
