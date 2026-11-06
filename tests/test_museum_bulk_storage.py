"""Full imports refresh official records without losing IDs, images or call isolation."""
import copy
from pathlib import Path
import tempfile
import unittest

from live_voice.collections import CollectionDatabase, MAX_DATABASE_ENTRIES
from live_voice.museum_common import SOURCES, museum_record


class BulkStorageTests(unittest.TestCase):
    def test_refresh_replaces_fts_and_is_idempotent_while_call_remains_frozen(self):
        with tempfile.TemporaryDirectory() as folder:
            store = CollectionDatabase(folder)
            spec = SOURCES['aic']
            options = dict(collection_id=spec.collection_id,label=spec.label,source_kind='museum-aic')
            old = museum_record('aic','1','Old botanical print','https://www.artic.edu/artworks/1')
            store.import_records([old],**options)
            call = store.open_snapshot([spec.collection_id])
            disk = Path(call._temporary.name)/'snapshot.sqlite3'
            self.assertTrue(disk.is_file())
            new = {**old,'title':'Modern landscape','text':'A modern landscape painting.'}
            result = store.import_records([new],update_existing=True,**options)
            self.assertEqual((result['added'],result['updated'],result['duplicates']),(0,1,0))
            self.assertEqual(store.page([spec.collection_id],query='botanical')['matched'],0)
            self.assertEqual(store.page([spec.collection_id],query='landscape')['matched'],1)
            self.assertEqual(call.search('botanical')[0]['id'],old['id'])
            self.assertEqual(call.search('landscape'),[])
            duplicate = store.import_records([new],update_existing=True,**options)
            self.assertEqual((duplicate['added'],duplicate['updated'],duplicate['duplicates']),(0,0,1))
            call.close(); self.assertFalse(disk.exists()); store.close()

    def test_met_bulk_metadata_keeps_an_existing_eligible_image(self):
        with tempfile.TemporaryDirectory() as folder:
            store = CollectionDatabase(folder); spec = SOURCES['met']
            options = dict(collection_id=spec.collection_id,label=spec.label,source_kind='museum-met')
            old = museum_record('met','1','A portrait','https://www.metmuseum.org/art/collection/search/1',
                image_url='https://images.metmuseum.org/CRDImages/ep/web-large/1.jpg',
                metadata={'artist':'Old attribution','rights':'Image: public domain under the Met Open Access policy.'})
            store.import_records([old],**options)
            incoming = copy.deepcopy(old); incoming['image_url'] = None
            incoming['metadata'] = {'artist':'Corrected attribution','rights':
                'Catalogue metadata: CC0. The CSV contains no image URLs; '
                'image availability is checked on request. Source public-domain flag: True.'}
            store.import_records([incoming],update_existing=True,**options)
            saved = store.page([spec.collection_id])['entries'][0]
            self.assertEqual(saved['image_url'],old['image_url'])
            self.assertEqual(saved['metadata']['artist'],'Corrected attribution')
            self.assertEqual(saved['metadata']['rights'],old['metadata']['rights'])
            restricted = copy.deepcopy(incoming)
            restricted['metadata']['rights'] = 'Collection metadata: CC0. No public-domain image selected.'
            store.import_records([restricted],update_existing=True,**options)
            self.assertIsNone(store.page([spec.collection_id])['entries'][0]['image_url'])
            self.assertEqual(store.page([spec.collection_id])['entries'][0]['metadata']['rights'],restricted['metadata']['rights'])
            store.close()

    def test_custom_conflicts_and_failed_batches_remain_atomic(self):
        with tempfile.TemporaryDirectory() as folder:
            store = CollectionDatabase(folder)
            record = museum_record('aic','1','Original','https://www.artic.edu/artworks/1')
            options = dict(collection_id='art-institute-of-chicago',label='Chicago',source_kind='museum-aic')
            store.import_records([record],**options)
            changed = {**record,'title':'Changed'}
            with self.assertRaises(ValueError): store.import_records([changed],**options)
            with self.assertRaises(ValueError):
                store.import_records([changed,{'bad':'row'}],update_existing=True,**options)
            self.assertEqual(store.page(['art-institute-of-chicago'])['entries'][0]['title'],'Original')
            with self.assertRaises(ValueError):
                store.import_records([],collection_id='custom',label='Custom',update_existing=True)
            self.assertGreaterEqual(MAX_DATABASE_ENTRIES,2_000_000)
            store.close()
