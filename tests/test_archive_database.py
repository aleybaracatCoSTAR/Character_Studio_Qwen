"""Synthetic, CPU-only SQLite archive and frozen-call retrieval checks."""
from concurrent.futures import ThreadPoolExecutor
import csv
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from live_voice.archive import ArchiveEntry, ArchiveStore, MANIFEST_NAME, grounding_messages
from live_voice import archive_database as database


class ArchiveDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.store = database.ArchiveDatabase(self.root / 'archive')
        self.addCleanup(self.store.close)

    def file(self, value, suffix='.json'):
        path = self.root / ('input' + suffix)
        if isinstance(value, bytes): path.write_bytes(value)
        elif isinstance(value, str): path.write_text(value, encoding='utf-8')
        else: path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        return path

    def add(self, number, text, **kwargs):
        return self.store.add(text=text, source_url=f'https://www.queeringthemap.com/moment/{number}', **kwargs)

    def test_private_storage_and_full_selected_entry_compatibility(self):
        added = self.add(123, 'A synthetic library memory.', provenance='fetched')
        self.assertEqual(self.store.path.name, 'archive.sqlite3')
        self.assertEqual(self.store.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(self.store.path) as connection:
            self.assertEqual(connection.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(added['title'], 'Queering the Map #123')
        self.assertEqual(self.store.list(), [added])
        selected = self.store.snapshot([added['id']])
        self.assertIsInstance(selected, tuple)
        self.assertEqual(selected[0].model_dump(), added)
        modified = self.store.list(); modified[0]['text'] = 'changed'
        self.assertEqual(self.store.list(), [added])
        with self.assertRaises(KeyError): self.store.snapshot(['f' * 32])
        with self.assertRaises(ValueError): self.store.snapshot([added['id'], added['id']])
        self.assertFalse(self.store.remove('missing'))
        self.assertTrue(self.store.remove(added['id']))
        self.assertFalse(self.store.remove(added['id']))
        self.assertEqual(self.store.count(), 0)

    def test_deduplication_preserves_original_id_provenance_and_distinct_revisions(self):
        first = self.add(7, 'Original supplied words.', title='Original label')
        duplicate = self.add(7, 'Original supplied words.', title='Another label', provenance='fetched')
        revised = self.add(7, 'A changed account.', provenance='fetched')
        self.assertEqual(first, duplicate)
        self.assertEqual(duplicate['provenance'], 'pasted')
        self.assertNotEqual(first['id'], revised['id'])
        self.assertEqual(self.store.count(), 2)

    def test_thousand_entry_import_pages_search_and_legacy_list_stay_bounded(self):
        rows = [{'id': i + 1, 'text': f'Synthetic passage marker{i:04d} about a library.'} for i in range(1200)]
        result = self.store.import_file(self.file({'entries': rows}))
        self.assertEqual(result, {'added': 1200, 'duplicates': 0, 'total': 1200})
        self.assertEqual(self.store.count(), 1200)
        self.assertEqual(len(self.store.list()), 50)
        first = self.store.page(limit=25)
        second = self.store.page(offset=25, limit=25)
        self.assertEqual((first['total'], first['matched'], first['offset'], first['limit']), (1200, 1200, 0, 25))
        self.assertTrue(set(e['id'] for e in first['entries']).isdisjoint(e['id'] for e in second['entries']))
        match = self.store.page(query='marker1137')
        self.assertEqual((match['total'], match['matched']), (1200, 1))
        self.assertEqual(match['entries'][0]['source_entry_id'], '1138')
        self.assertEqual(self.store.page(query='astronomy')['entries'], [])
        self.assertEqual(self.store.page(query='Tell me a story')['matched'], 0)
        self.assertEqual(self.store.page(offset=1500)['entries'], [])

    def test_official_id_map_html_conversion_ignores_coordinates_and_never_claims_fetched(self):
        source = self.file({'00123': '<p>A café story.<br>We kissed.</p><script>DO NOT SAVE</script>', '124': 'Plain words.'})
        self.assertEqual(self.store.import_file(source)['added'], 2)
        first = self.store.list()[0]
        self.assertEqual(first['source_entry_id'], '123')
        self.assertEqual(first['source_url'], 'https://www.queeringthemap.com/moment/123')
        self.assertEqual(first['text'], 'A café story.\nWe kissed.')
        self.assertEqual(first['provenance'], 'imported')
        self.assertNotIn('DO NOT SAVE', first['text'])
        self.store.import_file(self.file([{'id': 125, 'description': '<b>Other words.</b>', 'longitude': 999,
                                          'latitude': 888, 'provenance': 'fetched'}]))
        last = self.store.list()[-1]
        self.assertEqual(last['text'], 'Other words.')
        self.assertNotIn('latitude', last)
        self.assertEqual(last['provenance'], 'imported')

    def test_jsonl_and_csv_are_streamed_bom_safe_and_multiline_text_is_preserved(self):
        jsonl = '\ufeff' + json.dumps({'short_id': '10', 'description': '<p>A JSONL story.</p>'}) + '\n\n'
        self.assertEqual(self.store.import_file(self.file(jsonl, '.jsonl'))['added'], 1)
        path = self.root / 'records.csv'
        with path.open('w', newline='', encoding='utf-8-sig') as stream:
            writer = csv.DictWriter(stream, fieldnames=['source_entry_id', 'title', 'text'])
            writer.writeheader(); writer.writerow({'source_entry_id': '11', 'title': 'A label', 'text': 'One line.\nAnother line.'})
        self.assertEqual(self.store.import_file(path)['added'], 1)
        self.assertEqual(self.store.list()[-1]['text'], 'One line.\nAnother line.')
        self.assertEqual(self.store.import_file(path), {'added': 0, 'duplicates': 1, 'total': 2})

    def test_import_dedupes_same_source_and_text_without_changing_pasted_provenance(self):
        first = self.add(2, 'Existing original words.')
        result = self.store.import_file(self.file([{'source_entry_id': '0002', 'text': 'Existing original words.', 'provenance': 'fetched'},
                                                  {'source_entry_id': '2', 'text': 'A distinct revision.'}]))
        self.assertEqual(result, {'added': 1, 'duplicates': 1, 'total': 2})
        self.assertEqual(self.store.list()[0], first)
        self.assertEqual(self.store.list()[1]['provenance'], 'imported')

    def test_invalid_later_record_rolls_back_data_and_index_and_has_no_private_error_text(self):
        self.add(1, 'Already stored.')
        source = self.file([{'id': '2', 'text': 'Should roll back. uniqueprobe'},
                            {'id': '3', 'text': 'PRIVATE_SENTINEL' * 1000}])
        with self.assertRaises(ValueError) as error: self.store.import_file(source)
        self.assertIn('row 2', str(error.exception))
        self.assertNotIn('PRIVATE_SENTINEL', str(error.exception))
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(self.store.page(query='uniqueprobe')['matched'], 0)
        self.assertEqual(self.store.page(query='Already')['matched'], 1)

    def test_capacity_failure_rolls_back_entire_import_but_duplicates_are_allowed_at_cap(self):
        self.add(1, 'Existing words.')
        source = self.file([{'id': '2', 'text': 'First new.'}, {'id': '3', 'text': 'Second new.'}])
        with patch.object(database, 'MAX_DATABASE_ENTRIES', 2):
            with self.assertRaises(OverflowError): self.store.import_file(source)
        self.assertEqual(self.store.count(), 1)
        with patch.object(database, 'MAX_DATABASE_ENTRIES', 1):
            self.assertEqual(self.store.import_file(self.file({'1': 'Existing words.'}))['duplicates'], 1)

    def test_invalid_shapes_ids_sources_duplicate_keys_and_unknown_formats_fail_safely(self):
        cases = [
            ({'entries': [1]}, '.json'),
            ({'not-a-number': 'Words'}, '.json'),
            ([{'id': 0, 'text': 'Words'}], '.json'),
            ([{'id': True, 'text': 'Words'}], '.json'),
            ([{'id': 4, 'text': 'Words', 'source_url': 'https://elsewhere.invalid/'}], '.json'),
            ('{"1":"first","1":"second"}', '.json'),
            ('{"id":1,"text":"valid"}\nINVALID_PRIVATE\n', '.jsonl'),
            ('id,text,text\n1,a,b\n', '.csv'),
            (b'\xff', '.jsonl'),
            ({'1': 'Words'}, '.unknown'),
        ]
        for value, suffix in cases:
            with self.subTest(suffix=suffix, value_type=type(value).__name__):
                with self.assertRaises(ValueError) as error: self.store.import_file(self.file(value, suffix))
                self.assertNotIn('INVALID_PRIVATE', str(error.exception))
                self.assertEqual(self.store.count(), 0)

    def test_file_and_line_bounds_and_empty_archives(self):
        source = self.file({'1': 'A story.'})
        with patch.object(database, 'MAX_IMPORT_BYTES', 10):
            with self.assertRaises(ValueError): self.store.import_file(source)
        source = self.file('{"id":1,"text":"' + 'x' * 100 + '"}\n', '.jsonl')
        with patch.object(database, 'MAX_IMPORT_LINE_BYTES', 40):
            with self.assertRaises(ValueError): self.store.import_file(source)
        for value in ({}, [], {'entries': []}):
            self.assertEqual(self.store.import_file(self.file(value)), {'added': 0, 'duplicates': 0, 'total': 0})

    def test_search_normalization_unicode_followups_and_literal_fts_syntax(self):
        self.add(1, 'I told my mum that I was coming out. We met at a café.', title='Family')
        self.add(2, 'A first kiss beside the library.', title='Romance')
        snap = self.store.open_snapshot(); self.addCleanup(snap.close)
        matches = snap.search('Tell me about coming out to your mother.')
        self.assertEqual(matches[0]['source_entry_id'], '1')
        self.assertIn('café', snap.search('cafe')[0]['excerpt'])
        followup = snap.search('Tell me more about that.', [{'role': 'user', 'content': 'Tell me about my mother.'}])
        self.assertEqual(followup[0]['source_entry_id'], '1')
        self.assertEqual(snap.search('astronomy', [{'role': 'user', 'content': 'mother'}]), [])
        self.assertEqual(snap.search('Tell me a story'), [])
        for query in ['" OR *', 'NEAR(foo)', 'title_terms:private', 'DROP TABLE entries; --']:
            with self.subTest(query=query): snap.search(query)
        self.assertEqual(self.store.count(), 2)

    def test_results_are_exact_bounded_source_excerpts_with_existing_grounding_shape(self):
        for i in range(5): self.add(i + 1, 'prefix ' * 600 + 'unicorn ' * 400, title='Synthetic quote')
        snap = self.store.open_snapshot(); self.addCleanup(snap.close)
        matches = snap.search('unicorn')
        self.assertEqual(len(matches), 3)
        self.assertLessEqual(sum(len(m['excerpt']) for m in matches), 6000)
        for match in matches:
            self.assertLessEqual(len(match['excerpt']), 2000)
            original = self.store.snapshot([match['id']])[0].text
            self.assertIn(match['excerpt'], original)
            self.assertIn('unicorn', match['excerpt'])
        messages = grounding_messages(matches)
        self.assertEqual(len(json.loads(messages[1]['content'])['references']), 3)

    def test_snapshot_is_frozen_across_import_and_removal_and_fresh_snapshot_sees_changes(self):
        first = self.add(1, 'Old library words.')
        snap = self.store.open_snapshot(); self.addCleanup(snap.close)
        self.assertEqual(snap.count, 1)
        self.assertEqual(snap._connection.execute('PRAGMA database_list').fetchone()[2], '')
        self.assertEqual(snap._connection.execute('PRAGMA query_only').fetchone()[0], 1)
        self.store.import_file(self.file({'2': 'New astronomy words.'}))
        self.store.remove(first['id'])
        self.assertEqual(snap.count, 1)
        self.assertEqual(snap.search('astronomy'), [])
        self.assertEqual(snap.search('library')[0]['id'], first['id'])
        fresh = self.store.open_snapshot(); self.addCleanup(fresh.close)
        self.assertEqual(fresh.count, 1)
        self.assertEqual(fresh.search('library'), [])
        self.assertEqual(fresh.search('astronomy')[0]['source_entry_id'], '2')
        snap.close(); snap.close()
        with self.assertRaisesRegex(ValueError, 'closed'): snap.search('library')

    def test_snapshot_is_safe_for_worker_threads(self):
        self.add(1, 'A library memory.')
        snap = self.store.open_snapshot(); self.addCleanup(snap.close)
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(snap.search, ['library', 'astronomy', 'library']))
        self.assertEqual([len(result) for result in results], [1, 0, 1])

    def test_page_bounds_and_unknown_selected_id_are_rejected(self):
        for kwargs in ({'offset': -1}, {'offset': True}, {'offset': 2**100}, {'limit': 0}, {'limit': 101}, {'limit': True}, {'query': None}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError): self.store.page(**kwargs)
        with self.assertRaises(KeyError): self.store.snapshot(['0' * 32])

    def test_long_query_has_a_bounded_quoted_fts_expression(self):
        query = ' '.join('topic' + str(index) for index in range(1000))
        terms, expression = database._fts_query(query)
        self.assertLessEqual(len(terms), database.MAX_QUERY_TERMS)
        self.assertLessEqual(expression.count(' OR '), database.MAX_QUERY_TERMS - 1)
        self.assertEqual(self.store.page(query=query)['matched'], 0)


class MigrationTests(unittest.TestCase):
    def test_legacy_migration_preserves_ids_provenance_original_file_and_only_runs_once(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            legacy = ArchiveStore(root)
            old = legacy.add(text='A previous library story.', source_url='https://www.queeringthemap.com/moment/8', provenance='fetched')
            before = (root / MANIFEST_NAME).read_bytes()
            store = database.ArchiveDatabase(root)
            self.assertEqual(store.list(), [old])
            self.assertEqual((root / MANIFEST_NAME).read_bytes(), before)
            store.close()
            legacy.add(text='A late edit in the old file.', source_url='https://www.queeringthemap.com/moment/9')
            reopened = database.ArchiveDatabase(root)
            try: self.assertEqual(reopened.list(), [old])
            finally: reopened.close()

    def test_invalid_legacy_file_and_unsupported_sqlite_features_have_clear_failures(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); legacy = root / MANIFEST_NAME
            legacy.write_text('PRIVATE_INVALID_JSON')
            with self.assertRaises(ValueError) as error: database.ArchiveDatabase(root)
            self.assertNotIn('PRIVATE_INVALID_JSON', str(error.exception))
            self.assertEqual(legacy.read_text(), 'PRIVATE_INVALID_JSON')
        class Unsupported:
            row_factory = None
            def execute(self, query): return self
            def fetchone(self): return ('delete',)
            def executescript(self, query): raise sqlite3.OperationalError('no such module: fts5')
            def close(self): pass
        with tempfile.TemporaryDirectory() as folder, patch.object(database.sqlite3, 'connect', return_value=Unsupported()):
            with self.assertRaisesRegex(ValueError, 'FTS5 and working filesystem locking'): database.ArchiveDatabase(folder)


if __name__ == '__main__':
    unittest.main()
