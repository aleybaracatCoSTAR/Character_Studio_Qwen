"""Actual image-load feedback is bounded state, never a new model request."""
import asyncio
import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from live_voice.app import create_app
from live_voice.archive import ArchiveSettings
from live_voice.archive_database import ArchiveDatabase
from live_voice.collections import CollectionDatabase
from live_voice.session import LiveSession
from live_voice.turns import TurnDetector
from studio.characters import example
from test_archive_session import ArchiveProviders
from test_artwork_session import ObservedSnapshot, artwork, COLLECTION
from test_collection_integration import Providers


class ImageStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.store=CollectionDatabase(self.directory.name)
        self.store.import_records([artwork(1,'Claude Monet','Water Lilies'),
            artwork(2,'Claude Monet','The Garden')],collection_id=COLLECTION,label='National Gallery of Art',source_kind='nga')
        self.events=[]; self.providers=ArchiveProviders()
        async def send_json(event): self.events.append(deepcopy(event))
        async def send_bytes(packet): pass
        self.snapshot=ObservedSnapshot(self.store.open_snapshot([COLLECTION]))
        self.session=LiveSession(self.providers,example(),send_json,send_bytes,
            archive=ArchiveSettings(enabled=True,scope='library',collection_ids=[COLLECTION]),archive_search=self.snapshot)

    async def asyncTearDown(self):
        await self.session.close(); self.store.close(); self.directory.cleanup()

    async def turn(self,text):
        await self.session.begin(text=text)
        await asyncio.wait_for(self.session.task,2)
        self.assertEqual(self.events[-1]['type'],'response_done')
        self.session.history.acknowledge(self.session.turn_id,0)
        self.session.playback_done(self.session.turn_id)

    def update(self,**changes):
        return {'type':'image_status','turn_id':self.session.turn_id,'status':'loaded',
                'loaded_count':1,'total_count':2,**changes}

    async def shown(self):
        await self.turn('Claude Monet')
        await self.turn('Show me the images')
        self.assertEqual(self.session.image_display['total_count'],2)

    async def test_load_feedback_changes_only_next_grounding_not_model_call_counts(self):
        await self.shown()
        calls=len(self.providers.actor)
        self.assertTrue(self.session.image_status(self.update(status='loading',loaded_count=0)))
        self.assertTrue(self.session.image_status(self.update()))
        self.assertEqual(len(self.providers.actor),calls)
        await self.turn('What does it look like?')
        context=self.providers.actor[-1]['kwargs']['archive']
        self.assertIn('browser reported',context[0]['content'])
        self.assertIn('loaded',context[0]['content'])
        self.assertEqual(len(self.snapshot.searches),1)
        self.assertEqual(len(json.loads(context[1]['content'])['references']),2)

    async def test_failed_loading_preserves_records_and_retry_reemits_the_same_artworks(self):
        await self.shown()
        previous=next(event for event in reversed(self.events) if event['type']=='archive_sources')
        self.assertTrue(self.session.image_status(self.update(status='unavailable',loaded_count=0)))
        await self.turn('I can’t see them')
        retry=next(event for event in reversed(self.events) if event['type']=='archive_sources')
        self.assertIs(retry['image_request'],True)
        self.assertEqual(retry['matches'],previous['matches'])
        self.assertEqual(len(self.snapshot.searches),1)
        self.assertEqual(self.session.image_display['status'],'sent')
        self.assertTrue(all(match['image_url'] for match in retry['matches']))

    async def test_invalid_stale_and_regressive_feedback_cannot_change_rendering_state(self):
        self.assertFalse(self.session.image_status(self.update()))
        await self.shown()
        original=deepcopy(self.session.image_display)
        for changes in ({'turn_id':True},{'turn_id':1},{'loaded_count':True},{'loaded_count':-1},
                        {'loaded_count':3},{'total_count':3},{'total_count':4},{'status':'unknown'},
                        {'status':'loading','loaded_count':1},{'status':'loaded','loaded_count':0},
                        {'status':'unavailable','loaded_count':1}):
            self.assertFalse(self.session.image_status(self.update(**changes)))
            self.assertEqual(self.session.image_display,original)
        self.assertTrue(self.session.image_status(self.update()))
        self.assertFalse(self.session.image_status(self.update(status='unavailable',loaded_count=0)))
        self.assertFalse(self.session.image_status(self.update(status='loading',loaded_count=0)))
        await self.turn('Thanks')
        self.assertFalse(self.session.image_status(self.update(turn_id=2,loaded_count=2)))

    async def test_closing_the_call_clears_feedback_and_offers(self):
        await self.shown(); await self.session.close()
        self.assertIsNone(self.session.image_display)
        self.assertEqual(self.session.pending_image_offer,[])
        self.assertFalse(self.session.image_status(self.update()))


class ImageCapabilityAPITests(unittest.TestCase):
    def test_browser_load_feedback_reaches_the_next_actor_prompt_over_the_websocket(self):
        with tempfile.TemporaryDirectory() as directory:
            art=CollectionDatabase(Path(directory)/'art'); stories=ArchiveDatabase(Path(directory)/'stories')
            art.import_records([artwork(1,'Claude Monet','Water Lilies')],collection_id=COLLECTION,
                label='National Gallery of Art',source_kind='nga')
            providers=Providers()
            try:
                app=create_app(providers=providers,archive_store=stories,collections_store=art,
                    vad_factory=lambda:TurnDetector(lambda pcm:False))
                with TestClient(app) as client:
                    with client.websocket_connect('/api/live',headers={'origin':'http://testserver'}) as socket:
                        socket.send_json({'type':'start','character':example().model_dump(),
                            'archive':{'enabled':True,'scope':'library','collection_ids':[COLLECTION]}})
                        self.assertEqual(socket.receive_json()['type'],'ready')
                        for utterance in ('Claude Monet','Show me the image'):
                            socket.send_json({'type':'text','text':utterance})
                            while True:
                                packet=socket.receive()
                                if 'text' not in packet: continue
                                event=json.loads(packet['text'])
                                if event['type']=='error': self.fail(event['message'])
                                if event['type']=='response_done': break
                        socket.send_json({'type':'image_status','turn_id':2,'status':'loaded',
                                          'loaded_count':1,'total_count':1})
                        socket.send_json({'type':'text','text':'Describe it'})
                        while True:
                            packet=socket.receive()
                            if 'text' not in packet: continue
                            event=json.loads(packet['text'])
                            if event['type']=='error': self.fail(event['message'])
                            if event['type']=='response_done': break
                        self.assertEqual(len(providers.actor_contexts),3)
                        self.assertIn('browser reported',providers.actor_contexts[-1][0]['content'])
                        self.assertIn('loaded',providers.actor_contexts[-1][0]['content'])
                        socket.send_json({'type':'stop'})
            finally: art.close(); stories.close()

    def test_status_and_ready_advertise_the_updated_image_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            art=CollectionDatabase(Path(directory)/'art'); stories=ArchiveDatabase(Path(directory)/'stories')
            try:
                app=create_app(providers=Providers(),archive_store=stories,collections_store=art,
                    vad_factory=lambda:TurnDetector(lambda pcm:False))
                with TestClient(app) as client:
                    status=client.get('/api/status').json()
                    self.assertEqual(status['artwork_images'],'dialogue-v2')
                    self.assertIs(status['image_status'],True)
                    with client.websocket_connect('/api/live',headers={'origin':'http://testserver'}) as socket:
                        socket.send_json({'type':'start','character':example().model_dump()})
                        ready=socket.receive_json()
                        self.assertEqual(ready['type'],'ready')
                        self.assertEqual(ready['artwork_images'],'dialogue-v2')
                        self.assertIs(ready['image_status'],True)
                        socket.send_json({'type':'image_status','turn_id':999,'status':'loaded','loaded_count':1,'total_count':1})
                        socket.send_json({'type':'stop'})
            finally: art.close(); stories.close()


if __name__=='__main__': unittest.main()
