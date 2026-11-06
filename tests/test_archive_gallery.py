"""Gallery navigation preserves the studio without inference or archive mutation."""
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urljoin

from fastapi.testclient import TestClient

from live_voice.app import create_app
from live_voice.archive_database import ArchiveDatabase
from live_voice.collections import CollectionDatabase


class NoInference:
    def __init__(self):
        self.closed = False
        self.stt = self

    def load(self):
        raise AssertionError('Opening a gallery route must not load a speech model.')

    async def health(self):
        raise AssertionError('Opening a gallery route must not contact inference services.')

    async def close(self):
        self.closed = True


class DocumentAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = []

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == 'script' and attributes.get('src'):
            self.assets.append(attributes['src'])
        if tag == 'link' and attributes.get('rel') == 'stylesheet':
            self.assets.append(attributes['href'])


class ArchiveGalleryRoutes(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        directory = Path(self.temporary.name)
        self.stories = ArchiveDatabase(directory / 'stories')
        self.art = CollectionDatabase(directory / 'art')
        self.services = NoInference()
        with patch.dict('os.environ', {'LIVE_PROFILE': 'shared', 'LIVE_LISTENING_MODE': 'baseline',
                                      'LIVE_DEFAULT_ENGINE': 'baseline', 'LIVE_OMNI_URL': ''}):
            self.app = create_app(preview=True, providers=self.services,
                                  archive_store=self.stories, collections_store=self.art)
        self.client = TestClient(self.app)
        self.initial_collections = self.art.list_collections()
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.stories.close()
        self.art.close()
        self.temporary.cleanup()

    def test_gallery_and_existing_views_share_the_retained_studio_document(self):
        pages = [self.client.get(path) for path in ('/', '/archive/', '/archive/gallery/')]
        for page in pages:
            self.assertEqual(page.status_code, 200)
            self.assertTrue(page.headers['content-type'].startswith('text/html'))
            self.assertEqual(page.headers['cache-control'], 'no-store')
            self.assertIn('Character Studio', page.text)
            self.assertIn('id="character-form"', page.text)
            self.assertIn('id="inspect-model"', page.text)
        self.assertEqual(pages[0].content, pages[1].content)
        self.assertEqual(pages[0].content, pages[2].content)
        self.assertEqual(self.stories.count(), 0)
        self.assertEqual(self.art.count(), 0)
        self.assertEqual(self.art.list_collections(), self.initial_collections)

    def test_nested_gallery_loads_the_same_root_absolute_script_and_style_assets(self):
        document = self.client.get('/archive/gallery/')
        assets = DocumentAssets()
        assets.feed(document.text)
        self.assertIn('/static/live.js', assets.assets)
        self.assertIn('/static/live.css', assets.assets)
        self.assertIn('/static/styles.css', assets.assets)
        for asset in assets.assets:
            with self.subTest(asset=asset):
                self.assertTrue(asset.startswith('/static/'))
                absolute = urljoin('http://testserver/archive/gallery/', asset)
                response = self.client.get(absolute)
                self.assertEqual(response.status_code, 200)
                self.assertGreater(len(response.content), 0)
                self.assertNotIn('text/html', response.headers.get('content-type', ''))
                self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_unsuffixed_gallery_redirects_without_losing_the_existing_views(self):
        redirect = self.client.get('/archive/gallery', follow_redirects=False)
        self.assertIn(redirect.status_code, (307, 308))
        self.assertEqual(redirect.headers['location'], 'http://testserver/archive/gallery/')
        self.assertEqual(self.client.get('/archive/').status_code, 200)
        self.assertEqual(self.client.get('/').status_code, 200)


if __name__ == '__main__':
    unittest.main()
