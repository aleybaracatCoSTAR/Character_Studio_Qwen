"""CPU-only catalogue retrieval regressions: canonical facts, no model/network."""
from concurrent.futures import ThreadPoolExecutor
import json
import tempfile
import unittest
from unittest.mock import patch

import live_voice.collections as catalog
from live_voice.artwork_retrieval import (ArtworkIndex, MAX_ALLOWED_SETS, MAX_CANDIDATES,
                                         one_edit, retrieval_terms, spoken_years, spoken_artwork_query)
from live_voice.collections import CollectionDatabase, CollectionMatch, NGA_COLLECTION_ID


TITLE = 'Madonna and Child Enthroned with Twelve Angels, and with the Blessing Christ [middle panel]'


def artwork(number, title, artist, date, **extra):
    return {'id':f'{number:032x}', 'title':title, 'text':f'Canonical catalogue record {number}.',
            'source_entry_id':str(number), 'source_url':f'https://www.nga.gov/artworks/{number}',
            'image_url':f'https://api.nga.gov/iiif/fixture-{number}/full/!400,400/0/default.jpg',
            'metadata':{'artist':artist, 'date':date, 'rights':'CC0 fixture metadata'}, **extra}


FIXTURES = [
    artwork(2,'Saint Andrew and Saint Benedict with the Archangel Gabriel [left panel]','Agnolo Gaddi','shortly before 1387'),
    artwork(3,TITLE,'Agnolo Gaddi','shortly before 1387'),
    artwork(4,'Saint Bernard and Saint Catherine of Alexandria with the Virgin of the Annunciation [right panel]','Agnolo Gaddi','shortly before 1387'),
    artwork(344,'The Coronation of the Virgin with Six Angels','Agnolo Gaddi','c. 1390'),
    artwork(800,'The Circle of the Thieves; Agnolo Brunelleschi Attacked by a Six-Footed Serpent','William Blake','1827'),
    artwork(8572,'The Virgin and Child with Two Angels','Lucas van Leyden','1523'),
    artwork(44013,'Madonna and Child Enthroned with Two Angels','Moderno','1552'),
    artwork(59127,'Don Quixote','Endre Szasz','1948',text='Accession 1387. A garden with an apple tree.'),
    artwork(6217,"Papa contemplant l'image de son image",'Honoré Daumier','1847'),
]


class ArtworkRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = CollectionDatabase(self.directory.name)
        self.store.import_records(FIXTURES, collection_id=NGA_COLLECTION_ID, label='National Gallery of Art', source_kind='nga')
        self.snapshot = self.store.open_snapshot([NGA_COLLECTION_ID])

    def tearDown(self):
        self.snapshot.close(); self.store.close(); self.directory.cleanup()

    def ids(self, query, snapshot=None):
        return [hit['source_entry_id'] for hit in (snapshot or self.snapshot).search(query)]

    def test_exact_and_two_transcription_errors_retrieve_the_canonical_specific_title_only(self):
        for query in ('Madonna and Child Enthroned with Twelve Angels',
                      'Madonne and Child Entrhoned with Twelve Angels',
                      'Show me the artwork called Madonne and Child Entrhoned with Twelve Angels',
                      'Please look for the painting named Madonna and Child Enthroned with Twelve Angels.'):
            with self.subTest(query=query):
                hits = self.snapshot.search(query)
                self.assertEqual([hit['source_entry_id'] for hit in hits], ['3'])
                self.assertEqual(hits[0]['title'], TITLE)
                self.assertEqual(hits[0]['metadata']['artist'], 'Agnolo Gaddi')
                CollectionMatch.model_validate(hits[0])

    def test_joined_and_misspelled_artist_queries_keep_real_multiple_works_not_blake_first_name_matches(self):
        for query in ('AgnoloGadi','AgnoloGaddi','Agnolo Gadi','Agnolo Gaddi','Find the image by AgnoloGadi'):
            with self.subTest(query=query):
                hits = self.snapshot.search(query)
                self.assertEqual({hit['source_entry_id'] for hit in hits}, {'2','3','4'})
                self.assertTrue(all(hit['metadata']['artist'] == 'Agnolo Gaddi' for hit in hits))

    def test_numeric_and_actual_spoken_years_match_date_fields_and_preserve_triptych_ambiguity(self):
        queries = ('1387','Show me the painting from 1387','thirteen eighty-seven',
                   'from thirteen eighty seven','painted in 1387','shortly before thirteen eighty seven',
                   'fromthirteeneightyseven','paintedin1387','shortlybeforethirteeneightyseven')
        for query in queries:
            with self.subTest(query=query):
                hits = self.snapshot.search(query)
                self.assertEqual({hit['source_entry_id'] for hit in hits}, {'2','3','4'})
                self.assertTrue(all(hit['metadata']['date'] == 'shortly before 1387' for hit in hits))

    def test_year_filters_do_not_use_accessions_or_silently_ignore_a_conflicting_actual_date(self):
        self.assertNotIn('59127', self.ids('1387'))
        self.assertEqual(self.ids('Agnolo Gaddi from 1827'), [])
        self.assertEqual(self.ids('AgnoloGadi from 1948'), [])
        self.assertEqual(self.ids('painted in 1234'), [])
        self.assertEqual(self.ids('Madonne and Child Entrhoned with Twelve Angels from 1387'), ['3'])

    def test_an_absent_numeric_or_spoken_date_never_falls_back_to_an_accession_or_body_number(self):
        confounder = artwork(100,'A later catalogue record','Fixture Artist','1948',
            text='Inventory 1498 appears only in catalogue prose, not in its creation date.')
        confounder['metadata']['accession_number'] = '1498'
        self.store.import_records([confounder],collection_id=NGA_COLLECTION_ID,
            label='National Gallery of Art',source_kind='nga')
        fresh = self.store.open_snapshot([NGA_COLLECTION_ID])
        try:
            for query in ('1498','fourteen ninety-eight','from fourteen ninety eight'):
                with self.subTest(query=query):
                    self.assertEqual(self.ids(query,fresh),[])
                    self.assertEqual(self.store.page([NGA_COLLECTION_ID],query=query)['matched'],0)
        finally: fresh.close()

    def test_unrelated_artist_names_and_short_ambiguous_tokens_are_not_changed_into_known_artists(self):
        for query in ('Agnolo Zyxz','AgnoloGady','Portrait by Zygmund Zyxzz',
                      'Find a work by Mirella Qxyzz','Gadi','XgnoloGadi'):
            with self.subTest(query=query): self.assertEqual(self.ids(query), [])

    def test_unrelated_number_words_are_not_rewritten_as_years(self):
        for query in ('I counted thirteen eighty seven birds','She met thirteen eighty-seven people',
                      'thirteen friends and eighty seven paintings',
                      'I have thirteen eighty seven birds in my garden'):
            with self.subTest(query=query): self.assertEqual(spoken_years(query), set())
        self.assertEqual(spoken_years('thirteen eighty-seven'), {'1387'})
        self.assertEqual(spoken_years('from thirteen hundred and eighty seven'), {'1387'})
        self.assertEqual(spoken_years('painted in eighteen eighty-nine'), {'1889'})

    def test_page_and_call_retrieval_agree_and_page_preserves_all_artist_matches_and_offsets(self):
        page = self.store.page([NGA_COLLECTION_ID],query='AgnoloGadi',limit=2,offset=1)
        self.assertEqual(page['matched'],4)
        self.assertEqual([entry['source_entry_id'] for entry in page['entries']], ['3','4'])
        title = self.store.page([NGA_COLLECTION_ID],query='Madonne and Child Entrhoned with Twelve Angels')
        self.assertEqual(title['matched'],1); self.assertEqual(title['entries'][0]['source_entry_id'],'3')
        date = self.store.page([NGA_COLLECTION_ID],query='thirteen eighty-seven')
        self.assertEqual(date['matched'],3)

    def test_canonical_record_content_is_unchanged_after_spelling_and_date_queries(self):
        before = self.store.page([NGA_COLLECTION_ID])['entries']
        for query in ('AgnoloGadi','Madonne and Child Entrhoned with Twelve Angels','thirteen eighty-seven'):
            self.snapshot.search(query); self.store.page([NGA_COLLECTION_ID],query=query)
        self.assertEqual(self.store.page([NGA_COLLECTION_ID])['entries'],before)
        self.assertEqual(next(item for item in before if item['source_entry_id']=='3')['title'],TITLE)

    def test_plain_body_topics_still_use_fts_without_artwork_name_inference(self):
        self.assertEqual(self.ids('garden apple'),['59127'])
        self.assertEqual(self.ids('unmatched telescope'),[])

    def test_spoken_topic_correction_matches_the_unframed_topic_without_reusing_the_old_artist(self):
        records = [artwork(100,'Landscapes near a river','Fixture Landscape Artist','1900'),
                   artwork(101,"I'm Jealous",'Fixture Artist','1901'),
                   artwork(102,"I'm Christmas",'Fixture Artist','1902')]
        self.store.import_records(records,collection_id=NGA_COLLECTION_ID,label='National Gallery of Art',source_kind='nga')
        fresh=self.store.open_snapshot([NGA_COLLECTION_ID])
        history=[{'role':'user','content':'Show me AgnoloGadi.'},
                 {'role':'assistant','content':'There are works by Agnolo Gaddi.'}]
        try:
            expected=fresh.search('landscapes')
            self.assertEqual([hit['source_entry_id'] for hit in expected],['100'])
            for query in ("Uh no, I'm looking for landscapes",'Actually, I am searching for landscapes',
                          'Okay, we are looking for landscapes','Um, I want to find landscapes'):
                with self.subTest(query=query):
                    self.assertEqual(fresh.search(query,history),expected)
                    self.assertEqual(self.store.page([NGA_COLLECTION_ID],query=query)['entries'],
                                     self.store.page([NGA_COLLECTION_ID],query='landscapes')['entries'])
            self.assertEqual([hit['source_entry_id'] for hit in fresh.search('Find the artwork titled "I\'m Jealous"')],['101'])
            self.assertEqual(catalog._query_terms("Uh no, I'm looking for landscapes",history),{'landscapes'})
            self.assertEqual(history[0]['content'],'Show me AgnoloGadi.')
        finally: fresh.close()

    def test_a_facet_followup_can_borrow_a_cleaned_search_without_changing_history_or_the_transcript(self):
        history=[{'role':'user','content':"Uh, no, I'm looking for Agnolo Gaddi"}]
        self.assertEqual(self.snapshot.search('What year was it painted?',history),self.snapshot.search('Agnolo Gaddi'))
        self.assertEqual(history,[{'role':'user','content':"Uh, no, I'm looking for Agnolo Gaddi"}])

    def test_prefixed_spoken_year_is_cleaned_before_both_query_terms_and_year_normalization(self):
        expected=self.ids('1387')
        for query in ("I'm looking for thirteen eighty-seven", "Uh no, I'm looking for thirteen eighty-seven"):
            with self.subTest(query=query):
                self.assertEqual(catalog._fts_query(query)[0],{'1387'})
                self.assertEqual(self.ids(query),expected)
                page=self.store.page([NGA_COLLECTION_ID],query=query)
                self.assertEqual(page['matched'],3)
                self.assertEqual([entry['source_entry_id'] for entry in page['entries']],expected)

    def test_selected_sources_are_respected_for_exact_and_fuzzy_queries(self):
        self.store.import_records([artwork(100,'The very particular hummingbird','Elisabeth Test','1789')],
                                  collection_id='unselected',label='Not chosen')
        self.assertEqual(self.store.page([NGA_COLLECTION_ID],query='Elisabeth Test')['matched'],0)
        self.assertEqual(self.store.page(['unselected'],query='ElisabethTest')['matched'],1)
        self.assertEqual(self.ids('ElisabethTest'),[])

    def test_store_index_is_reused_then_rebuilt_once_after_a_successful_import_not_after_rollback(self):
        with patch.object(catalog,'_artwork_index', wraps=catalog._artwork_index) as build:
            self.store.page([NGA_COLLECTION_ID],query='AgnoloGadi')
            first = self.store._retrieval_index
            self.store.page([NGA_COLLECTION_ID],query='1387')
            self.assertIs(self.store._retrieval_index,first); self.assertEqual(build.call_count,1)
            self.store.import_records([artwork(100,'New test work','Agnolo Gaddi','1400')],
                collection_id=NGA_COLLECTION_ID,label='National Gallery of Art',source_kind='nga')
            self.assertEqual(self.store.page([NGA_COLLECTION_ID],query='AgnoloGadi')['matched'],5)
            second = self.store._retrieval_index
            self.assertIsNot(second,first); self.assertEqual(build.call_count,2)
            with self.assertRaises(ValueError):
                self.store.import_records([artwork(101,'A late invalid row','Agnolo Gaddi','1400',source_url='http://private/')],
                    collection_id=NGA_COLLECTION_ID,label='National Gallery of Art',source_kind='nga')
            self.store.page([NGA_COLLECTION_ID],query='AgnoloGadi')
            self.assertIs(self.store._retrieval_index,second); self.assertEqual(build.call_count,2)

    def test_external_database_commit_refreshes_page_cache_but_cannot_change_existing_call_snapshot(self):
        self.assertEqual(self.store.page([NGA_COLLECTION_ID],query='AgnoloGadi')['matched'],4)
        original = self.store._retrieval_index
        other = CollectionDatabase(self.directory.name)
        try:
            other.import_records([artwork(100,'External new work','Agnolo Gaddi','1400')],
                collection_id=NGA_COLLECTION_ID,label='National Gallery of Art',source_kind='nga')
        finally: other.close()
        self.assertEqual(self.store.page([NGA_COLLECTION_ID],query='AgnoloGadi')['matched'],5)
        self.assertIsNot(self.store._retrieval_index,original)
        self.assertEqual(self.ids('1400'),[])
        fresh = self.store.open_snapshot([NGA_COLLECTION_ID])
        try: self.assertEqual(self.ids('1400',fresh),['100'])
        finally: fresh.close()

    def test_warmed_queries_do_not_rescan_all_catalogue_rows_or_rebuild_vocabulary(self):
        statements=[]; self.snapshot._connection.set_trace_callback(statements.append)
        with patch.object(catalog,'_artwork_index',side_effect=AssertionError('Must be prepared before speech.')):
            self.ids('AgnoloGadi'); self.ids('Madonne and Child Entrhoned with Twelve Angels'); self.ids('1387')
        self.assertFalse(any('SELECT rowid,collection_id,title,metadata FROM collection_entries' in sql for sql in statements))
        ranked=[sql for sql in statements if 'SELECT rowid,title,metadata' in sql]
        self.assertTrue(ranked)
        self.assertTrue(all('WHERE rowid IN (' in sql for sql in ranked))
        self.assertTrue(all(sql.count(',') < MAX_CANDIDATES+3 for sql in ranked))

    def test_group_selection_cache_is_bounded_and_never_includes_an_unselected_record(self):
        for number in range(12):
            self.store.import_records([artwork(100+number,'Side work','Side Artist','1500')],
                collection_id='side-'+str(number),label='Side')
        self.store.page([NGA_COLLECTION_ID],query='AgnoloGadi')
        index=self.store._retrieval_index
        for number in range(12):
            plan=index.ranked(self.store._connection,[NGA_COLLECTION_ID,'side-'+str(number)],'AgnoloGadi',{'agnologadi'})
            self.assertTrue(plan); self.assertLessEqual(len(index._allowed_cache),MAX_ALLOWED_SETS)
        self.assertEqual(self.store.page(['side-0'],query='AgnoloGadi')['matched'],0)

    def test_shared_snapshot_queries_are_thread_safe_and_close_releases_auxiliary_memory(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            results=list(pool.map(self.ids,['AgnoloGadi','thirteen eighty-seven','Madonne and Child Entrhoned with Twelve Angels']))
        self.assertEqual(results[2],['3'])
        self.snapshot.close(); self.assertIsNone(self.snapshot._retrieval_index)
        self.store.page([NGA_COLLECTION_ID],query='1387'); self.store.close(); self.assertIsNone(self.store._retrieval_index)

    def test_grounding_policy_preserves_canonical_spelling_and_requests_confirmation_without_inventing_an_alias(self):
        self.assertIn('canonical spelling',catalog.COLLECTION_GROUNDING_POLICY)
        self.assertIn('do not reject a relevant candidate solely for a spelling difference',catalog.COLLECTION_GROUNDING_POLICY)
        self.assertIn('ask for confirmation',catalog.COLLECTION_GROUNDING_POLICY)
        self.assertIn('do not assert an inferred artist alias as fact',catalog.COLLECTION_GROUNDING_POLICY)


class LexicalBoundsTests(unittest.TestCase):
    def test_spoken_search_cleanup_is_anchored_bounded_and_preserves_literal_titles_and_meaningful_no(self):
        for query in ("Uh no, I'm looking for landscapes",'Um, I am searching for landscapes',
                      'Actually, we’re looking for landscapes','Okay, I want to find landscapes'):
            with self.subTest(query=query): self.assertEqual(spoken_artwork_query(query),'landscapes')
        for query in ("I'm Jealous",'No Woman No Cry','Find the work called "I\'m Jealous"',
                      '"I\'m looking for landscapes"','Uh Huh','No, I prefer paintings',
                      'Tell me why I am looking for landscapes'):
            with self.subTest(query=query): self.assertEqual(spoken_artwork_query(query),query)
        self.assertEqual(len(spoken_artwork_query('x'*5000)),4000)

    def test_only_one_edit_or_adjacent_transposition_is_permitted(self):
        for first,second in [('gadi','gaddi'),('entrhoned','enthroned'),('madonne','madonna')]:
            self.assertTrue(one_edit(first,second))
        self.assertFalse(one_edit('gady','gaddi')); self.assertFalse(one_edit('random','gaddi'))

    def test_broad_fuzzy_buckets_decline_to_guess_and_generic_framing_is_removed(self):
        bucket={('ma',7):['madonna']+['ma'+str(number).zfill(5) for number in range(600)]}
        self.assertEqual(ArtworkIndex._near('madonne',bucket),())
        self.assertEqual(retrieval_terms('Find the image by AgnoloGadi',{'find','image','agnologadi'}),{'agnologadi'})
        self.assertEqual(retrieval_terms('thirteen eighty-seven',{'thirteen','eighty','seven'}),{'1387'})


if __name__ == '__main__': unittest.main()
