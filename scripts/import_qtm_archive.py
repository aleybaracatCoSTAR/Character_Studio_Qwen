"""Import a local story file or the first-party public QTM snapshot, without GPUs."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import errno
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


_STAGES = frozenset(('prepare_source', 'open_database', 'import_records', 'close_database'))
_SQLITE_FAILURES = {
    sqlite3.SQLITE_BUSY: ('sqlite_busy', 'Another process is using the archive database. Stop the portal and other imports, then retry.'),
    sqlite3.SQLITE_LOCKED: ('sqlite_locked', 'The archive database is locked. Stop the portal and other imports, then retry.'),
    sqlite3.SQLITE_READONLY: ('sqlite_readonly', 'The archive database is read-only. Check the chosen directory and its permissions.'),
    sqlite3.SQLITE_CANTOPEN: ('sqlite_cantopen', 'SQLite could not open the archive database. Check that the chosen directory exists and is writable.'),
    sqlite3.SQLITE_FULL: ('sqlite_full', 'SQLite ran out of storage. Check free space and your storage quota before retrying.'),
    sqlite3.SQLITE_IOERR: ('sqlite_io', 'SQLite reported a filesystem I/O problem. Check the shared-storage connection and locking support.'),
    sqlite3.SQLITE_CORRUPT: ('sqlite_corrupt', 'SQLite reports a damaged database. Preserve the existing database and test with a separate archive directory.'),
    sqlite3.SQLITE_NOTADB: ('sqlite_not_database', 'The saved database is not a readable SQLite database. Preserve it and test with a separate archive directory.'),
}
_SQLITE_NAMES = {value: name for name, value in vars(sqlite3).items()
                 if name in ('SQLITE_BUSY', 'SQLITE_LOCKED', 'SQLITE_READONLY', 'SQLITE_CANTOPEN',
                             'SQLITE_FULL', 'SQLITE_IOERR', 'SQLITE_CORRUPT', 'SQLITE_NOTADB', 'SQLITE_ERROR')}
_KNOWN_VALIDATION = {
    'The local archive database has an unsupported schema version.':
        ('unsupported_schema', 'The saved archive uses an unsupported database schema. Preserve it and use a separate archive directory to test.'),
    'The existing selected-story archive is invalid; it was not changed.':
        ('legacy_invalid', 'The existing selected-story collection could not be migrated. Keep that file and test with a separate archive directory.'),
    'The existing selected-story archive is too large to migrate safely.':
        ('legacy_size_limit', 'The existing selected-story collection exceeds the migration limit. Keep that file and test with a separate archive directory.'),
    'The local archive needs working SQLite rollback journalling and filesystem locking.':
        ('filesystem_locking', 'The archive needs SQLite rollback journalling and filesystem locking. Stop other users of this database or test a local filesystem.'),
    'Use a local archive file no larger than 128 MiB.':
        ('import_file_limit', 'Use a readable local story file no larger than 128 MiB; check the file format and path.'),
    'Choose a JSON, JSONL or CSV archive file.':
        ('import_format', 'Choose JSON, JSONL or CSV as the story file format.'),
    'The local archive has reached its entry limit.':
        ('entry_limit', 'The archive has reached its entry limit. Keep the existing database and reduce the import size.'),
}
_ROW_FAILURE = re.compile(
    r'Archive import row ([1-9][0-9]{0,8}) '
    r'(is invalid or conflicts with a saved entry|is not a supported JSON file|'
    r'has an unsupported JSON structure|is not a JSON record|is not a valid CSV record|'
    r'needs CSV headers including text or description|has invalid CSV columns|'
    r'is not UTF-8 text|exceeds its file or line limit)\.\Z')


def safe_diagnostic(error, stage) -> dict:
    """Return only fixed messages and bounded codes; never echo exception text."""
    result = {'stage': stage if stage in _STAGES else 'unknown', 'reason': 'import_failed',
              'message': 'The story file could not be imported at this stage. Check its format, size and archive-directory permissions.'}
    chain, seen = [], set()
    current = error
    for _ in range(6):
        if current is None or id(current) in seen:
            break
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ if current.__cause__ is not None else current.__context__
    for item in chain:
        if type(item) in (ValueError, OverflowError):
            match = _ROW_FAILURE.fullmatch(str(item))
            if match:
                result['row'] = int(match[1])
                break
    # DownloadError wraps HTTP failures, whose lower-level OSError must not be
    # mistaken for a failure of the local archive filesystem.
    for item in chain:
        if isinstance(item, DownloadError):
            text = str(item)
            status = re.fullmatch(r'The official export was not downloaded \(HTTP ([1-5][0-9]{2})\)\. The previous snapshot was kept\.', text)
            if status:
                result.update(reason='download_http', http_status=int(status[1]), message='The official export request failed. The previous snapshot was kept; check the HTTP status in this diagnostic.')
            elif text in ('The official export exceeds the 128 MiB download limit.', 'The official export returned an invalid size.'):
                result.update(reason='download_size', message='The official export returned an invalid size or exceeded the 128 MiB download limit.')
            elif text in ('The official export must be a JSON object mapping numeric story IDs to text.', 'The official export did not return JSON. The previous snapshot was kept.'):
                result.update(reason='download_format', message='The official export did not have the expected JSON format. The previous snapshot was kept.')
            elif text in ('The archive imports directory must not be a symbolic link.',
                          'The archive snapshot and metadata must not be symbolic links.'):
                result.update(reason='cache_symlink', message='The archive cache must use ordinary files and directories rather than symbolic links.')
            else:
                result.update(reason='download_failed', message='The official export could not be prepared. Check the connection and cache location, then retry.')
            return result
    for item in chain:
        if not isinstance(item, sqlite3.Error):
            continue
        code = getattr(item, 'sqlite_errorcode', None)
        if type(code) is int and 0 <= code <= 2**31 - 1:
            primary = code & 255
            result['sqlite_code'] = code
            result['sqlite_primary_code'] = primary
            name = getattr(item, 'sqlite_errorname', None)
            if (isinstance(name,str) and re.fullmatch(r'SQLITE_[A-Z0-9_]{1,64}',name)
                    and getattr(sqlite3,name,None) == code):
                result['sqlite_extended_name'] = name
            if primary in _SQLITE_NAMES:
                result['sqlite_name'] = _SQLITE_NAMES[primary]
            if primary in _SQLITE_FAILURES:
                result['reason'], result['message'] = _SQLITE_FAILURES[primary]
                return result
        if str(item).strip().lower() == 'no such module: fts5':
            result.update(reason='fts5_unavailable', message='This Python SQLite library lacks FTS5. Use an environment with SQLite FTS5 support; the model environment and GPU settings do not need changing.')
        else:
            result.update(reason='sqlite_error', message='SQLite could not complete this archive operation. Check the database stage, file format and filesystem permissions; the diagnostic contains no story text.')
        return result
    for item in chain:
        if not isinstance(item, OSError):
            continue
        number = getattr(item, 'errno', None)
        if type(number) is int and 0 <= number <= 4096:
            result['errno'] = number
        choices = (
            ({errno.EACCES, errno.EPERM}, 'permission_denied', 'The archive operation was denied by filesystem permissions. Check the chosen directory and file ownership.'),
            ({errno.EROFS}, 'readonly_filesystem', 'The chosen archive location is on a read-only filesystem.'),
            ({errno.ENOSPC, getattr(errno, 'EDQUOT', errno.ENOSPC)}, 'storage_full', 'The archive location has insufficient free space or quota.'),
            ({errno.ENOENT}, 'missing_path', 'A required archive file or directory is missing. Check the chosen location and input file.'),
            ({errno.ENOTDIR, errno.EISDIR}, 'wrong_path_type', 'An archive path has the wrong file or directory type.'),
            ({errno.EBUSY, errno.EAGAIN}, 'filesystem_busy', 'The archive filesystem is busy. Stop other imports or portal processes and retry.'),
            ({errno.EIO, getattr(errno, 'ESTALE', errno.EIO)}, 'filesystem_io', 'The archive filesystem reported an I/O problem. Check the shared-storage connection.'),
        )
        for numbers, reason, message in choices:
            if number in numbers:
                result.update(reason=reason, message=message)
                return result
        result.update(reason='filesystem_error', message='The filesystem could not complete this archive operation. Check the chosen directory, available space and permissions.')
        return result
    for item in chain:
        # Pydantic errors can embed contributor text; only our plain ValueErrors
        # are eligible for the exact-message and fixed row-number whitelist.
        if type(item) not in (ValueError, OverflowError):
            continue
        text = str(item)
        if text in _KNOWN_VALIDATION:
            result['reason'], result['message'] = _KNOWN_VALIDATION[text]
            return result
        match = _ROW_FAILURE.fullmatch(text)
        if match:
            result.update(reason='invalid_import_row', row=int(match[1]),
                          message='A story record failed import validation. Check the indicated row and file format; no contributor text is printed.')
            return result
    return result


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
    stage = 'prepare_source'
    try:
        print(f'Archive directory: {args.directory.resolve()}', flush=True)
        print(f'Archive import stage: {stage}', flush=True)
        path = download_snapshot(args.directory, refresh=args.refresh, client=client) if args.download else args.file
        if database_factory is None:
            from live_voice.archive_database import ArchiveDatabase
            database_factory = ArchiveDatabase
        stage = 'open_database'
        print(f'Archive import stage: {stage}', flush=True)
        database = database_factory(args.directory)
        import_error = None
        try:
            stage = 'import_records'
            print(f'Archive import stage: {stage}', flush=True)
            result = database.import_file(path, format='json' if args.download else args.format)
        except BaseException as error:
            import_error = error
            raise
        finally:
            close = getattr(database, 'close', None)
            if callable(close):
                print('Archive import stage: close_database', flush=True)
                if import_error is None:
                    stage = 'close_database'
                    close()
                else:
                    # Cleanup must not mask the failure that prevented import.
                    try:
                        close()
                    except Exception:
                        pass
        print(f"Imported {result['added']} new stories; {result['duplicates']} duplicates. Database contains {result['total']} stories.")
        return 0
    except (DownloadError, OSError, ValueError, OverflowError, sqlite3.Error) as error:
        diagnostic = safe_diagnostic(error, stage)
        print(diagnostic.pop('message'), file=sys.stderr)
        print('Archive import diagnostic: ' + json.dumps(diagnostic, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
