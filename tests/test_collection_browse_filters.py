"""Constraint filters run before the three-source cap and remain call-local."""
import tempfile
import unittest
from copy import deepcopy
from unittest.mock import patch

import live_voice.collections as catalog
from live_voice.collections import CollectionDatabase,CollectionRouter,filter_retrieved_matches
from test_artwork_session import artwork,COLLECTION


def record(number,*,medium='oil on canvas',image=True,title=None,artist='Fixture Artist',text=None,alt=''):
    item=artwork(number,artist,title or f'Colourful garden {number}',image=image)
    item['metadata']['medium']=medium
    item['text']=text or 'A vibrant, colourful garden in the supplied catalogue.'
    item['image_alt']=alt
    return item


class CollectionBrowseFilterTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.store=CollectionDatabase(self.directory.name)
        self.snapshots=[]

    def tearDown(self):
        for snapshot in self.snapshots: snapshot.close()
        self.store.close();self.directory.cleanup()

    def import_records(self,records,collection=COLLECTION):
        self.store.import_records(records,collection_id=collection,label='Fixture collection',
            source_kind='nga' if collection==COLLECTION else 'custom')

    def snapshot(self,ids=None):
        snapshot=self.store.open_snapshot(ids or [COLLECTION]);self.snapshots.append(snapshot);return snapshot

    def test_filters_find_eligible_images_after_first_three_no_image_catalogue_matches(self):
        self.import_records([record(n,image=n>3) for n in range(1,8)])
        snapshot=self.snapshot()
        self.assertEqual([hit['source_entry_id'] for hit in snapshot.search('Fixture Artist')],['1','2','3'])
        hits=snapshot.search('Fixture Artist',require_images=True,medium='painting')
        self.assertEqual([hit['source_entry_id'] for hit in hits],['4','5','6'])
        self.assertTrue(all(hit['image_url'] for hit in hits))

    def test_fts_filter_precedes_limit_and_more_excludes_seen_or_failed_keys(self):
        self.import_records([record(n,image=n>4) for n in range(1,12)])
        snapshot=self.snapshot()
        first=snapshot.search('colorful',require_images=True,medium='paintings')
        self.assertEqual(len(first),3)
        excluded={(hit['collection_id'],hit['id']) for hit in first}
        more=snapshot.search('colourful',require_images=True,medium='painting',exclude_keys=excluded)
        self.assertEqual(len(more),3)
        self.assertFalse(excluded & {(hit['collection_id'],hit['id']) for hit in more})
        self.assertTrue(all(int(hit['source_entry_id'])>4 for hit in first+more))

    def test_paint_medium_family_covers_actual_materials_not_caption_mentions(self):
        media=('oil on linen','acrylic on board','watercolour','gouache over graphite','egg tempera on panel')
        titles=('Amber Garden','Cobalt Garden','Dawn Garden','Evening Garden','Forest Garden')
        self.import_records([record(n,medium=medium,title=titles[n-1]) for n,medium in enumerate(media,1)]+[
            record(10,medium='gelatin silver print',title='A painting',text='A colourful painting photographed in a frame.'),
            record(11,medium='bronze',title='Paintings in the garden',text='Vibrant paintings mentioned by the sculptor.')])
        snapshot=self.snapshot()
        for number in range(1,6):
            with self.subTest(number=number):
                hits=snapshot.search(titles[number-1],medium='painting',require_images=True)
                self.assertEqual([hit['source_entry_id'] for hit in hits],[str(number)])
        hits=snapshot.search('',medium='painting',require_images=True)
        self.assertEqual(len(hits),3)
        self.assertFalse({'10','11'} & {hit['source_entry_id'] for hit in hits})

    def test_medium_spelling_plural_aliases_and_specific_medium(self):
        self.import_records([record(1,medium='oil on canvas'),record(2,medium='watercolours'),record(3,medium='woodcut')])
        snapshot=self.snapshot()
        for spelling in ('painting','paintings','paintngs','paintign'):
            with self.subTest(spelling=spelling):
                self.assertEqual({hit['source_entry_id'] for hit in snapshot.search('',medium=spelling)},{'1','2'})
        self.assertEqual([hit['source_entry_id'] for hit in snapshot.search('',medium='watercolour')],['2'])
        self.assertEqual([hit['source_entry_id'] for hit in snapshot.search('',medium='woodcuts')],['3'])

    def test_color_synonyms_and_supplied_visual_description_fallback(self):
        self.import_records([
            record(1,title='Garden 1',text='A colour study.',medium='oil on panel'),
            record(2,title='Garden 2',text='A polychrome study.',medium='tempera'),
            record(3,title='Garden 3',text='A vibrant study.',medium='gouache')])
        snapshot=self.snapshot()
        self.assertEqual(len(snapshot.search('colorful',require_images=True,medium='painting')),3)
        self.import_records([record(10,title='Untitled',text='A catalogue record.',medium='oil on canvas',alt='Red flowers in a blue vase.')],collection='visual-only')
        visual=self.snapshot(['visual-only'])
        hits=visual.search('colorful',require_images=True,medium='painting')
        self.assertEqual([hit['source_entry_id'] for hit in hits],['10'])
        self.assertEqual(hits[0]['image_alt'],'Red flowers in a blue vase.')
        self.assertEqual(visual.search('colorful',require_images=True,medium='woodcut'),[])

    def test_a_named_colorful_title_keeps_its_identity_instead_of_broadening_to_colour_terms(self):
        self.import_records([record(1,title='Colourful Garden'),
            record(2,title='Red Sunset'),record(3,title='A Vibrant Portrait'),record(4,title='Blue Night')])
        snapshot=self.snapshot()
        hits=snapshot.search('Colourful Garden',require_images=True,medium='painting')
        self.assertEqual([hit['source_entry_id'] for hit in hits],['1'])

    def test_browse_index_is_built_only_at_call_setup_and_warm_queries_do_not_scan_json(self):
        self.import_records([record(n) for n in range(1,8)])
        snapshot=self.snapshot();statements=[];snapshot._connection.set_trace_callback(statements.append)
        with patch.object(catalog,'_prepare_browse_index',side_effect=AssertionError('Do not rebuild during speech.')):
            for query in ('colorful','colourful','vibrant',''):
                self.assertEqual(len(snapshot.search(query,require_images=True,medium='painting')),3)
        self.assertFalse(any('SELECT rowid,metadata,image_url,image_alt FROM' in sql for sql in statements))
        self.assertTrue(any('collection_browse' in sql for sql in statements))
        self.assertTrue(all('LIMIT 3' in sql or 'rowid IN (' in sql for sql in statements if sql.startswith('SELECT e.*')))

    def test_snapshot_freezes_availability_and_filters_reject_invalid_arguments(self):
        self.import_records([record(1,image=False),record(2)])
        snapshot=self.snapshot()
        self.import_records([record(3)])
        self.assertEqual([hit['source_entry_id'] for hit in snapshot.search('',require_images=True)],['2'])
        for kwargs in ({'require_images':1},{'medium':'some arbitrary material'},
                       {'exclude_keys':[('other','../private')]},{'exclude_keys':[(COLLECTION,'a'*32)]*251}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):snapshot.search('garden',**kwargs)
        self.assertEqual(snapshot.search(''),[])
        snapshot.close()
        with self.assertRaisesRegex(ValueError,'closed'):snapshot.search('garden',require_images=True)

    def test_current_match_filter_does_not_mutate_sources_or_admit_nonpublic_images(self):
        records=[{**record(1),'collection_id':COLLECTION},{**record(2),'collection_id':COLLECTION},
                 {**record(3,medium='woodcut'),'collection_id':COLLECTION}]
        records[1]['image_url']='http://127.0.0.1/private'
        before=deepcopy(records)
        hits=filter_retrieved_matches(records,require_images=True,medium='paintings')
        self.assertEqual(hits,[records[0]])
        self.assertEqual(records,before)
        self.assertEqual(filter_retrieved_matches(records,require_images=True,exclude_keys=[(COLLECTION,records[0]['id'])]),[records[2]])

    def test_router_applies_filters_per_art_snapshot_and_skips_legacy_qtm_image_search(self):
        self.import_records([record(1,image=False),record(2),record(3,medium='woodcut')])
        self.import_records([record(4),record(5)],collection='second-museum')
        first=self.snapshot();second=self.snapshot(['second-museum'])
        class LegacyQTM:
            count=123
            def search(self,query,history=()): raise AssertionError('Images must not query the story archive.')
            def close(self): pass
        router=CollectionRouter([LegacyQTM(),first,second])
        hits=router.search('colorful',require_images=True,medium='painting',exclude_keys=[(COLLECTION,record(2)['id'])])
        self.assertEqual({hit['source_entry_id'] for hit in hits},{'4','5'})
        router.close()


if __name__=='__main__': unittest.main()
