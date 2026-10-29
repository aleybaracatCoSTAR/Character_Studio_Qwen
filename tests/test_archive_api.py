"""Selected-story endpoints preserve the studio and never crawl or call models."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx

from live_voice.app import create_app
from live_voice.archive import ArchiveStore
from live_voice.archive_source import (ArchiveSourceUnavailable, QueeringTheMap,
                                       description_text, qtm_entry_id)
from live_voice.turns import TurnDetector
from studio.characters import example
from test_live_voice import FakeProviders


ORIGIN = {'origin':'http://testserver'}
SOURCE = 'https://www.queeringthemap.com/moment/123'
STORY = 'A fictional selected story for a transport test — القاهرة.'


class ReadOnlyProviders:
    def __init__(self): self.closed = False
    async def close(self): self.closed = True
    async def health(self): raise AssertionError('Archive endpoints must not contact model services.')


class FakeImporter:
    def __init__(self): self.calls = []
    async def fetch(self, entry_id):
        self.calls.append(entry_id)
        return {'title':f'Selected story {entry_id}','text':STORY,
                'source_url':f'https://www.queeringthemap.com/moment/{entry_id}',
                'source_entry_id':entry_id}


class ArchiveAPITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = ArchiveStore(Path(self.directory.name))
        self.importer = FakeImporter()
        self.providers = ReadOnlyProviders()
        self.client = TestClient(create_app(preview=True, providers=self.providers,
            archive_store=self.store,archive_importer=self.importer))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None,None,None)
        self.directory.cleanup()

    def manual(self, **changes):
        values = {'title':'Selected story','text':STORY,'source_url':SOURCE}
        values.update(changes)
        return self.client.post('/api/archive/entries',headers=ORIGIN,json=values)

    def test_archive_page_retains_the_existing_studio_document_and_starts_with_a_selected_empty_library(self):
        studio = self.client.get('/')
        page = self.client.get('/archive/')
        self.assertEqual(page.status_code,200)
        self.assertEqual(page.content,studio.content)
        self.assertIn('Character Studio',page.text)
        result = self.client.get('/api/archive/entries')
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json(),{'entries':[],'limit':50,'scope':'selected'})
        self.assertEqual(result.headers['cache-control'],'no-store')
        self.assertEqual(self.importer.calls,[])

    def test_one_explicit_import_calls_the_importer_once_with_a_normalized_id(self):
        result = self.client.post('/api/archive/entries',headers=ORIGIN,json={'entry_id':SOURCE})
        self.assertEqual(result.status_code,201)
        self.assertEqual(self.importer.calls,['123'])
        entry = result.json()['entry']
        self.assertRegex(entry['id'],r'^[a-f0-9]{32}$')
        self.assertEqual(entry['text'],STORY)
        self.assertEqual(entry['source_url'],SOURCE)
        self.assertEqual(entry['source_entry_id'],'123')
        self.assertEqual(result.json()['entries'],[entry])
        self.assertEqual(self.client.get('/api/archive/entries').json()['entries'],[entry])

    def test_manual_story_preserves_unicode_plain_text_and_does_not_fetch_a_url(self):
        text = '<img src=x onerror="PRIVATE_SENTINEL()">\nA plain story — القاهرة.'
        result = self.manual(text=text)
        self.assertEqual(result.status_code,201)
        self.assertEqual(result.json()['entry']['text'],text)
        self.assertEqual(self.importer.calls,[])
        self.assertNotIn('PRIVATE_SENTINEL',self.client.get('/archive/').text)
        self.assertEqual(result.headers['cache-control'],'no-store')

    def test_missing_manual_source_defaults_to_the_map_without_claiming_a_verified_import(self):
        response = self.client.post('/api/archive/entries',headers=ORIGIN,
                                   json={'text':STORY,'title':'A selected pasted passage'})
        self.assertEqual(response.status_code,201)
        entry = response.json()['entry']
        self.assertEqual(entry['source_url'],'https://www.queeringthemap.com/')
        self.assertEqual(entry['provenance'],'pasted')
        self.assertIsNone(entry['source_entry_id'])
        self.assertEqual(self.importer.calls,[])
        forged = self.client.post('/api/archive/entries',headers=ORIGIN,
                                  json={'text':STORY,'provenance':'fetched'})
        self.assertEqual(forged.status_code,400)

    def test_manual_title_is_optional_and_duplicate_selected_sources_do_not_multiply(self):
        values = {'text':STORY,'source_url':SOURCE}
        first = self.client.post('/api/archive/entries',headers=ORIGIN,json=values)
        second = self.client.post('/api/archive/entries',headers=ORIGIN,json=values)
        self.assertEqual(first.status_code,201)
        self.assertEqual(second.status_code,201)
        self.assertTrue(first.json()['entry']['title'])
        self.assertEqual(first.json()['entry']['id'],second.json()['entry']['id'])
        self.assertEqual(len(self.client.get('/api/archive/entries').json()['entries']),1)

    def test_delete_removes_only_the_requested_entry_and_unknown_id_is_404(self):
        entry = self.manual().json()['entry']
        result = self.client.delete('/api/archive/entries/'+entry['id'],headers=ORIGIN)
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json(),{'entries':[]})
        self.assertEqual(self.client.delete('/api/archive/entries/'+entry['id'],headers=ORIGIN).status_code,404)
        self.assertEqual(self.importer.calls,[])

    def test_mutations_require_an_exact_same_origin_including_missing_origin_rejection(self):
        entry = self.manual().json()['entry']
        for origin in (None,'null','https://testserver','http://testserver:8080','http://testserver.evil.invalid',
                       'http://other.invalid','http://testserver/','http://testserver/path'):
            headers = {} if origin is None else {'origin':origin}
            for operation in ('post','delete'):
                with self.subTest(origin=origin,operation=operation):
                    if operation=='post':
                        result = self.client.post('/api/archive/entries',headers=headers,json={'entry_id':'123'})
                    else:
                        result = self.client.delete('/api/archive/entries/'+entry['id'],headers=headers)
                    self.assertEqual(result.status_code,403)
                    self.assertNotIn(STORY,result.text)
        self.assertEqual(self.importer.calls,[])
        self.assertEqual(len(self.store.list()),1)

    def test_unselected_bulk_requests_invalid_shapes_and_unknown_fields_are_rejected(self):
        bad = [None,[],{}, {'entry_id':'123','text':STORY,'source_url':SOURCE},
            {'entry_id':'123','title':'Unexpected mixed mode'}, {'entry_id':'123','batch':True},
            {'entry_id':123},{'entry_id':['123','124']}, {'text':STORY,'source_url':SOURCE,'unknown':'PRIVATE_SENTINEL'},
            {'title':'PRIVATE_SENTINEL','source_url':SOURCE}, {'text':'','source_url':SOURCE}]
        for values in bad:
            with self.subTest(values=values):
                result = self.client.post('/api/archive/entries',headers=ORIGIN,json=values)
                self.assertEqual(result.status_code,400)
                self.assertNotIn('PRIVATE_SENTINEL',result.text)
        self.assertEqual(self.importer.calls,[])
        self.assertEqual(self.store.list(),[])

    def test_noncanonical_story_urls_and_non_ascii_ids_never_reach_importer(self):
        invalid = ['١٢٣','１２３','12٣','-1','12 3','1e3','https://www.queeringthemap.com/',
            'https://www.queeringthemap.com/moment/123?search=anything',
            'https://www.queeringthemap.com/moment/123#fragment',
            'https://www.queeringthemap.com:443/moment/123',
            'https://user:password@www.queeringthemap.com/moment/123',
            'https://www.queeringthemap.com.evil.invalid/moment/123',
            'https://evil.invalid/moment/123','http://www.queeringthemap.com/moment/123',
            'https://www.queeringthemap.com/data/descriptions.json',
            'http://127.0.0.1:18093/health']
        for value in invalid:
            with self.subTest(value=value):
                result = self.client.post('/api/archive/entries',headers=ORIGIN,json={'entry_id':value})
                self.assertEqual(result.status_code,400)
        self.assertEqual(self.importer.calls,[])
        self.assertEqual(self.store.list(),[])

    def test_manual_provenance_is_limited_to_the_selected_official_site(self):
        for url in ('javascript:PRIVATE_SENTINEL()', 'file:///PRIVATE_SENTINEL','http://127.0.0.1/PRIVATE_SENTINEL',
                    'https://other.invalid/PRIVATE_SENTINEL',SOURCE+'?private=PRIVATE_SENTINEL'):
            result = self.manual(source_url=url)
            with self.subTest(url=url):
                self.assertEqual(result.status_code,400)
                self.assertNotIn('PRIVATE_SENTINEL',result.text)
        self.assertEqual(self.store.list(),[])

    def test_bad_json_wrong_content_type_and_large_body_fail_before_import(self):
        for body,content_type in ((b'{bad PRIVATE_SENTINEL','application/json'),(b'PRIVATE_SENTINEL','text/plain')):
            result = self.client.post('/api/archive/entries',headers={**ORIGIN,'content-type':content_type},content=body)
            self.assertEqual(result.status_code,400)
            self.assertNotIn('PRIVATE_SENTINEL',result.text)
        result = self.client.post('/api/archive/entries',headers={**ORIGIN,'content-type':'application/json'},
                                  content=b'X'*(64*1024+1))
        self.assertEqual(result.status_code,413)
        self.assertLess(len(result.content),1024)
        self.assertEqual(self.importer.calls,[])
        self.assertEqual(self.store.list(),[])

    def test_remote_service_failure_is_safe_and_leaves_the_archive_unchanged(self):
        async def unavailable(value):
            raise ArchiveSourceUnavailable('PRIVATE_REMOTE_BODY https://private.invalid/secret')
        with patch.object(self.importer,'fetch',side_effect=unavailable):
            result = self.client.post('/api/archive/entries',headers=ORIGIN,json={'entry_id':'123'})
        self.assertIn(result.status_code,(502,503))
        self.assertNotIn('PRIVATE_REMOTE_BODY',result.text)
        self.assertNotIn('private.invalid',result.text)
        self.assertEqual(self.store.list(),[])

    def test_filesystem_failures_return_safe_errors_without_exposing_paths_or_story_text(self):
        entry = self.manual().json()['entry']
        cases = [('list',lambda:self.client.get('/api/archive/entries')),
                 ('add',lambda:self.manual()),
                 ('remove',lambda:self.client.delete('/api/archive/entries/'+entry['id'],headers=ORIGIN))]
        for method,operation in cases:
            with self.subTest(method=method),patch.object(self.store,method,side_effect=OSError('/PRIVATE_PATH/PRIVATE_SENTINEL')):
                result = operation()
                self.assertIn(result.status_code,(500,503))
                self.assertNotIn('PRIVATE_PATH',result.text)
                self.assertNotIn('PRIVATE_SENTINEL',result.text)
                self.assertNotIn(STORY,result.text)

    def test_active_call_blocks_archive_mutation_without_contacting_the_importer(self):
        providers = FakeProviders()
        app = create_app(preview=False,providers=providers,archive_store=self.store,archive_importer=self.importer,
                         vad_factory=lambda:TurnDetector(lambda frame:False))
        with TestClient(app) as client:
            entry = self.manual().json()['entry']
            with client.websocket_connect('/api/live',headers=ORIGIN) as socket:
                socket.send_json({'type':'start','character':example().model_dump()})
                self.assertEqual(socket.receive_json()['type'],'ready')
                add = client.post('/api/archive/entries',headers=ORIGIN,json={'entry_id':'123'})
                remove = client.delete('/api/archive/entries/'+entry['id'],headers=ORIGIN)
                self.assertEqual(add.status_code,409)
                self.assertEqual(remove.status_code,409)
                self.assertEqual(client.get('/api/archive/entries').status_code,200)
                self.assertEqual(self.importer.calls,[])
                self.assertEqual(providers.spoken,[])
                socket.send_json({'type':'stop'})
        self.assertEqual(len(self.store.list()),1)

    def test_call_starting_during_an_import_prevents_the_late_story_from_being_saved(self):
        started,release = threading.Event(),threading.Event()
        original_fetch = self.importer.fetch
        async def delayed_fetch(entry_id):
            started.set()
            while not release.is_set(): await asyncio.sleep(.001)
            return await original_fetch(entry_id)
        providers = FakeProviders()
        app = create_app(preview=False,providers=providers,archive_store=self.store,archive_importer=self.importer,
                         vad_factory=lambda:TurnDetector(lambda frame:False))
        with TestClient(app) as client,patch.object(self.importer,'fetch',side_effect=delayed_fetch),ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(lambda:client.post('/api/archive/entries',headers=ORIGIN,json={'entry_id':'123'}))
            try:
                self.assertTrue(started.wait(1))
                with client.websocket_connect('/api/live',headers=ORIGIN) as socket:
                    socket.send_json({'type':'start','character':example().model_dump()})
                    self.assertEqual(socket.receive_json()['type'],'ready')
                    release.set()
                    self.assertEqual(pending.result(timeout=1).status_code,409)
                    self.assertEqual(self.store.list(),[])
                    socket.send_json({'type':'stop'})
            finally: release.set()


class ArchiveSourceTests(unittest.IsolatedAsyncioTestCase):
    def test_plain_text_preserves_story_words_unicode_and_paragraphs_without_executing_markup(self):
        value = '<p>First &amp; foremost — القاهرة.</p><script>PRIVATE_SCRIPT()</script><style>.PRIVATE_STYLE{}</style><p>Second <b>thought</b>.<br>Last line…</p>'
        result = description_text(value)
        self.assertEqual(result,'First & foremost — القاهرة.\nSecond thought.\nLast line…')
        self.assertNotIn('PRIVATE_SCRIPT',result)
        self.assertNotIn('PRIVATE_STYLE',result)
        self.assertNotIn('<b>',result)

    async def test_connector_reads_one_canonical_id_and_returns_only_the_selected_plain_text(self):
        requests = []
        def serve(request):
            requests.append(request)
            return httpx.Response(200,json={'short_id':'123','description':'<p>Selected — القاهرة.</p>'})
        connector = QueeringTheMap(transport=httpx.MockTransport(serve))
        actual = await connector.fetch('https://www.queeringthemap.com/moment/123')
        self.assertEqual(len(requests),1)
        self.assertEqual(str(requests[0].url),SOURCE)
        self.assertEqual(requests[0].method,'GET')
        self.assertEqual(actual['text'],'Selected — القاهرة.')
        self.assertEqual(actual['source_entry_id'],'123')
        self.assertEqual(actual['source_url'],SOURCE)

    async def test_connector_rejects_remote_errors_redirects_invalid_shape_id_mismatch_and_oversize(self):
        responses = [httpx.Response(500,json={'error':'PRIVATE_REMOTE_BODY'}),
            httpx.Response(302,headers={'location':'http://127.0.0.1/private'}),
            httpx.Response(200,text='PRIVATE_REMOTE_BODY',headers={'content-type':'text/html'}),
            httpx.Response(200,text='{PRIVATE_REMOTE_BODY',headers={'content-type':'application/json'}),
            httpx.Response(200,json=[]), httpx.Response(200,json={'short_id':'124','description':'Wrong entry'}),
            httpx.Response(200,json={'short_id':'123','description':None}),
            httpx.Response(200,json={'short_id':'123','description':'<script>PRIVATE_REMOTE_BODY</script>'}),
            httpx.Response(200,json={'short_id':'123','description':'x'*12001}),
            httpx.Response(200,content=b'X'*(64*1024+1),headers={'content-type':'application/json'})]
        for response in responses:
            calls = []
            def serve(request): calls.append(request);return response
            connector = QueeringTheMap(transport=httpx.MockTransport(serve))
            with self.subTest(response=response):
                with self.assertRaises(ArchiveSourceUnavailable) as raised: await connector.fetch('123')
                self.assertEqual(len(calls),1)
                self.assertNotIn('PRIVATE_REMOTE_BODY',str(raised.exception))
                self.assertNotIn('127.0.0.1',str(raised.exception))

    async def test_invalid_id_never_opens_a_remote_request(self):
        def forbidden(request): self.fail('An invalid selection must not make a request')
        connector = QueeringTheMap(transport=httpx.MockTransport(forbidden))
        for value in ('https://evil.invalid/moment/123','١٢٣','https://www.queeringthemap.com/data/descriptions.json'):
            with self.subTest(value=value),self.assertRaises(ValueError): await connector.fetch(value)
        self.assertEqual(qtm_entry_id('123'),'123')
        self.assertEqual(qtm_entry_id(SOURCE),'123')


if __name__ == '__main__':unittest.main()
