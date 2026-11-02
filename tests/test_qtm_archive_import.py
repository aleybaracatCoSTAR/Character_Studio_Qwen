"""No network, model or GPU: fixed-export caching and import CLI contracts."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

import httpx

from scripts import import_qtm_archive as importer


BODY = json.dumps({'123': '<p>Synthetic café story — no real contributor.</p>',
                   '456': 'A second invented passage.'}, ensure_ascii=False).encode('utf-8')


class QTMDownloadTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.directory = Path(self.folder.name) / 'private archive'

    def client(self, serve):
        client = httpx.Client(transport=httpx.MockTransport(serve), follow_redirects=True)
        self.addCleanup(client.close)
        return client

    def success(self, request):
        self.assertEqual(str(request.url), importer.EXPORT_URL)
        return httpx.Response(200, content=BODY, headers={'Content-Type': 'application/json', 'ETag': '"v1"'})

    def seed(self):
        return importer.download_snapshot(self.directory, client=self.client(self.success))

    def test_fixed_official_download_preserves_unicode_and_private_files_with_metadata_only(self):
        path = self.seed()
        self.assertEqual(path, self.directory / 'imports/descriptions.json')
        self.assertEqual(path.read_bytes(), BODY)
        metadata = path.with_name(importer.METADATA_NAME)
        info = json.loads(metadata.read_text())
        self.assertEqual(info['source_url'], importer.EXPORT_URL)
        self.assertEqual(info['license'], 'CC BY-NC-SA 4.0')
        self.assertEqual(info['bytes'], len(BODY))
        self.assertEqual(info['etag'], '"v1"')
        self.assertIsNotNone(importer._verified_cache(path, metadata))
        self.assertNotIn('Synthetic', metadata.read_text())
        for file in (path, metadata):
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        self.assertFalse(list(path.parent.glob('.qtm-import-*')))

    def test_default_reuses_verified_snapshot_without_an_http_request(self):
        path = self.seed()
        def unexpected(_request): raise AssertionError('A valid cache should not be downloaded again.')
        self.assertEqual(importer.download_snapshot(self.directory, client=self.client(unexpected)), path)

    def test_explicit_refresh_uses_etag_and_accepts_304_only_with_valid_cache(self):
        path = self.seed()
        def unchanged(request):
            self.assertEqual(request.headers['If-None-Match'], '"v1"')
            return httpx.Response(304)
        self.assertEqual(importer.download_snapshot(self.directory, refresh=True, client=self.client(unchanged)), path)
        self.assertEqual(path.read_bytes(), BODY)
        empty = self.directory / 'new'
        with self.assertRaises(importer.DownloadError):
            importer.download_snapshot(empty, refresh=True, client=self.client(lambda _: httpx.Response(304)))

    def test_cache_requires_hash_schema_source_and_json_not_just_metadata_claims(self):
        path = self.seed()
        metadata = path.with_name(importer.METADATA_NAME)
        original = metadata.read_bytes()
        for field, value in [('source_url', 'https://example.invalid/other.json'),
                             ('sha256', '0' * 64), ('schema', 2), ('downloaded_at', 'not-a-date'),
                             ('etag', 'bad\r\nheader'), ('bytes', len(BODY) + 1)]:
            info = json.loads(original)
            info[field] = value
            metadata.write_text(json.dumps(info))
            with self.subTest(field=field):
                self.assertIsNone(importer._verified_cache(path, metadata))
        metadata.write_bytes(original)
        path.write_bytes(b'{"123":"Tampered but still valid JSON"}')
        def replace(request):
            self.assertNotIn('If-None-Match', request.headers)
            return self.success(request)
        importer.download_snapshot(self.directory, refresh=True, client=self.client(replace))
        self.assertEqual(path.read_bytes(), BODY)

    def test_redirects_are_not_followed_even_with_redirect_enabled_client(self):
        requests = []
        def redirect(request):
            requests.append(request)
            return httpx.Response(302, headers={'Location': 'https://example.invalid/private'})
        with self.assertRaises(importer.DownloadError):
            importer.download_snapshot(self.directory, client=self.client(redirect))
        self.assertEqual(len(requests), 1)
        self.assertEqual(str(requests[0].url), importer.EXPORT_URL)

    def test_rejected_content_and_sizes_keep_previous_snapshot(self):
        path = self.seed()
        metadata = path.with_name(importer.METADATA_NAME)
        original_metadata = metadata.read_bytes()
        cases = [httpx.Response(200, content=b'<html>private rejected text</html>', headers={'Content-Type':'text/html'}),
                 httpx.Response(200, content=b'{invalid secret JSON', headers={'Content-Type':'application/json'}),
                 httpx.Response(200, json={'unexpected': 'a story'}),
                 httpx.Response(200, json=['not the official map']),
                 httpx.Response(200, json={'123': None}),
                 httpx.Response(200, json={'123': 'x'}, headers={'Content-Length':str(importer.MAX_DOWNLOAD_BYTES + 1)}),
                 httpx.Response(503, text='private upstream response')]
        for response in cases:
            with self.subTest(status=response.status_code), self.assertRaises(importer.DownloadError) as caught:
                importer.download_snapshot(self.directory, refresh=True, client=self.client(lambda _, r=response: r))
            self.assertNotIn('private', str(caught.exception))
            self.assertEqual(path.read_bytes(), BODY)
            self.assertEqual(metadata.read_bytes(), original_metadata)
            self.assertFalse(list(path.parent.glob('.qtm-import-*')))

    def test_streaming_bound_and_read_failure_do_not_leave_partial_file(self):
        path = self.seed()
        class Broken(httpx.SyncByteStream):
            def __iter__(self):
                yield b'{"123":"beginning'
                raise httpx.ReadError('sensitive upstream diagnostic')
        for body in (Broken(), httpx.ByteStream(b'x' * 101)):
            client = self.client(lambda _: httpx.Response(200, stream=body, headers={'Content-Type':'application/json'}))
            with self.subTest(kind=type(body).__name__), patch.object(importer, 'MAX_DOWNLOAD_BYTES', 100):
                with self.assertRaises(importer.DownloadError) as caught:
                    importer.download_snapshot(self.directory, refresh=True, client=client)
                self.assertNotIn('sensitive', str(caught.exception))
            self.assertEqual(path.read_bytes(), BODY)
            self.assertFalse(list(path.parent.glob('.qtm-import-*')))

    def test_symlink_cache_and_metadata_are_rejected(self):
        path = self.seed()
        for file in (path, path.with_name(importer.METADATA_NAME)):
            saved = file.read_bytes()
            target = self.directory / 'untouched'
            target.write_bytes(saved)
            file.unlink()
            file.symlink_to(target)
            with self.subTest(file=file.name), self.assertRaises(importer.DownloadError):
                importer.download_snapshot(self.directory, client=self.client(self.success))
            self.assertEqual(target.read_bytes(), saved)
            file.unlink()
            file.write_bytes(saved)

    def test_bad_etag_is_not_persisted_or_used_in_headers(self):
        client = self.client(lambda _: httpx.Response(200, content=BODY, headers={'Content-Type':'application/json','ETag':'bad\nvalue'}))
        path = importer.download_snapshot(self.directory, client=client)
        self.assertIsNone(json.loads(path.with_name(importer.METADATA_NAME).read_text())['etag'])


class QTMImportCLITests(unittest.TestCase):
    def test_default_directory_matches_live_environment_priority(self):
        with patch.dict(os.environ, {'LIVE_ARCHIVE_DIR':'/tmp/archive','LIVE_VOICE_STORE':'/tmp/store'}):
            self.assertEqual(importer.default_directory(), Path('/tmp/archive'))
        with patch.dict(os.environ, {'LIVE_VOICE_STORE':'/tmp/store'}, clear=True):
            self.assertEqual(importer.default_directory(), Path('/tmp/store/archive'))
        with patch.dict(os.environ, {}, clear=True), patch.object(Path, 'home', return_value=Path('/tmp/home')):
            self.assertEqual(importer.default_directory(), Path('/tmp/home/.local/share/character-studio/archive'))

    def test_local_file_routes_format_and_directory_without_download_or_model_import(self):
        calls = []
        class Database:
            def __init__(self, directory): calls.append(('directory', directory))
            def import_file(self, path, format='auto'):
                calls.append(('import', path, format))
                return {'added':2,'duplicates':1,'total':3}
        for suffix in ('json','jsonl','csv'):
            with self.subTest(format=suffix), redirect_stdout(StringIO()) as output, patch.object(importer, 'download_snapshot', side_effect=AssertionError('No network for local files')):
                status = importer.main(['--file', '/tmp/with spaces/stories.'+suffix, '--format', suffix,
                                        '--directory','/tmp/my archive'], database_factory=Database)
            self.assertEqual(status, 0)
            self.assertIn('2 new stories; 1 duplicates', output.getvalue())
            self.assertEqual(calls[-2:], [('directory', Path('/tmp/my archive')),
                                        ('import', Path('/tmp/with spaces/stories.'+suffix), suffix)])

    def test_download_routes_verified_snapshot_as_json_and_refresh_is_explicit(self):
        class Database:
            def __init__(self, directory): self.directory=directory
            def import_file(self, path, format='auto'):
                self_test.assertEqual(path, Path('/tmp/cache.json'))
                self_test.assertEqual(format, 'json')
                return {'added':1,'duplicates':0,'total':1}
        self_test = self
        with patch.object(importer, 'download_snapshot', return_value=Path('/tmp/cache.json')) as download, redirect_stdout(StringIO()):
            self.assertEqual(importer.main(['--download','--directory','/tmp/archive'], database_factory=Database), 0)
        download.assert_called_once_with(Path('/tmp/archive'), refresh=False, client=None)
        with patch.object(importer, 'download_snapshot', return_value=Path('/tmp/cache.json')) as download, redirect_stdout(StringIO()):
            self.assertEqual(importer.main(['--download','--refresh','--directory','/tmp/archive'], database_factory=Database), 0)
        download.assert_called_once_with(Path('/tmp/archive'), refresh=True, client=None)

    def test_invalid_arguments_do_not_start_a_download(self):
        for arguments in (['--refresh','--file','x.json'], ['--download','--file','x.json'],
                          ['--download','--format','csv'], ['--download','--url','https://example.invalid']):
            with self.subTest(arguments=arguments), redirect_stderr(StringIO()), patch.object(importer, 'download_snapshot') as download:
                with self.assertRaises(SystemExit) as caught: importer.main(arguments)
                self.assertEqual(caught.exception.code, 2)
                download.assert_not_called()

    def test_import_errors_are_safe_and_do_not_echo_contributor_text(self):
        class Database:
            def __init__(self, _directory): pass
            def import_file(self, *_args, **_kwargs): raise ValueError('private story: confidential')
        with redirect_stderr(StringIO()) as error:
            result = importer.main(['--file','/tmp/stories.json'], database_factory=Database)
        self.assertEqual(result, 1)
        self.assertNotIn('confidential', error.getvalue())
        self.assertIn('format', error.getvalue())

    def test_database_is_closed_after_success_or_failure_and_sqlite_errors_are_safe(self):
        for failure in (None, ValueError('private contributor text'), sqlite3.OperationalError('private database path')):
            closed = []
            class Database:
                def __init__(self, _directory): pass
                def import_file(self, *_args, **_kwargs):
                    if failure is not None: raise failure
                    return {'added':1,'duplicates':0,'total':1}
                def close(self): closed.append(True)
            with self.subTest(failure=type(failure).__name__), redirect_stdout(StringIO()), redirect_stderr(StringIO()) as error:
                result = importer.main(['--file','/tmp/stories.json'], database_factory=Database)
            self.assertEqual(result, int(failure is not None))
            self.assertEqual(closed, [True])
            self.assertNotIn('private', error.getvalue())

    def test_real_database_imports_all_file_formats_and_deduplicates_repeated_runs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            samples = {
                'json': '{"123":"Synthetic café passage."}',
                'jsonl': '{"short_id":"123","description":"<p>Synthetic café passage.</p>"}\n',
                'csv': 'short_id,description\n123,<p>Synthetic café passage.</p>\n',
            }
            for format, content in samples.items():
                source = root / ('stories.' + format)
                source.write_text(content, encoding='utf-8')
                directory = root / ('archive-' + format)
                for expected in ('1 new stories; 0 duplicates', '0 new stories; 1 duplicates'):
                    with self.subTest(format=format, expected=expected), redirect_stdout(StringIO()) as output:
                        status = importer.main(['--file', str(source), '--directory', str(directory)])
                    self.assertEqual(status, 0)
                    self.assertIn(expected, output.getvalue())
                with sqlite3.connect(directory / 'archive.sqlite3') as connection:
                    entries = connection.execute('SELECT text,source_url,provenance FROM entries').fetchall()
                self.assertEqual(entries, [('Synthetic café passage.', 'https://www.queeringthemap.com/moment/123', 'imported')])


if __name__ == '__main__':
    unittest.main()
