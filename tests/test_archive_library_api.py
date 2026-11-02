"""Large-collection routes and active-call boundaries with local synthetic stories."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from live_voice.app import create_app
from live_voice.archive import ArchiveSettings
from live_voice.archive_database import ArchiveDatabase
from live_voice.director import DirectorSettings
from live_voice.session import LiveSession
from live_voice.turns import TurnDetector
from studio.characters import example
from test_archive_api import ReadOnlyProviders, FakeImporter
from test_archive_session import ArchiveProviders
from test_live_voice import FakeProviders

ORIGIN = {'origin':'http://testserver'}
UPLOAD = {**ORIGIN,'content-type':'application/octet-stream','x-archive-filename':'stories.json'}


class LibraryAPITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = ArchiveDatabase(self.temporary.name)
        self.client = TestClient(create_app(preview=True,providers=ReadOnlyProviders(),
            archive_store=self.store,archive_importer=FakeImporter()))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None,None,None)
        self.store.close()
        self.temporary.cleanup()

    def upload(self, values, **headers):
        return self.client.post('/api/archive/import',headers={**UPLOAD,**headers},
                                content=json.dumps(values).encode())

    def test_bulk_export_is_indexed_beyond_old_limit_and_pages_do_not_return_the_whole_corpus(self):
        values = {str(index):f'Fictional garden memory number {index}.' for index in range(1,81)}
        result = self.upload(values)
        self.assertEqual(result.status_code,200)
        self.assertEqual((result.json()['added'],result.json()['total']),(80,80))
        page = self.client.get('/api/archive/entries?limit=25&offset=50').json()
        self.assertEqual((page['total'],page['matched'],page['offset'],len(page['entries'])),(80,80,50,25))
        self.assertEqual(page['scope'],'library')
        self.assertTrue(all(entry['provenance']=='imported' for entry in page['entries']))
        selected = page['entries'][0]
        selection = self.client.get('/api/archive/selection',params={'ids':selected['id']}).json()
        self.assertEqual(selection['entries'],[selected])
        self.assertNotIn(selected['id'],[entry['id'] for entry in result.json()['entries']])

    def test_search_returns_relevant_sources_and_counts_but_no_script_execution(self):
        self.upload([{'source_entry_id':'1','description':'<p>Fictional telescope discovery — القاهرة.</p><script>PRIVATE_SCRIPT()</script>'},
                     {'source_entry_id':'2','text':'A fictional garden memory.'}])
        result = self.client.get('/api/archive/entries',params={'q':'telescope'}).json()
        self.assertEqual((result['total'],result['matched']),(2,1))
        self.assertEqual(result['entries'][0]['source_url'],'https://www.queeringthemap.com/moment/1')
        self.assertNotIn('PRIVATE_SCRIPT',json.dumps(result))
        self.assertIn('القاهرة',result['entries'][0]['text'])
        self.assertEqual(self.client.get('/api/archive/entries?q=nonexistent').json()['entries'],[])

    def test_official_plain_text_map_preserves_literal_symbols_and_markup_without_skipping_records(self):
        values={'1':'</3','2':'<3','3':'<p>Literal words, not an HTML field.</p>','4':'A following fictional story.'}
        result=self.upload(values)
        self.assertEqual(result.status_code,200)
        self.assertEqual((result.json()['added'],result.json()['total']),(4,4))
        actual={entry['source_entry_id']:entry['text'] for entry in result.json()['entries']}
        self.assertEqual(actual,values)
        repeated=self.upload(values).json()
        self.assertEqual((repeated['added'],repeated['duplicates'],repeated['total']),(0,4,4))

    def test_duplicate_import_is_idempotent_and_invalid_import_rolls_back_all_rows(self):
        initial = self.upload({'1':'A fictional garden memory.'}).json()
        second = self.upload({'1':'A fictional garden memory.'}).json()
        self.assertEqual((second['added'],second['duplicates'],second['total']),(0,1,1))
        invalid = self.upload([{'id':'2','text':'PRIVATE_VALID_SENTINEL'}, {'id':'3','text':None}])
        self.assertEqual(invalid.status_code,400)
        self.assertNotIn('PRIVATE_VALID_SENTINEL',invalid.text)
        self.assertEqual(self.store.count(),1)
        self.assertEqual(self.store.list()[0]['id'],initial['entries'][0]['id'])

    def test_upload_requires_same_origin_supported_type_and_bounded_size(self):
        for origin in ('null','https://evil.invalid',''):
            self.assertEqual(self.upload({'1':'Fictional story.'},origin=origin).status_code,403)
        self.assertEqual(self.upload({'1':'Fictional story.'},**{'content-type':'application/json'}).status_code,400)
        self.assertEqual(self.upload({'1':'Fictional story.'},**{'x-archive-filename':'stories.exe'}).status_code,400)
        with patch('live_voice.app.MAX_IMPORT_BYTES',8):
            self.assertEqual(self.upload({'1':'A fictional long story.'}).status_code,413)
        self.assertEqual(self.store.count(),0)

    def test_page_and_selection_inputs_are_bounded_and_unknown_ids_are_pruned(self):
        self.assertEqual(self.client.get('/api/archive/entries?offset=-1').status_code,422)
        self.assertEqual(self.client.get('/api/archive/entries?limit=1000').status_code,422)
        self.assertEqual(self.client.get('/api/archive/selection?ids=../bad').status_code,400)
        self.assertEqual(self.client.get('/api/archive/selection',params={'ids':'a'*32}).json(),{'entries':[]})
        ids = ','.join(f'{index:032x}' for index in range(51))
        self.assertEqual(self.client.get('/api/archive/selection',params={'ids':ids}).status_code,400)

    def test_all_library_call_freezes_snapshot_and_blocks_import_and_delete_until_ended(self):
        self.upload({'1':'A fictional garden memory.'})
        entry = self.store.list()[0]
        app = create_app(preview=False,providers=FakeProviders(),archive_store=self.store,
                         vad_factory=lambda:TurnDetector(lambda frame:False))
        with TestClient(app) as client:
            with client.websocket_connect('/api/live',headers=ORIGIN) as socket:
                socket.send_json({'type':'start','character':example().model_dump(),
                                  'archive':{'enabled':True,'scope':'library','entry_ids':[]}})
                self.assertEqual(socket.receive_json()['type'],'ready')
                self.assertEqual(client.post('/api/archive/import',headers=UPLOAD,content=b'{"2":"fictional"}').status_code,409)
                self.assertEqual(client.delete('/api/archive/entries/'+entry['id'],headers=ORIGIN).status_code,409)
                self.assertEqual(client.get('/api/archive/entries').json()['total'],1)
                socket.send_json({'type':'stop'})


class LibrarySessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_question_searches_database_and_grounds_actor_director_and_citations_with_the_same_excerpts(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ArchiveDatabase(folder)
            path = Path(folder)/'stories.json'
            path.write_text(json.dumps({str(index):'A fictional garden memory.' for index in range(1,101)}))
            store.import_file(path)
            selected = store.add(text='A fictional telescope discovery.',source_url='https://www.queeringthemap.com/moment/999')
            snapshot = store.open_snapshot()
            # Later changes cannot silently alter this active call's evidence.
            store.remove(selected['id'])
            events = []
            async def send_json(event): events.append(event)
            async def send_bytes(packet): pass
            providers = ArchiveProviders()
            session = LiveSession(providers,example(),send_json,send_bytes,
                director=DirectorSettings(enabled=True),archive=ArchiveSettings(enabled=True,scope='library'),
                archive_search=snapshot)
            try:
                await session.begin(text='Tell me about telescope discovery.')
                await asyncio.wait_for(session.task,2)
                source = next(event for event in events if event['type']=='archive_sources')
                self.assertEqual((source['scope'],source['selected_count']),('library',101))
                self.assertEqual(source['matches'][0]['id'],selected['id'])
                actor = providers.actor[0]['kwargs']['archive']
                self.assertEqual(actor,providers.director[0]['kwargs']['archive'])
                self.assertEqual(json.loads(actor[1]['content'])['references'],source['matches'])
                self.assertIn('local imported Queering the Map database',actor[1]['content'])
                self.assertEqual(session.character.memories,example().memories)
                await session.close()
                with self.assertRaises(ValueError): snapshot.search('telescope')
            finally:
                await session.close()
                store.close()

    async def test_empty_library_supplies_explicit_no_evidence_without_inventing_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ArchiveDatabase(folder)
            providers,events = ArchiveProviders(),[]
            async def send_json(event): events.append(event)
            async def send_bytes(packet): pass
            session = LiveSession(providers,example(),send_json,send_bytes,
                director=DirectorSettings(enabled=False),archive=ArchiveSettings(enabled=True,scope='library'),
                archive_search=store.open_snapshot())
            try:
                await session.begin(text='Tell me about a garden.')
                await asyncio.wait_for(session.task,2)
                context = providers.actor[0]['kwargs']['archive']
                self.assertIn('cannot find that in the local archive',context[0]['content'])
                self.assertEqual(json.loads(context[1]['content'])['references'],[])
            finally:
                await session.close()
                store.close()


if __name__ == '__main__': unittest.main()
