"""Independent museum import/thumbnail boundary regressions; no live requests."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from live_voice.collections import CollectionDatabase
from live_voice.museum_aic import ENDPOINT, iter_records
from live_voice.museum_common import MuseumImportError, museum_record
from live_voice.museum_images import MuseumImages, MuseumImageUnavailable
from live_voice.museum_import import MuseumHTTP, MuseumImporter, _bounded_structure


IMAGE_ID = '12345678-1234-5678-abcd-123456789abc'
IMAGE_URL = 'https://www.artic.edu/iiif/2/' + IMAGE_ID + '/full/400,/0/default.jpg'


def aic_result(*, eligible=True):
    return {
        'data': [{'id': 1, 'title': 'A test artwork', 'artist_display': 'Example Artist',
                  'is_public_domain': eligible, 'image_id': IMAGE_ID,
                  'description': 'Excluded separately licensed description',
                  'thumbnail': {'alt_text': 'A test garden.'}}],
        'pagination': {'total': 1}, 'config': {'iiif_url': 'https://www.artic.edu/iiif/2'},
    }


class Clock:
    def __init__(self): self.now = 0.0
    def __call__(self): return self.now
    def sleep(self, seconds): self.now += seconds


class MuseumTransportReview(unittest.TestCase):
    def client(self, provider, handler):
        clock = Clock()
        transport = MuseumHTTP(provider, client=httpx.Client(transport=httpx.MockTransport(handler)),
                               clock=clock, sleep=clock.sleep)
        self.addCleanup(transport.client.close)
        return transport, clock

    def test_wrong_origin_paths_and_credentials_never_reach_http(self):
        calls = []
        transport, _ = self.client('aic', lambda request: calls.append(request))
        urls = [
            'https://evil.invalid/api/v1/artworks/search',
            'https://api.artic.edu.evil.invalid/api/v1/artworks/search',
            'https://user@api.artic.edu/api/v1/artworks/search',
            'https://api.artic.edu:443/api/v1/artworks/search',
            'http://api.artic.edu/api/v1/artworks/search',
            'https://api.artic.edu/admin', ENDPOINT + '#fragment',
        ]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(MuseumImportError):
                transport(url)
        with self.assertRaises(MuseumImportError):
            transport(ENDPOINT, headers={'Authorization': 'secret'})
        self.assertEqual(calls, [])

    def test_redirect_is_not_followed_and_error_hides_upstream_data(self):
        calls = []
        def respond(request):
            calls.append(str(request.url))
            return httpx.Response(302, headers={'location': 'https://private.invalid/secret'},
                                  text='private upstream response')
        transport, _ = self.client('aic', respond)
        with self.assertRaises(MuseumImportError) as caught:
            transport(ENDPOINT)
        self.assertEqual(len(calls), 1)
        self.assertNotIn('private', str(caught.exception))
        self.assertNotIn('secret', str(caught.exception))

    def test_partial_timeout_bytes_count_toward_operation_budget(self):
        requests = []
        class Broken(httpx.SyncByteStream):
            def __iter__(self):
                yield b'0123456789'
                raise httpx.ReadTimeout('private response details')
        def respond(request):
            requests.append(request)
            return httpx.Response(200, headers={'content-type': 'application/json'}, stream=Broken())
        transport, _ = self.client('aic', respond)
        with patch('live_voice.museum_import.MAX_TOTAL_BYTES', 25):
            with self.assertRaises(MuseumImportError) as caught:
                transport(ENDPOINT)
        self.assertEqual(caught.exception.reason, 'invalid_response')
        self.assertEqual(transport.bytes, 20)
        self.assertEqual(len(requests), 3)

    def test_rate_retry_bounded_and_identifies_client(self):
        calls = []
        def respond(request):
            calls.append(request)
            if len(calls) < 3:
                return httpx.Response(429, headers={'retry-after': '999'}, text='private rate error')
            return httpx.Response(200, json={'data': []})
        transport, clock = self.client('aic', respond)
        self.assertEqual(transport(ENDPOINT, params={'q': 'garden'}), {'data': []})
        self.assertEqual(len(calls), 3)
        self.assertGreaterEqual(clock.now, 10)
        self.assertLess(clock.now, 13)
        self.assertIn('CharacterStudio', calls[0].headers['AIC-User-Agent'])
        self.assertEqual(len({str(request.url) for request in calls}), 1)

    def test_bad_json_mime_shape_and_deep_nesting_become_safe_errors(self):
        cases = [
            ('text/html', b'<h1>private upstream</h1>'),
            ('application/json', b'{private upstream'),
            ('application/json', b'[]'),
            ('application/json', b'{"private":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}'),
        ]
        for mime, body in cases:
            with self.subTest(mime=mime, length=len(body)):
                transport, _ = self.client('aic', lambda request: httpx.Response(
                    200, headers={'content-type': mime}, content=body))
                with self.assertRaises(MuseumImportError) as caught:
                    transport(ENDPOINT)
                self.assertEqual(caught.exception.reason, 'invalid_response')
                self.assertNotIn('private', str(caught.exception))

    def test_decoded_structure_depth_limit_is_inclusive_and_decoder_independent(self):
        def nested(depth):
            value = 0
            for _ in range(depth - 1):
                value = [value]
            return {'data': value}
        allowed = nested(64)
        self.assertIs(_bounded_structure(allowed), allowed)
        with self.assertRaises(ValueError):
            _bounded_structure(nested(65))
        with patch('live_voice.museum_import.MAX_JSON_VALUES', 5):
            allowed = {'data': [0, 1, 2]}
            self.assertIs(_bounded_structure(allowed), allowed)
            with self.assertRaises(ValueError):
                _bounded_structure({'data': [0, 1, 2, 3]})


class MuseumRecordReview(unittest.TestCase):
    def test_aic_copyrighted_artwork_keeps_cc0_metadata_without_image(self):
        calls = []
        def fetch(url, params=None, headers=None):
            calls.append((url, params))
            return aic_result(eligible=False)
        row, = list(iter_records(fetch, 'modern artist', 1))
        self.assertIsNone(row['image_url'])
        self.assertIn('Example Artist', row['text'])
        self.assertIn('Catalogue metadata: CC0', row['metadata']['rights'])
        self.assertNotIn('Excluded separately licensed', row['text'])
        self.assertNotIn('description', calls[0][1]['fields'].split(','))
        self.assertFalse(any('is_public_domain' in key for key in calls[0][1] if key != 'fields'))

    def test_html_and_large_fields_remain_valid_bounded_catalogue_data(self):
        row = museum_record('aic', '1', '<b>Title</b>' + 'x' * 500,
                            'https://www.artic.edu/artworks/1',
                            description='<p>Visible.</p><script>hidden</script>' + 'y' * 20000,
                            metadata={key: 'z' * 5000 for key in (
                                'artist', 'date', 'medium', 'dimensions', 'credit_line',
                                'accession_number', 'classification', 'rights')})
        self.assertLessEqual(len(row['title']), 240)
        self.assertLessEqual(len(row['text']), 12000)
        self.assertLessEqual(sum(len(value) for value in row['metadata'].values()), 4000)
        self.assertIn('[excerpt]', row['title'])
        self.assertNotIn('<b>', row['title'])
        self.assertNotIn('hidden', row['text'])

    def test_failed_late_parse_does_not_save_an_earlier_record(self):
        class Transport:
            requests = 0
            closed = False
            def __call__(self, url, params=None, headers=None):
                self.requests += 1
                if '/search' in url: return {'total': 2, 'objectIDs': [1, 2]}
                return {'objectID': 1 if url.endswith('/1') else 999, 'title': 'Example', 'isPublicDomain': False}
            def close(self): self.closed = True
        transport = Transport()
        with tempfile.TemporaryDirectory() as directory:
            store = CollectionDatabase(Path(directory))
            try:
                original_collections = store.list_collections()
                importer = MuseumImporter(client_factory=lambda provider: transport)
                with self.assertRaises(MuseumImportError):
                    importer.import_collection(store, 'met', 'garden', 2)
                self.assertEqual(store.count(), 0)
                self.assertEqual(store.list_collections(), original_collections)
                self.assertTrue(transport.closed)
            finally:
                store.close()


class MuseumImageReview(unittest.IsolatedAsyncioTestCase):
    async def test_only_saved_official_image_is_relayed_and_cached(self):
        calls = []
        def respond(request):
            calls.append(request)
            return httpx.Response(200, headers={'content-type': 'image/jpeg'}, content=b'\xff\xd8\xff\xe0test')
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            images = MuseumImages(client=client)
            with tempfile.TemporaryDirectory() as directory:
                store = CollectionDatabase(directory)
                try:
                    row, = list(iter_records(lambda *a, **k: aic_result(), 'garden', 1))
                    store.import_records([row], collection_id='art-institute-of-chicago',
                        label='Art Institute of Chicago', source_kind='museum-aic', source_url='https://www.artic.edu/')
                    first = await images.fetch(store, IMAGE_ID)
                    self.assertEqual(await images.fetch(store, IMAGE_ID), first)
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(str(calls[0].url), IMAGE_URL)
                    self.assertEqual(calls[0].headers['referer'], 'https://www.artic.edu/')
                    with self.assertRaises(MuseumImageUnavailable):
                        await images.fetch(store, 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa')
                    self.assertEqual(len(calls), 1)
                finally:
                    store.close()
                    await images.close()

    async def test_generic_import_cannot_authorize_museum_image_fetch(self):
        calls = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: calls.append(request))) as client:
            images = MuseumImages(client=client)
            with tempfile.TemporaryDirectory() as directory:
                store = CollectionDatabase(directory)
                try:
                    row = museum_record('aic', '1', 'Example', 'https://www.artic.edu/artworks/1', image_url=IMAGE_URL)
                    store.import_records([row], collection_id='my-art', label='My art', source_kind='custom')
                    with self.assertRaises(MuseumImageUnavailable):
                        await images.fetch(store, IMAGE_ID)
                    self.assertEqual(calls, [])
                finally:
                    store.close()
                    await images.close()


if __name__ == '__main__':
    unittest.main()
