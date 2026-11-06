"""Current museum response contracts, without network calls or image downloads."""
import copy
import unittest
from unittest.mock import patch

from live_voice import museum_cleveland as cleveland, museum_met as met
from live_voice.museum_common import MuseumImportError


def met_object(identity=1, **changes):
    value = {
        'objectID': identity, 'title': 'A <em>garden</em>',
        'artistDisplayName': 'Example Artist', 'objectDate': '1901',
        'medium': 'Oil on canvas', 'dimensions': '30 × 20 cm',
        'creditLine': 'Example gift', 'accessionNumber': '1901.1',
        'classification': 'Paintings', 'isPublicDomain': True,
        'primaryImageSmall': 'https://images.metmuseum.org/CRDImages/ep/web-large/example.jpg',
        'primaryImage': 'https://images.metmuseum.org/CRDImages/ep/original/example.jpg',
        'culture': 'Example culture', 'artistPrefix': 'Attributed to',
        'tags': [{'term': 'Flowers'}],
        'objectURL': 'https://attacker.invalid/must-never-be-followed',
    }
    value.update(changes)
    return value


def cleveland_object(identity=1, **changes):
    value = {
        'id': identity, 'title': 'A <em>garden</em>',
        'url': 'https://clevelandart.org/art/1901.' + str(identity),
        'share_license_status': 'CC0',
        'images': {'web': {'url': 'https://openaccess-cdn.clevelandart.org/1901.1/example_web.jpg'},
                   'full': {'url': 'https://openaccess-cdn.clevelandart.org/1901.1/example_full.tif'}},
        'creators': [{'description': 'Example Artist (1900–1980)', 'role': 'artist', 'qualifier': 'attributed to'}],
        'creation_date': '1901', 'technique': 'Oil on canvas', 'measurements': '30 × 20 cm',
        'creditline': 'Example gift', 'accession_number': '1901.1', 'type': 'Painting',
        'description': '<p>A garden at <b>dawn</b>.</p><script>not narrative context</script>',
        'did_you_know': 'Painted outdoors.', 'culture': ['Example culture'],
    }
    value.update(changes)
    return value


class MuseumInputTests(unittest.TestCase):
    def test_invalid_search_is_rejected_before_transport(self):
        def never(*args, **kwargs):
            self.fail('Invalid input reached transport')
        for module in (met, cleveland):
            for query, limit in (('', 1), (' ' * 3, 1), ('x' * 201, 1), ('a\nb', 1),
                                 ('garden', 0), ('garden', 1001), ('garden', True), ('garden', 1.5)):
                with self.subTest(provider=module.PROVIDER, query=repr(query), limit=limit):
                    with self.assertRaises(MuseumImportError):
                        list(module.iter_records(never, query, limit))


class MetAdapterTests(unittest.TestCase):
    def test_current_search_pagination_detail_ids_and_limit(self):
        calls = []
        def fetch(url, params=None, headers=None):
            calls.append((url, copy.deepcopy(params)))
            if url == met.SEARCH_URL:
                offset = params['offset']
                return {'total': 50000, 'objectIDs': list(range(offset, offset + params['limit']))}
            return met_object(int(url.rsplit('/', 1)[1]))
        with patch.object(met, 'PAGE_SIZE', 2):
            rows = list(met.iter_records(fetch, '  flowers  ', 3))
        self.assertEqual([row['source_entry_id'] for row in rows], ['0', '1', '2'])
        self.assertEqual([params for url, params in calls if url == met.SEARCH_URL], [
            {'q': 'flowers', 'offset': 0, 'limit': 2}, {'q': 'flowers', 'offset': 2, 'limit': 1}])
        self.assertIn('/v1.1/search', calls[0][0])
        self.assertEqual(len(calls), 5)
        self.assertTrue(all(url.startswith('https://collectionapi.metmuseum.org/') for url, _ in calls))
        self.assertEqual(rows[0]['source_url'], 'https://www.metmuseum.org/art/collection/search/0')
        self.assertNotIn('collection_id', rows[0])

    def test_record_fields_and_image_rights_stay_separate(self):
        def fetch(url, params=None, headers=None):
            if url == met.SEARCH_URL:
                return {'total': 2, 'objectIDs': [1, 2]}
            return met_object(int(url.rsplit('/', 1)[1]), isPublicDomain=url.endswith('/1'))
        first, second = list(met.iter_records(fetch, 'garden', 2))
        self.assertEqual(first['title'], 'A garden')
        self.assertIn('Attributed to', first['text'])
        self.assertIn('Flowers', first['text'])
        self.assertEqual(first['metadata']['credit_line'], 'Example gift')
        self.assertTrue(first['image_url'].endswith('/web-large/example.jpg'))
        self.assertIn('public domain', first['metadata']['rights'])
        self.assertIsNone(second['image_url'])
        self.assertIn('metadata: CC0', second['metadata']['rights'])
        self.assertIn('No public-domain image selected', second['metadata']['rights'])

    def test_strict_boolean_rights_and_official_image_host(self):
        for public, image in ((1, met_object()['primaryImageSmall']),
                              ('true', met_object()['primaryImageSmall']),
                              (True, 'https://images.metmuseum.org.evil.invalid/CRDImages/x.jpg'),
                              (True, 'https://user@images.metmuseum.org/CRDImages/x.jpg')):
            def fetch(url, params=None, headers=None):
                if url == met.SEARCH_URL: return {'total': 1, 'objectIDs': [1]}
                return met_object(isPublicDomain=public, primaryImageSmall=image, primaryImage='')
            with self.subTest(public=public, image=image):
                self.assertIsNone(list(met.iter_records(fetch, 'garden', 1))[0]['image_url'])

    def test_empty_and_malformed_responses(self):
        self.assertEqual(list(met.iter_records(lambda *a, **k: {'total': 0, 'objectIDs': None}, 'empty', 1)), [])
        invalid = [{'total': True, 'objectIDs': []}, {'total': 1, 'objectIDs': []},
                   {'total': 1, 'objectIDs': [True]}, {'total': 1, 'objectIDs': [-1]},
                   {'total': 1, 'objectIDs': [1, 2]}, {'total': 1, 'objectIDs': '1'}]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(MuseumImportError):
                list(met.iter_records(lambda *a, **k: payload, 'garden', 1))

    def test_mismatched_detail_and_repeated_pages_fail_safely(self):
        for mismatch in (True, False):
            def fetch(url, params=None, headers=None):
                if url == met.SEARCH_URL: return {'total': 4, 'objectIDs': [1]}
                return met_object(999 if mismatch else 1)
            with self.subTest(mismatch=mismatch), self.assertRaises(MuseumImportError) as caught:
                list(met.iter_records(fetch, 'private query', 4))
            self.assertEqual(caught.exception.reason, 'invalid_response')
            self.assertNotIn('private query', str(caught.exception))


class ClevelandAdapterTests(unittest.TestCase):
    def test_skip_limit_pages_and_current_field_contract(self):
        calls = []
        def fetch(url, params=None, headers=None):
            calls.append((url, copy.deepcopy(params)))
            return {'info': {'total': 10000}, 'data': [
                cleveland_object(index) for index in range(params['skip'], params['skip'] + params['limit'])]}
        with patch.object(cleveland, 'PAGE_SIZE', 2):
            rows = list(cleveland.iter_records(fetch, 'monet', 3))
        self.assertEqual([row['source_entry_id'] for row in rows], ['0', '1', '2'])
        self.assertEqual([(p['skip'], p['limit']) for _, p in calls], [(0, 2), (2, 1)])
        self.assertEqual({url for url, _ in calls}, {cleveland.SEARCH_URL})
        self.assertIn('share_license_status', calls[0][1]['fields'].split(','))
        self.assertNotIn('license', calls[0][1]['fields'].split(','))
        self.assertNotIn('cc0', calls[0][1])  # Keep metadata even without image rights.

    def test_full_metadata_and_cc0_image_normalization(self):
        def fetch(*args, **kwargs):
            return {'info': {'total': 1}, 'data': [cleveland_object()]}
        row = list(cleveland.iter_records(fetch, 'garden', 1))[0]
        self.assertEqual(row['title'], 'A garden')
        self.assertEqual(row['source_url'], 'https://clevelandart.org/art/1901.1')
        self.assertTrue(row['image_url'].endswith('_web.jpg'))
        self.assertEqual(row['metadata']['medium'], 'Oil on canvas')
        self.assertIn('Example Artist', row['metadata']['artist'])
        self.assertIn('attributed to', row['metadata']['artist'])
        self.assertIn('A garden at dawn.', row['text'])
        self.assertIn('Painted outdoors.', row['text'])
        self.assertNotIn('not narrative context', row['text'])
        self.assertNotIn('collection_id', row)

    def test_non_cc0_and_untrusted_image_never_become_thumbnails(self):
        cases = [cleveland_object(share_license_status='Copyrighted'),
                 cleveland_object(share_license_status='Open Access'),
                 cleveland_object(share_license_status=None, license='CC0'),
                 cleveland_object(images={'web': {'url': 'https://evil.invalid/x.jpg'}}),
                 cleveland_object(images={'web': None}), cleveland_object(images=None)]
        for record in cases:
            with self.subTest(record=record):
                rows = list(cleveland.iter_records(lambda *a, **k: {'info': {'total': 1}, 'data': [record]}, 'garden', 1))
                self.assertEqual(len(rows), 1)
                self.assertIsNone(rows[0]['image_url'])
                self.assertIn('metadata: CC0', rows[0]['metadata']['rights'])

    def test_empty_malformed_and_invalid_sources(self):
        self.assertEqual(list(cleveland.iter_records(lambda *a, **k: {'info': {'total': 0}, 'data': []}, 'empty', 1)), [])
        cases = [{'info': {'total': True}, 'data': []}, {'info': {'total': 1}, 'data': []},
                 {'info': {'total': 1}, 'data': [cleveland_object(id=True)]},
                 {'info': {'total': 1}, 'data': [cleveland_object(url='https://clevelandart.org.evil.invalid/art/1')]},
                 {'info': {'total': 1}, 'data': [cleveland_object(url='https://u@clevelandart.org/art/1')]},
                 {'info': {'total': 1}, 'data': 'private response'}]
        for response in cases:
            with self.subTest(response=response), self.assertRaises(MuseumImportError):
                list(cleveland.iter_records(lambda *a, **k: response, 'garden', 1))

    def test_duplicate_response_cannot_loop_or_duplicate_records(self):
        calls = []
        def fetch(url, params=None, headers=None):
            calls.append(params['skip'])
            return {'info': {'total': 100000}, 'data': [cleveland_object(1)]}
        with self.assertRaises(MuseumImportError):
            list(cleveland.iter_records(fetch, 'garden', 1000))
        self.assertEqual(calls, [0, 1])


if __name__ == '__main__':
    unittest.main()
