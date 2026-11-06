"""Full-source pagination/resume invariants, using only synthetic metadata."""
import copy
import re
import unittest
from unittest.mock import patch

from live_voice.museum_bulk_linked import iter_pages
from live_voice.museum_common import MuseumImportError
from live_voice import museum_getty as getty, museum_rijksmuseum as rijks


def rijks_page(ids, total, token=None):
    value = {'type': 'OrderedCollectionPage', 'partOf': {'totalItems': total},
             'orderedItems': [{'id': 'https://id.rijksmuseum.nl/' + value, 'type': 'HumanMadeObject'} for value in ids]}
    if token:
        value['next'] = {'id': rijks.SEARCH_URL + '?pageToken=' + token, 'type': 'OrderedCollectionPage'}
    return value


def rijks_object(identity, *, image=False):
    return {'id': 'https://id.rijksmuseum.nl/' + identity, 'type': 'HumanMadeObject',
            'identified_by': [{'type': 'Name', 'content': 'Fixture artwork ' + identity}],
            'produced_by': {'carried_out_by': [{'_label': 'Fixture artist'}]},
            'shows': [{'id': 'https://id.rijksmuseum.nl/9001'}] if image else []}


IDS = ['00000000-0000-0000-0000-00000000000' + str(number) for number in (1, 2, 3)]
MEDIA_ID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'


def getty_object(identity, *, image=False):
    return {'id': 'https://data.getty.edu/museum/collection/object/' + identity, 'type': 'HumanMadeObject',
            'identified_by': [{'type': 'Name', 'content': 'Fixture artwork'}],
            'produced_by': {'carried_out_by': [{'_label': 'Fixture artist'}]},
            'shows': [{'id': 'https://data.getty.edu/media/image/' + MEDIA_ID,
                       'classified_as': [{'id': 'http://vocab.getty.edu/aat/300215302'}]}] if image else []}


def count_result(total):
    return {'results': {'bindings': [{'total': {'type': 'literal', 'value': str(total)}}]}}


def id_result(ids):
    return {'results': {'bindings': [{'object': {'type': 'uri', 'value':
            'https://data.getty.edu/museum/collection/object/' + identity},
            'title': {'type': 'literal', 'value': 'Fixture artwork'},
            'artist': {'type': 'literal', 'value': 'Fixture artist'},
            'date': {'type': 'literal', 'value': '1901'},
            'medium': {'type': 'literal', 'value': 'Oil on canvas'},
            'accession': {'type': 'literal', 'value': '1901.1'},
            'classification': {'type': 'literal', 'value': 'Paintings'}} for identity in ids]}}


class RijksFullPages(unittest.TestCase):
    def fetcher(self, pages, *, image=False, restricted=False):
        calls = []
        def fetch(url, params=None, headers=None):
            calls.append((url, copy.deepcopy(params)))
            if url == rijks.SEARCH_URL:
                return copy.deepcopy(pages.get((params or {}).get('pageToken')))
            identity = url.rsplit('/', 1)[1]
            if identity == '9001':
                return {'id': 'https://id.rijksmuseum.nl/9001', 'type': 'VisualItem',
                        'subject_to': [{'classified_as': [{'id': 'https://creativecommons.org/licenses/by/4.0/'
                            if restricted else 'https://creativecommons.org/publicdomain/mark/1.0/'}]}],
                        'digitally_shown_by': [{'id': 'https://id.rijksmuseum.nl/9002'}]}
            if identity == '9002':
                return {'id': 'https://id.rijksmuseum.nl/9002', 'type': 'DigitalObject',
                        'access_point': [{'id': 'https://iiif.micr.io/example-image/full/max/0/default.jpg'}]}
            self.assertEqual(params, {'_profile': 'la-framed'})
            return rijks_object(identity, image=image)
        return fetch, calls

    def test_all_pages_are_normalized_and_resume_starts_at_the_committed_token(self):
        pages = {None: rijks_page(['1001', '1002'], 3, 'page_two'), 'page_two': rijks_page(['1003'], 3)}
        fetch, calls = self.fetcher(pages)
        iterator = iter_pages('rijksmuseum', fetch)
        first, cursor, expected = next(iterator)
        self.assertEqual([row['source_entry_id'] for row in first], ['1001', '1002'])
        self.assertEqual(expected, 3)
        self.assertEqual(cursor, {'v': 1, 'provider': 'rijksmuseum', 'page_token': 'page_two',
                                 'last_id': '1002', 'processed': 2, 'expected': 3})
        self.assertNotIn('collection_id', first[0])
        iterator.close()
        original = copy.deepcopy(cursor)
        fetch, calls = self.fetcher(pages)
        resumed = list(iter_pages('rijksmuseum', fetch, cursor))
        self.assertEqual(cursor, original)
        self.assertEqual(len(resumed), 1)
        self.assertEqual(resumed[0][0][0]['source_entry_id'], '1003')
        self.assertIsNone(resumed[0][1])
        self.assertEqual(calls[0], (rijks.SEARCH_URL, {'pageToken': 'page_two'}))
        self.assertNotIn('https://data.rijksmuseum.nl/1001', [url for url, _ in calls])

    def test_full_import_preserves_explicit_image_rights_without_fetching_pixels(self):
        for restricted in (False, True):
            fetch, calls = self.fetcher({None: rijks_page(['1001'], 1)}, image=True, restricted=restricted)
            records, cursor, total = next(iter_pages('rijksmuseum', fetch))
            self.assertIsNone(cursor)
            self.assertEqual(total, 1)
            self.assertEqual(bool(records[0]['image_url']), not restricted)
            self.assertTrue(all(url.startswith('https://data.rijksmuseum.nl/') for url, _ in calls))
            if not restricted:
                self.assertIn('publicdomain/mark', records[0]['metadata']['rights'])

    def test_missing_next_or_empty_page_before_expected_never_reports_complete(self):
        for response in (rijks_page([], 2), rijks_page(['1001'], 2), rijks_page([], 2, 'another')):
            fetch, _ = self.fetcher({None: response})
            with self.subTest(response=response), self.assertRaises(MuseumImportError):
                next(iter_pages('rijksmuseum', fetch))
        fetch, _ = self.fetcher({None: rijks_page([], 0)})
        self.assertEqual(list(iter_pages('rijksmuseum', fetch)), [([], None, 0)])

    def test_repeated_tokens_repeated_ids_and_changed_counts_fail_before_completion(self):
        bad = [
            {'page_two': rijks_page(['1003'], 3, 'page_two')},
            {'page_two': rijks_page(['1002'], 3)},
            {'page_two': rijks_page(['1003'], 4)},
        ]
        for changes in bad:
            fetch, _ = self.fetcher({None: rijks_page(['1001', '1002'], 3, 'page_two'), **changes})
            iterator = iter_pages('rijksmuseum', fetch)
            self.assertIsNotNone(next(iterator)[1])
            with self.subTest(changes=changes), self.assertRaises(MuseumImportError):
                next(iterator)

    def test_untrusted_next_urls_never_become_request_targets(self):
        urls = ['https://evil.invalid/search/collection?pageToken=x',
                rijks.SEARCH_URL + '?creator=someone&pageToken=x',
                rijks.SEARCH_URL + '?pageToken=x&pageToken=y',
                'https://user@data.rijksmuseum.nl/search/collection?pageToken=x']
        for url in urls:
            response = rijks_page(['1001'], 2)
            response['next'] = {'id': url}
            fetch, calls = self.fetcher({None: response})
            with self.subTest(url=url), self.assertRaises(MuseumImportError):
                next(iter_pages('rijksmuseum', fetch))
            self.assertEqual(len(calls), 1)

    def test_a_failed_record_cannot_yield_a_partially_normalized_page(self):
        fetch, _ = self.fetcher({None: rijks_page(['1001', '1002'], 2)})
        def failed(url, **kwargs):
            if url.endswith('/1002'):
                raise RuntimeError('operator stop between object requests')
            return fetch(url, **kwargs)
        with self.assertRaisesRegex(RuntimeError, 'operator stop'):
            next(iter_pages('rijksmuseum', failed))


class GettyFullPages(unittest.TestCase):
    def fetcher(self, ids=IDS, *, count=None, final_count=None, image=False):
        calls, counts = [], [0]
        def fetch(url, params=None, headers=None):
            calls.append((url, copy.deepcopy(params), copy.deepcopy(headers)))
            if url == getty.SEARCH_URL:
                query = params['query']
                self.assertNotIn('OFFSET', query)
                self.assertNotIn('CONTAINS', query)
                self.assertEqual(headers, {'Accept': 'application/sparql-results+json'})
                if 'COUNT' in query:
                    counts[0] += 1
                    return count_result(final_count if counts[0] > 1 and final_count is not None else len(ids) if count is None else count)
                after = re.search(r'FILTER\(STR\(\?object\) > "https://data.getty.edu/museum/collection/object/([^\"]+)"\)', query)
                chosen = [identity for identity in ids if not after or identity > after[1]][:2]
                return id_result(chosen)
            if '/media/image/' in url:
                return {'id': url, 'type': 'VisualItem',
                        'subject_to': [{'classified_as': [{'id': 'https://creativecommons.org/publicdomain/zero/1.0/'}]}],
                        'digitally_shown_by': [{'access_point': [{'id': 'https://media.getty.edu/iiif/image/' + MEDIA_ID}]}]}
            return getty_object(url.rsplit('/', 1)[1], image=image)
        return fetch, calls

    def test_keyset_continues_past_a_short_page_and_confirms_an_empty_terminal_page(self):
        fetch, calls = self.fetcher()
        with patch('live_voice.museum_bulk_linked.GETTY_PAGE_SIZE', 2):
            pages = list(iter_pages('getty', fetch))
        self.assertEqual([len(records) for records, _, _ in pages], [2, 1, 0])
        self.assertEqual([total for _, _, total in pages], [3, 3, 3])
        self.assertTrue(all(cursor is not None for _, cursor, _ in pages[:-1]))
        self.assertIsNone(pages[-1][1])
        self.assertEqual([row['source_entry_id'] for records, _, _ in pages for row in records], IDS)
        self.assertEqual(sum('COUNT' in (params or {}).get('query', '') for _, params, _ in calls), 2)

    def test_resume_checks_count_then_uses_the_last_committed_uuid(self):
        fetch, _ = self.fetcher()
        with patch('live_voice.museum_bulk_linked.GETTY_PAGE_SIZE', 2):
            iterator = iter_pages('getty', fetch)
            _, cursor, _ = next(iterator)
            iterator.close()
            original = copy.deepcopy(cursor)
            fetch, calls = self.fetcher()
            pages = list(iter_pages('getty', fetch, cursor))
        self.assertEqual(cursor, original)
        self.assertEqual([row['source_entry_id'] for records, _, _ in pages for row in records], [IDS[2]])
        self.assertIn(IDS[1], calls[1][1]['query'])
        self.assertNotIn('https://data.getty.edu/museum/collection/object/' + IDS[0], [url for url, _, _ in calls])

    def test_metadata_is_batched_without_individual_object_or_media_requests(self):
        fetch, calls = self.fetcher(IDS[:1], image=True)
        records, cursor, total = next(iter_pages('getty', fetch))
        self.assertEqual(total, 1)
        self.assertIsNotNone(cursor)
        self.assertIn('Getty CC0', records[0]['metadata']['rights'])
        self.assertIn('checked only when a record is retrieved', records[0]['metadata']['rights'])
        self.assertIsNone(records[0]['image_url'])
        self.assertEqual(records[0]['metadata']['artist'], 'Fixture artist')
        self.assertEqual(records[0]['metadata']['date'], '1901')
        self.assertEqual(records[0]['metadata']['medium'], 'Oil on canvas')
        self.assertEqual(records[0]['metadata']['accession_number'], '1901.1')
        self.assertEqual({url for url, _, _ in calls}, {getty.SEARCH_URL})
        self.assertEqual(len(calls), 2)

    def test_incomplete_enumeration_and_final_count_changes_do_not_report_complete(self):
        for count, final_count in ((4, None), (3, 4)):
            fetch, _ = self.fetcher(count=count, final_count=final_count)
            with patch('live_voice.museum_bulk_linked.GETTY_PAGE_SIZE', 2):
                iterator = iter_pages('getty', fetch)
                self.assertIsNotNone(next(iterator)[1])
                self.assertIsNotNone(next(iterator)[1])
                with self.subTest(count=count, final=final_count), self.assertRaises(MuseumImportError):
                    next(iterator)

    def test_repeated_or_untrusted_discovery_ids_fail_before_object_fetch(self):
        for ids in ([IDS[1], IDS[0]], [IDS[0], IDS[0]]):
            fetch, calls = self.fetcher(ids=ids)
            with self.subTest(ids=ids), self.assertRaises(MuseumImportError):
                next(iter_pages('getty', fetch))
            self.assertEqual(len(calls), 2)
        def unsafe(url, params=None, headers=None):
            if 'COUNT' in params['query']: return count_result(1)
            return {'results': {'bindings': [{'object': {'type': 'uri', 'value': 'https://evil.invalid/object/' + IDS[0]}}]}}
        with self.assertRaises(MuseumImportError):
            next(iter_pages('getty', unsafe))


class BulkCursorValidation(unittest.TestCase):
    def test_invalid_checkpoint_is_rejected_before_any_request(self):
        def never(*args, **kwargs): self.fail('Invalid cursor reached the network')
        cases = [
            ('rijksmuseum', {}), ('getty', 'url'),
            ('getty', {'v': 1, 'provider': 'getty', 'after': IDS[0], 'processed': True, 'expected': 1}),
            ('getty', {'v': 1, 'provider': 'getty', 'after': IDS[0] + '" }', 'processed': 1, 'expected': 2}),
            ('rijksmuseum', {'v': 1, 'provider': 'rijksmuseum', 'page_token': 'https://evil.invalid/',
                            'last_id': '1001', 'processed': 1, 'expected': 2}),
        ]
        for provider, cursor in cases:
            with self.subTest(provider=provider, cursor=cursor), self.assertRaises(MuseumImportError):
                next(iter_pages(provider, never, cursor))

    def test_unsupported_source_has_no_requests(self):
        with self.assertRaises(MuseumImportError):
            next(iter_pages('met', lambda *a, **k: self.fail('Wrong source reached HTTP')))


def edm_entry(identity='1001', *, deleted=False, rights='http://creativecommons.org/publicdomain/mark/1.0/',
              image='https://iiif.micr.io/example-image/full/max/0/default.jpg', conflicting=False):
    if deleted:
        return '<record><header status="deleted"><identifier>https://id.rijksmuseum.nl/' + identity + '</identifier></header></record>'
    return f'''<record><header><identifier>https://id.rijksmuseum.nl/{identity}</identifier></header><metadata>
      <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
        xmlns:edm="http://www.europeana.eu/schemas/edm/" xmlns:ore="http://www.openarchives.org/ore/terms/"
        xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dct="http://purl.org/dc/terms/"
        xmlns:skos="http://www.w3.org/2004/02/skos/core#">
        <ore:Aggregation rdf:about="https://id.rijksmuseum.nl/{identity}#aggregation">
          <edm:aggregatedCHO rdf:resource="https://id.rijksmuseum.nl/{identity}"/>
          <edm:isShownBy rdf:resource="{image}"/><edm:rights rdf:resource="{rights}"/>
        </ore:Aggregation>
        <edm:ProvidedCHO rdf:about="https://id.rijksmuseum.nl/{identity}">
          <dc:title xml:lang="nl">Testtitel</dc:title><dc:title xml:lang="en">Fixture landscape</dc:title>
          <dc:creator rdf:resource="https://id.rijksmuseum.nl/2001"/>
          <dc:type rdf:resource="https://id.rijksmuseum.nl/3001"/>
          <dc:subject rdf:resource="https://id.rijksmuseum.nl/3002"/>
          <dct:medium rdf:resource="https://id.rijksmuseum.nl/3003"/>
          <dc:identifier>SK-A-1</dc:identifier><dct:created xml:lang="en">1901</dct:created>
          <dct:extent xml:lang="en">20 × 30 cm</dct:extent>
        </edm:ProvidedCHO>
        <edm:Agent rdf:about="https://id.rijksmuseum.nl/2001"><skos:prefLabel xml:lang="en">Fixture artist</skos:prefLabel></edm:Agent>
        <skos:Concept rdf:about="https://id.rijksmuseum.nl/3001"><skos:prefLabel xml:lang="en">Painting</skos:prefLabel></skos:Concept>
        <skos:Concept rdf:about="https://id.rijksmuseum.nl/3002"><skos:prefLabel xml:lang="en">Canals</skos:prefLabel></skos:Concept>
        <skos:Concept rdf:about="https://id.rijksmuseum.nl/3003"><skos:prefLabel xml:lang="en">Oil paint</skos:prefLabel></skos:Concept>
        <edm:WebResource rdf:about="{image}">{'<dc:rights rdf:resource="http://rightsstatements.org/vocab/InC/1.0/"/>' if conflicting else ''}</edm:WebResource>
      </rdf:RDF></metadata></record>'''


def oai_page(entries, total, token=''):
    return ('''<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'''
            '<ListRecords>' + ''.join(entries) + f'<resumptionToken completeListSize="{total}">{token}</resumptionToken>'
            '</ListRecords></OAI-PMH>').encode()


class RijksOAIHarvest(unittest.TestCase):
    def fetcher(self, pages):
        owner = self
        class Transport:
            def __init__(self): self.calls = []
            def __call__(self, *a, **k): owner.fail('OAI harvest must not make individual JSON requests')
            def xml(self, url, params=None, headers=None):
                self.calls.append((url, copy.deepcopy(params)))
                return pages.get(params.get('resumptionToken'))
        return Transport()

    def test_fifty_record_protocol_batches_include_metadata_and_eligible_images_without_n_plus_one(self):
        transport = self.fetcher({None: oai_page([edm_entry()], 1)})
        rows, cursor, total = next(iter_pages('rijksmuseum', transport))
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0][0], 'https://data.rijksmuseum.nl/oai')
        self.assertEqual(transport.calls[0][1]['metadataPrefix'], 'edm')
        self.assertRegex(transport.calls[0][1]['until'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        self.assertIsNone(cursor)
        self.assertEqual(total, 1)
        row = rows[0]
        self.assertEqual(row['title'], 'Fixture landscape')
        self.assertEqual(row['metadata']['artist'], 'Fixture artist')
        self.assertEqual(row['metadata']['medium'], 'Oil paint')
        self.assertIn('Canals', row['text'])
        self.assertEqual(row['metadata']['dimensions'], '20 × 30 cm')
        self.assertIn('/full/!600,600/', row['image_url'])

    def test_nonexpiring_cursor_preserves_until_and_counts_deleted_headers_separately(self):
        pages = {None: oai_page([edm_entry(), edm_entry('1002', deleted=True)], 3, 'token/plus+=='),
                 'token/plus+==': oai_page([edm_entry('1003')], 3)}
        transport = self.fetcher(pages)
        iterator = iter_pages('rijksmuseum', transport)
        first, cursor, expected = next(iterator)
        self.assertEqual(len(first), 1)
        self.assertIsNone(expected)
        self.assertEqual((cursor['scanned'], cursor['deleted'], cursor['processed'], cursor['expected']), (2, 1, 1, 3))
        self.assertEqual(cursor['mode'], 'oai-edm')
        iterator.close()
        transport = self.fetcher(pages)
        original = copy.deepcopy(cursor)
        final, terminal, active_total = next(iter_pages('rijksmuseum', transport, cursor))
        self.assertEqual(cursor, original)
        self.assertEqual(transport.calls[0][1], {'verb': 'ListRecords', 'resumptionToken': 'token/plus+=='})
        self.assertEqual(final[0]['source_entry_id'], '1003')
        self.assertIsNone(terminal)
        self.assertEqual(active_total, 2)

    def test_restricted_conflicting_and_untrusted_images_keep_the_artwork_metadata(self):
        cases = [{'rights': 'http://rightsstatements.org/vocab/InC/1.0/'}, {'conflicting': True},
                 {'image': 'https://evil.invalid/pixel.jpg'}]
        for changes in cases:
            transport = self.fetcher({None: oai_page([edm_entry(**changes)], 1)})
            with self.subTest(changes=changes):
                rows, _, total = next(iter_pages('rijksmuseum', transport))
                self.assertEqual(total, 1)
                self.assertEqual(rows[0]['title'], 'Fixture landscape')
                self.assertIsNone(rows[0]['image_url'])
                self.assertEqual(len(transport.calls), 1)

    def test_empty_short_changed_or_cyclic_harvest_never_claims_completion(self):
        for first in (oai_page([], 2), oai_page([edm_entry()], 2)):
            with self.subTest(first=first[:60]), self.assertRaises(MuseumImportError):
                next(iter_pages('rijksmuseum', self.fetcher({None: first})))
        for second in (oai_page([edm_entry('1002')], 3), oai_page([edm_entry('1002')], 2, 'again')):
            iterator = iter_pages('rijksmuseum', self.fetcher({None: oai_page([edm_entry()], 2, 'again'), 'again': second}))
            self.assertIsNotNone(next(iterator)[1])
            with self.assertRaises(MuseumImportError): next(iterator)

    def test_bad_xml_external_entities_and_wrong_record_identity_fail_safely(self):
        bad = [b'<broken', b'<!DOCTYPE x [<!ENTITY y "private">]><OAI-PMH/>',
               ('<x>' * 65 + '</x>' * 65).encode(),
               '<!DOCTYPE x><OAI-PMH/>'.encode('utf-16'),
               oai_page([edm_entry().replace('edm:ProvidedCHO rdf:about="https://id.rijksmuseum.nl/1001"',
                                          'edm:ProvidedCHO rdf:about="https://id.rijksmuseum.nl/9999"')], 1)]
        for body in bad:
            with self.subTest(body=body[:60]), self.assertRaises(MuseumImportError):
                next(iter_pages('rijksmuseum', self.fetcher({None: body})))


if __name__ == '__main__':
    unittest.main()
