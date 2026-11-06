"""Museum selections enter the existing local RAG path without active-call writes."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from live_voice.app import create_app
from live_voice.archive_database import ArchiveDatabase
from live_voice.collections import CollectionDatabase
from live_voice.museum_common import MuseumImportError, SOURCES, museum_record
from live_voice.turns import TurnDetector
from test_collection_integration import Providers


class Importer:
    def __init__(self): self.calls=[]; self.error=None
    def import_collection(self,store,source,query,limit):
        self.calls.append((source,query,limit))
        if self.error: raise self.error
        entry = museum_record(source,'123','Monet catalogue test',SOURCES[source].source_url,
            metadata={'artist':'Claude Monet','rights':'CC0'}, image_url='https://example.org/test.jpg')
        spec=SOURCES[source]
        return {'source':source,**store.import_records([entry],collection_id=spec.collection_id,
            label=spec.label,source_kind='museum-'+source,source_url=spec.source_url)}


class MuseumAPITests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(); root=Path(self.temporary.name)
        self.qtm=ArchiveDatabase(root/'qtm'); self.art=CollectionDatabase(root/'art'); self.importer=Importer()
        self.app=create_app(providers=Providers(),archive_store=self.qtm,collections_store=self.art,
            museum_importer=self.importer,vad_factory=lambda:TurnDetector(lambda frame:False))
        self.headers={'origin':'http://testserver'}
        self.payload={'source':'aic','query':'Monet','limit':25}

    def tearDown(self):
        self.qtm.close(); self.art.close(); self.temporary.cleanup()

    def test_available_museums_are_not_mistaken_for_populated_collections(self):
        with TestClient(self.app) as client:
            response=client.get('/api/archive/museums')
            self.assertEqual(response.status_code,200)
            self.assertEqual({source['id'] for source in response.json()['sources']},set(SOURCES))
            self.assertEqual(response.json()['guide_url'],'https://www.navigating.art/education/open-access-2026')
            self.assertEqual({source['id'] for source in client.get('/api/archive/collections').json()['collections']},
                             {'qtm','national-gallery-of-art'})

    def test_import_persists_metadata_and_routes_it_into_existing_local_search(self):
        before=self.qtm.path.read_bytes()
        with TestClient(self.app) as client:
            response=client.post('/api/archive/museums/import',json=self.payload,headers=self.headers)
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['errors'],[])
            self.assertEqual(response.json()['results'][0]['added'],1)
            saved=client.get('/api/archive/collections/entries',params={'ids':'art-institute-of-chicago','q':'Monet'}).json()
            self.assertEqual(saved['matched'],1)
            self.assertEqual(saved['entries'][0]['metadata']['artist'],'Claude Monet')
            self.assertEqual(saved['entries'][0]['collection_label'],'Art Institute of Chicago')
            duplicate=client.post('/api/archive/museums/import',json=self.payload,headers=self.headers).json()['results'][0]
            self.assertEqual((duplicate['added'],duplicate['duplicates'],duplicate['total']),(0,1,1))
            snapshot=self.art.open_snapshot(['art-institute-of-chicago'])
            try:
                self.assertEqual(snapshot.search('Monet')[0]['source_kind'],'museum-aic')
            finally: snapshot.close()
        self.assertEqual(self.qtm.path.read_bytes(),before)

    def test_cross_origin_and_malformed_imports_never_start_network_or_write(self):
        with TestClient(self.app) as client:
            self.assertEqual(client.post('/api/archive/museums/import',json=self.payload).status_code,403)
            self.assertEqual(client.post('/api/archive/museums/import',json=self.payload,headers={'origin':'https://elsewhere.example'}).status_code,403)
            for value in ({**self.payload,'source':'https://127.0.0.1'}, {**self.payload,'query':''},
                          {**self.payload,'query':'x'*201},{**self.payload,'query':'Monet\nprivate'},
                          {**self.payload,'limit':201},{**self.payload,'limit':True},
                          {**self.payload,'source':[]},{**self.payload,'url':'https://example.org'},['aic']):
                self.assertEqual(client.post('/api/archive/museums/import',json=value,headers=self.headers).status_code,400)
            self.assertEqual(client.post('/api/archive/museums/import',content='x'*2049,
                headers={**self.headers,'content-type':'application/json'}).status_code,413)
            self.assertEqual(client.post('/api/archive/museums/import',content='{}',headers=self.headers).status_code,400)
        self.assertEqual(self.importer.calls,[]); self.assertEqual(self.art.count(),0)

    def test_upstream_failure_is_explicit_and_does_not_expose_response_content(self):
        self.importer.error=MuseumImportError('aic','rate_limited')
        with TestClient(self.app) as client:
            response=client.post('/api/archive/museums/import',json=self.payload,headers=self.headers)
            self.assertEqual(response.json()['results'],[])
            self.assertEqual(response.json()['errors'][0]['reason'],'rate_limited')
        self.assertEqual(self.art.count(),0)

    def test_active_call_blocks_an_import_before_worker_starts(self):
        with TestClient(self.app) as client:
            with client.websocket_connect('/api/live',headers=self.headers) as socket:
                # The call owns call_lock while awaiting its initial config.
                result=client.post('/api/archive/museums/import',json=self.payload,headers=self.headers)
                self.assertEqual(result.status_code,409)
                socket.send_text(json.dumps({'type':'bad'}))
                socket.receive_json()
        self.assertEqual(self.importer.calls,[])

    def test_unknown_image_identifiers_do_not_fetch_from_the_network(self):
        with patch('live_voice.museum_images.httpx.AsyncClient') as upstream:
            with TestClient(self.app) as client:
                self.assertEqual(client.get('/api/archive/museum-images/aic/not-a-uuid').status_code,404)
                self.assertEqual(client.get('/api/archive/museum-images/aic/12345678-1234-1234-1234-123456789abc').status_code,404)
            upstream.assert_not_called()


if __name__=='__main__': unittest.main()
