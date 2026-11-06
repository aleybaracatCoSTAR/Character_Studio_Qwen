"""Manual gallery FTS must not build the all-catalogue fuzzy voice index."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from live_voice.app import create_app
from live_voice.archive_database import ArchiveDatabase
from live_voice.collection_catalog import CollectionCatalog
from live_voice.collections import CollectionDatabase
import live_voice.collections as collections


def record(number, title, **metadata):
    return {'id':f'{number:032x}', 'title':title, 'text':'Official catalogue description.',
            'source_url':f'https://museum.example/works/{number}',
            'image_url':f'https://museum.example/images/{number}.jpg',
            'metadata':metadata}


class GallerySearchTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.store=CollectionDatabase(Path(self.folder.name)/'collections')
        self.qtm=ArchiveDatabase(Path(self.folder.name)/'qtm')
        self.catalog=CollectionCatalog(self.qtm,self.store)
        self.store.import_records([record(1,'Queer Fish',artist='Mabel Dwight'),
                                   record(2,'Water Lilies',artist='Claude Monet')],
                                  collection_id='first-museum',label='First museum')
        self.store.import_records([record(3,'Queer Fish, another impression',artist='Mabel Dwight'),
                                   record(4,'Fish market',artist='Another artist')],
                                  collection_id='second-museum',label='Second museum')

    def tearDown(self):
        self.store.close();self.qtm.close();self.folder.cleanup()

    def test_literal_title_and_artist_search_use_existing_fts_without_building_index(self):
        with patch('live_voice.collections._artwork_index',side_effect=AssertionError('No full catalogue scan.')):
            page=self.store.page(['first-museum'],query='Queer Fish',literal=True)
            self.assertEqual([entry['title'] for entry in page['entries']],['Queer Fish'])
            self.assertEqual((page['total'],page['matched']),(2,1))
            artist=self.store.page(['first-museum'],query='Monet',literal=True)
            self.assertEqual([entry['metadata']['artist'] for entry in artist['entries']],['Claude Monet'])
        self.assertIsNone(self.store._retrieval_index)

    def test_literal_search_keeps_collection_scope_pagination_and_source_labels(self):
        with patch('live_voice.collections._artwork_index',side_effect=AssertionError('No fuzzy index.')):
            first=self.catalog.page(['first-museum','second-museum'],query='Queer Fish',limit=1,literal=True)
            second=self.catalog.page(['first-museum','second-museum'],query='Queer Fish',offset=1,limit=1,literal=True)
            empty=self.catalog.page(['first-museum'],query='NoSuchArtwork',literal=True)
        self.assertEqual((first['total'],first['matched']),(4,2))
        self.assertEqual(first['entries'][0]['collection_id'],'first-museum')
        self.assertEqual(first['entries'][0]['collection_label'],'First museum')
        self.assertEqual(second['entries'][0]['collection_id'],'second-museum')
        self.assertEqual(empty['entries'],[])

    def test_empty_literal_query_and_invalid_modes_never_build_index(self):
        with patch('live_voice.collections._artwork_index',side_effect=AssertionError('No fuzzy index.')):
            page=self.store.page(['first-museum'],literal=True)
            self.assertEqual((page['total'],page['matched'],len(page['entries'])),(2,2,2))
            with self.assertRaises(KeyError):self.store.page(['unknown'],query='Fish',literal=True)
            with self.assertRaises(ValueError):self.store.page(['first-museum'],literal='literal')
            with self.assertRaises(ValueError):self.catalog.page(['first-museum'],literal=1)

    def test_smart_remains_default_and_literal_does_not_consult_an_existing_fuzzy_index(self):
        with patch('live_voice.collections._artwork_index',wraps=collections._artwork_index) as build:
            default=self.store.page(['first-museum'],query='Queer Fish')
            self.assertEqual(default['entries'][0]['title'],'Queer Fish')
            self.assertEqual(build.call_count,1)
        with patch.object(self.store._retrieval_index,'ranked',side_effect=AssertionError('Literal cannot use fuzzy rankings.')):
            literal=self.store.page(['first-museum'],query='Queer Fish',literal=True)
            self.assertEqual(literal['entries'][0]['title'],'Queer Fish')

    def test_http_literal_mode_is_allowlisted_and_defaults_to_existing_smart(self):
        app=create_app(preview=True,archive_store=self.qtm,collections_store=self.store)
        with TestClient(app) as client:
            with patch('live_voice.collections._artwork_index',side_effect=AssertionError('Gallery must not build full index.')):
                response=client.get('/api/archive/collections/entries',params={
                    'ids':'first-museum','q':'Queer Fish','search_mode':'literal','limit':18})
                self.assertEqual(response.status_code,200)
                self.assertEqual([entry['id'] for entry in response.json()['entries']],[f'{1:032x}'])
                self.assertEqual(response.json()['collections'],True)
                invalid=client.get('/api/archive/collections/entries',params={
                    'ids':'first-museum','q':'Fish','search_mode':'anything'})
                self.assertEqual(invalid.status_code,422)
            with patch('live_voice.collections._artwork_index',wraps=collections._artwork_index) as build:
                smart=client.get('/api/archive/collections/entries',params={'ids':'first-museum','q':'Queer Fish'})
                self.assertEqual(smart.status_code,200)
                self.assertEqual(build.call_count,1)


if __name__=='__main__':unittest.main()
