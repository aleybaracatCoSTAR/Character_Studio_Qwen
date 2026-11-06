"""Collection choice controls retrieval, grounding and visible source metadata."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from live_voice.app import create_app
from live_voice.archive import ArchiveSettings
from live_voice.archive_database import ArchiveDatabase
from live_voice.collection_catalog import CollectionCatalog
from live_voice.artwork_requests import image_capability_context
from live_voice.collections import CollectionDatabase, collection_grounding_messages
from live_voice.director import DirectorPlan
from live_voice.presets import CharacterPreset, preset
from live_voice.turns import TurnDetector


def artwork(number=1, **changes):
    return {'id':f'{number:032x}', 'title':'Monet garden catalogue test',
            'text':'A catalogue test record about Monet and a garden.',
            'source_url':f'https://www.nga.gov/collection/art-object-page.{number}.html',
            'source_entry_id':str(number),
            'image_url':f'https://api.nga.gov/iiif/example-{number}/full/!200,200/0/default.jpg',
            'image_alt':'Catalogue-provided description',
            'metadata':{'artist':'Claude Monet', 'date':'1900', 'medium':'Oil on canvas', 'rights':'CC0'},
            **changes}


class Providers:
    class STT:
        def load(self): pass
        async def transcribe(self, pcm): return 'Monet'
    def __init__(self):
        self.stt = self.STT()
        self.actor_contexts, self.director_contexts = [], []
    async def health(self): return {'stt':True,'llm':True,'tts':True}
    async def close(self): pass
    async def text(self, character, history, **kwargs):
        self.actor_contexts.append(kwargs.get('archive'))
        yield 'The catalogue has a matching record.'
    async def direct(self, *args, **kwargs):
        self.director_contexts.append(kwargs.get('archive'))
        return DirectorPlan(intention='Use the catalogue evidence.',emotion='neutral',delivery='Natural conversation.')
    async def audio(self, text, reference=None, on_notice=None): yield b'\x01\x00'*100


class CollectionFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.qtm = ArchiveDatabase(self.root/'qtm')
        self.data = CollectionDatabase(self.root/'art')
        self.catalog = CollectionCatalog(self.qtm, self.data)
        self.qtm.add(text='A garden where I first held her hand.',source_url='https://www.queeringthemap.com/moment/1')
        self.data.import_records([artwork()],collection_id='national-gallery-of-art',label='National Gallery of Art',
                                 source_kind='nga',source_url='https://github.com/NationalGalleryOfArt/opendata')
    def tearDown(self):
        self.qtm.close(); self.data.close(); self.temporary.cleanup()
    def app(self, **kwargs):
        return create_app(providers=Providers(), archive_store=self.qtm, collections_store=self.data,
                          vad_factory=lambda:TurnDetector(lambda frame:False), **kwargs)


class CollectionCatalogTests(CollectionFixture):
    def test_explicit_sources_are_isolated_and_mixed_retrieval_retains_rights_and_images(self):
        original = hashlib.sha256(self.qtm.path.read_bytes()).hexdigest()
        art = self.catalog.open_snapshot(['national-gallery-of-art'])
        try:
            hits = art.search('garden')
            self.assertEqual(art.count,1)
            self.assertEqual([value['collection_id'] for value in hits],['national-gallery-of-art'])
            self.assertEqual(hits[0]['metadata']['rights'],'CC0')
            self.assertTrue(hits[0]['image_url'].startswith('https://api.nga.gov/'))
        finally: art.close()
        mixed = self.catalog.open_snapshot(['qtm','national-gallery-of-art'])
        try:
            hits = mixed.search('garden')
            self.assertEqual(mixed.count,2)
            self.assertEqual({value['collection_id'] for value in hits},{'qtm','national-gallery-of-art'})
            qtm = next(value for value in hits if value['collection_id']=='qtm')
            self.assertEqual(qtm['metadata']['rights'],'CC BY-NC-SA 4.0')
            self.assertIsNone(qtm['image_url'])
        finally: mixed.close()
        self.assertEqual(hashlib.sha256(self.qtm.path.read_bytes()).hexdigest(),original)

    def test_pages_cross_source_boundaries_without_losing_labels_or_repeating_records(self):
        self.data.import_records([artwork(2)],collection_id='another',label='Another collection')
        ids = ['qtm','national-gallery-of-art','another']
        pages = [self.catalog.page(ids,offset=index,limit=1) for index in range(3)]
        self.assertTrue(all(page['total']==3 and page['matched']==3 for page in pages))
        entries = [page['entries'][0] for page in pages]
        self.assertEqual([value['collection_id'] for value in entries],ids)
        self.assertEqual(entries[1]['collection_label'],'National Gallery of Art')
        self.assertEqual(self.catalog.page(ids,query='Monet')['matched'],2)
        self.assertEqual(self.catalog.page(ids,offset=9)['entries'],[])

    def test_generic_sources_share_one_frozen_copy_and_partial_failure_disposes_owned_snapshots(self):
        self.data.import_records([artwork(2)],collection_id='another',label='Another collection')
        with patch.object(self.data,'open_snapshot',wraps=self.data.open_snapshot) as opened:
            snapshot = self.catalog.open_snapshot(['national-gallery-of-art','qtm','another'])
            self.assertEqual(opened.call_count,1)
            self.assertEqual(opened.call_args.args[0],['national-gallery-of-art','another'])
            snapshot.close()
        original_open = self.qtm.open_snapshot
        owned = []
        def capture_snapshot():
            snapshot = original_open(); owned.append(snapshot); return snapshot
        with patch.object(self.qtm,'open_snapshot',side_effect=capture_snapshot):
            with patch.object(self.data,'open_snapshot',side_effect=ValueError('test failure')):
                with self.assertRaises(ValueError): self.catalog.open_snapshot(['qtm','national-gallery-of-art'])
            self.assertEqual(len(owned),1)
            self.assertIsNone(owned[0]._connection)
        with self.assertRaises(KeyError): self.catalog.open_snapshot(['not-saved'])

    def test_collection_selection_requires_unique_known_bounded_ids_and_unambiguous_scope(self):
        for values in (['../qtm'],['qtm','qtm'],['a']*9):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                ArchiveSettings(scope='library',collection_ids=values)
        with self.assertRaises(ValidationError): ArchiveSettings(collection_ids=['qtm'])
        with self.assertRaises(ValidationError):
            ArchiveSettings(scope='library',collection_ids=['qtm'],entry_ids=['a'*32])
        self.assertEqual(ArchiveSettings().collection_ids,[])


class CollectionAPITests(CollectionFixture):
    def test_registry_pages_and_new_character_are_available_without_model_loading(self):
        with TestClient(self.app(preview=True)) as client:
            registry = client.get('/api/archive/collections').json()['collections']
            self.assertEqual([value['id'] for value in registry],['qtm','national-gallery-of-art'])
            self.assertEqual([value['count'] for value in registry],[1,1])
            page = client.get('/api/archive/collections/entries',params={'ids':'national-gallery-of-art','q':'Monet'})
            self.assertEqual(page.status_code,200)
            self.assertEqual(page.json()['entries'][0]['metadata']['artist'],'Claude Monet')
            self.assertEqual(client.get('/api/archive/collections/entries?ids=missing').status_code,404)
            self.assertEqual(client.get('/api/archive/collections/entries?ids=qtm,qtm').status_code,400)
            robin = CharacterPreset.model_validate(client.get('/api/characters/presets/art-guide').json())
            self.assertEqual(robin.character.name,'Robin')
            self.assertEqual(robin.archive.collection_ids,['national-gallery-of-art'])
            self.assertFalse(robin.narrative.enabled)

    def test_named_upload_is_atomic_same_origin_and_preserves_legacy_stories(self):
        with TestClient(self.app(preview=True)) as client:
            before = client.get('/api/archive/entries').json()
            headers = {'origin':'http://testserver','content-type':'application/octet-stream',
                       'x-archive-filename':'art.json','x-collection-name':'My%20collection'}
            record = artwork(3)
            denied = client.post('/api/archive/collections/import',content=json.dumps([record]),headers={**headers,'origin':'https://elsewhere.example'})
            self.assertEqual(denied.status_code,403)
            uploaded = client.post('/api/archive/collections/import',content=json.dumps([record]),headers=headers)
            self.assertEqual(uploaded.status_code,200)
            value = uploaded.json()
            self.assertEqual(value['added'],1)
            self.assertEqual(value['collection']['label'],'My collection')
            self.assertTrue(value['collection']['id'].startswith('user-'))
            names = client.get('/api/archive/collections').json()
            bad = client.post('/api/archive/collections/import',content=json.dumps([artwork(4),{**artwork(5),'source_url':'file:///private'}]),headers=headers)
            self.assertEqual(bad.status_code,400)
            self.assertEqual(client.get('/api/archive/collections').json(),names)
            self.assertEqual(client.get('/api/archive/entries').json(),before)

    def test_websocket_delivers_the_same_bounded_sources_to_both_models_and_the_image_display(self):
        providers = Providers()
        app = create_app(providers=providers,archive_store=self.qtm,collections_store=self.data,
                         vad_factory=lambda:TurnDetector(lambda frame:False))
        config = preset('art-guide')
        config['director']['enabled'] = True
        with TestClient(app) as client:
            with client.websocket_connect('/api/live',headers={'origin':'http://testserver'}) as socket:
                socket.send_json({'type':'start',**config})
                self.assertEqual(socket.receive_json()['type'],'ready')
                socket.send_json({'type':'text','text':'Show me Monet'})
                events = []
                for _ in range(30):
                    message = socket.receive()
                    if 'text' not in message: continue
                    event = json.loads(message['text']); events.append(event)
                    if event['type'] in ('response_done','error'): break
                self.assertEqual(events[-1]['type'],'response_done')
                sources = next(value for value in events if value['type']=='archive_sources')
                self.assertEqual(sources['collection_ids'],['national-gallery-of-art'])
                self.assertEqual(sources['matches'][0]['metadata']['artist'],'Claude Monet')
                expected = collection_grounding_messages(sources['matches'])
                expected[0]['content'] += '\n\n'+image_capability_context(sources['matches'])
                self.assertEqual(providers.actor_contexts,[expected])
                self.assertEqual(providers.director_contexts,[expected])
                self.assertIn('not mean you have seen',expected[0]['content'])
                socket.send_json({'type':'stop'})

    def test_import_is_refused_during_an_active_call(self):
        with TestClient(self.app()) as client:
            with client.websocket_connect('/api/live',headers={'origin':'http://testserver'}) as socket:
                socket.send_json({'type':'start','character':preset('art-guide')['character']})
                self.assertEqual(socket.receive_json()['type'],'ready')
                result = client.post('/api/archive/collections/import',content=json.dumps([artwork(3)]),
                    headers={'origin':'http://testserver','content-type':'application/octet-stream',
                             'x-archive-filename':'art.json','x-collection-name':'Test'})
                self.assertEqual(result.status_code,409)
                socket.send_json({'type':'stop'})


if __name__ == '__main__': unittest.main()
