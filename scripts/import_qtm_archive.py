"""Import a local story file or the first-party public QTM snapshot, without GPUs."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

EXPORT_URL = 'https://www.queeringthemap.com/data/descriptions.json'
LICENSE = 'CC BY-NC-SA 4.0'
LICENSE_URL = 'https://creativecommons.org/licenses/by-nc-sa/4.0/'
MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024
SNAPSHOT_NAME = 'descriptions.json'
METADATA_NAME = 'descriptions.metadata.json'
_ID = re.compile(r'[0-9]{1,12}\Z', re.ASCII)


class DownloadError(RuntimeError):
    """A safe download/cache error with no contributor text or response body."""


def default_directory() -> Path:
    return Path(os.environ.get('LIVE_ARCHIVE_DIR') or
                str(Path(os.environ.get('LIVE_VOICE_STORE') or
                         Path.home() / '.local/share/character-studio') / 'archive'))


def _snapshot_shape(path: Path) -> None:
    try:
        with path.open('r', encoding='utf-8') as stream:
            value = json.load(stream)
        valid = (isinstance(value, dict) and bool(value) and
                 all(isinstance(key, str) and _ID.fullmatch(key) and int(key) > 0
                     and isinstance(text, str) for key, text in value.items()))
    except (OSError, ValueError, UnicodeError):
        valid = False
    if not valid:
        raise DownloadError('The official export must be a JSON object mapping numeric story IDs to text.')


def _etag(value) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 512:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    return value


def _verified_cache(snapshot: Path, metadata: Path) -> dict | None:
    """A metadata claim alone never authorizes reusing an arbitrary file."""
    try:
        if snapshot.is_symlink() or metadata.is_symlink():
            return None
        if not snapshot.is_file() or not metadata.is_file() or metadata.stat().st_size > 16_384:
            return None
        info = json.loads(metadata.read_text(encoding='utf-8'))
        if not isinstance(info, dict):
            return None
        expected = {'schema': 1, 'source_url': EXPORT_URL, 'license': LICENSE,
                    'license_url': LICENSE_URL}
        if any(info.get(key) != value for key, value in expected.items()):
            return None
        size = snapshot.stat().st_size
        if not 0 < size <= MAX_DOWNLOAD_BYTES or type(info.get('bytes')) is not int or info['bytes'] != size:
            return None
        digest = hashlib.sha256()
        with snapshot.open('rb') as stream:
            for chunk in iter(lambda: stream.read(65_536), b''):
                digest.update(chunk)
        if info.get('sha256') != digest.hexdigest():
            return None
        stamp = datetime.fromisoformat(info['downloaded_at'])
        if stamp.tzinfo is None or (info.get('etag') is not None and _etag(info['etag']) is None):
            return None
        _snapshot_shape(snapshot)
        return info
    except (OSError, ValueError, TypeError, KeyError, DownloadError):
        return None


def _temporary(directory: Path):
    descriptor, name = tempfile.mkstemp(prefix='.qtm-import-', dir=directory)
    os.fchmod(descriptor, 0o600)
    return descriptor, Path(name)


def download_snapshot(directory: Path, *, refresh: bool = False, client=None) -> Path:
    """Fetch only the fixed official export; reuse a byte-verified snapshot by default."""
    imports = Path(directory) / 'imports'
    if imports.is_symlink():
        raise DownloadError('The archive imports directory must not be a symbolic link.')
    imports.mkdir(parents=True, exist_ok=True, mode=0o700)
    snapshot, metadata = imports / SNAPSHOT_NAME, imports / METADATA_NAME
    for path in (snapshot, metadata):
        if path.is_symlink():
            raise DownloadError('The archive snapshot and metadata must not be symbolic links.')
    cached = _verified_cache(snapshot, metadata)
    if cached and not refresh:
        return snapshot
    headers = {'Accept': 'application/json', 'User-Agent': 'CharacterStudio-archive-import/1.0'}
    if cached and refresh and cached.get('etag'):
        headers['If-None-Match'] = cached['etag']
    temporary = metadata_temporary = None
    try:
        with ExitStack() as stack:
            http = client or stack.enter_context(httpx.Client(
                timeout=httpx.Timeout(30, connect=10), follow_redirects=False))
            with http.stream('GET', EXPORT_URL, headers=headers, follow_redirects=False) as response:
                if response.status_code == 304:
                    verified = _verified_cache(snapshot, metadata)
                    if cached and verified and verified == cached:
                        return snapshot
                    raise DownloadError('The server returned an unchanged export without a valid local snapshot. Retry the download.')
                if response.status_code != 200:
                    raise DownloadError(f'The official export was not downloaded (HTTP {response.status_code}). The previous snapshot was kept.')
                if response.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
                    raise DownloadError('The official export did not return JSON. The previous snapshot was kept.')
                length = response.headers.get('content-length')
                if length is not None:
                    try:
                        if int(length) < 0 or int(length) > MAX_DOWNLOAD_BYTES:
                            raise DownloadError('The official export exceeds the 128 MiB download limit.')
                    except ValueError as error:
                        raise DownloadError('The official export returned an invalid size.') from error
                descriptor, temporary = _temporary(imports)
                digest, count = hashlib.sha256(), 0
                with os.fdopen(descriptor, 'wb') as stream:
                    for chunk in response.iter_bytes(chunk_size=65_536):
                        count += len(chunk)
                        if count > MAX_DOWNLOAD_BYTES:
                            raise DownloadError('The official export exceeds the 128 MiB download limit.')
                        digest.update(chunk)
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                _snapshot_shape(temporary)
                info = {'schema': 1, 'source_url': EXPORT_URL,
                        'downloaded_at': datetime.now(timezone.utc).isoformat(),
                        'sha256': digest.hexdigest(), 'bytes': count,
                        'etag': _etag(response.headers.get('etag')),
                        'license': LICENSE, 'license_url': LICENSE_URL}
                descriptor, metadata_temporary = _temporary(imports)
                with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                    json.dump(info, stream, ensure_ascii=False, indent=2)
                    stream.write('\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, snapshot)
                os.replace(metadata_temporary, metadata)
                return snapshot
    except httpx.HTTPError as error:
        raise DownloadError('The official export download failed. Check the connection and try again; no incomplete download was imported.') from error
    finally:
        for path in (temporary, metadata_temporary):
            if path is not None:
                path.unlink(missing_ok=True)


def main(argv=None, *, database_factory=None, client=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--download', action='store_true', help='Download or reuse the verified official QTM text snapshot.')
    source.add_argument('--file', type=Path, help='Import an existing JSON, JSONL or CSV file.')
    parser.add_argument('--directory', type=Path, default=default_directory(), help='Archive directory used by the portal.')
    parser.add_argument('--format', choices=('auto', 'json', 'jsonl', 'csv'), default='auto')
    parser.add_argument('--refresh', action='store_true', help='Check the official snapshot again, using its ETag when available.')
    args = parser.parse_args(argv)
    if args.refresh and not args.download:
        parser.error('--refresh requires --download')
    if args.download and args.format not in ('auto', 'json'):
        parser.error('The official export uses JSON; omit --format or use json.')
    try:
        path = download_snapshot(args.directory, refresh=args.refresh, client=client) if args.download else args.file
        if database_factory is None:
            from live_voice.archive_database import ArchiveDatabase
            database_factory = ArchiveDatabase
        database = database_factory(args.directory)
        try:
            result = database.import_file(path, format='json' if args.download else args.format)
        finally:
            close = getattr(database, 'close', None)
            if callable(close):
                close()
        print(f"Imported {result['added']} new stories; {result['duplicates']} duplicates. Database contains {result['total']} stories.")
        return 0
    except (DownloadError, OSError, ValueError, OverflowError, sqlite3.Error) as error:
        # Import validation can include row numbers, but never echo file contents.
        message = str(error) if isinstance(error, DownloadError) else 'The story file could not be imported. Check its format, size and archive-directory permissions.'
        print(message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
