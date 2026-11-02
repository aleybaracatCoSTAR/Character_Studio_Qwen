"""A local SQLite FTS5 archive; only bounded retrieved excerpts enter a call."""
from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import uuid

from .archive import (
    ArchiveEntry, ArchiveSettings, MANIFEST_NAME, MAX_MATCHES,
    _Manifest, _excerpt, _query_terms, _terms,
)
from .archive_source import description_text


MAX_DATABASE_ENTRIES = 500_000
MAX_IMPORT_BYTES = 128 * 1024 * 1024
MAX_IMPORT_LINE_BYTES = 256 * 1024
MAX_PAGE_SIZE = 100
MAX_QUERY_TERMS = 64
DATABASE_NAME = 'archive.sqlite3'
_COLUMNS = 'id,title,text,source_url,source_entry_id,provenance'
_OPAQUE_ID = re.compile(r'^[a-f0-9]{32}$')
_SOURCE_ID = re.compile(r'^[0-9]{1,12}$')


def _record(row):
    return ArchiveEntry.model_validate({name: row[name] for name in _COLUMNS.split(',')}).model_dump()


def _fts_query(query, history=()):
    if not isinstance(query, str):
        raise ValueError('Use a plain-text archive question.')
    terms = set(sorted(_query_terms(query, history))[:MAX_QUERY_TERMS])
    # Parameter binding protects SQL; quoting each normalized term additionally
    # prevents user input from becoming FTS operators, prefixes, or column names.
    expression = ' OR '.join('"' + term.replace('"', '""') + '"' for term in sorted(terms))
    return terms, expression


def _matches(connection, query, history=()):
    terms, expression = _fts_query(query, history)
    if not expression:
        return []
    rows = connection.execute(
        'SELECT e.* FROM archive_fts JOIN entries AS e ON e.rowid=archive_fts.rowid '
        'WHERE archive_fts MATCH ? ORDER BY bm25(archive_fts,2.0,1.0),e.rowid LIMIT ?',
        (expression, MAX_MATCHES),
    ).fetchall()
    result = []
    for row in rows:
        entry = _record(row)
        result.append({
            'id': entry['id'],
            'citation_id': ('QTM-' + entry['source_entry_id']) if entry['source_entry_id'] else 'passage-' + entry['id'][:8],
            'title': entry['title'], 'source_url': entry['source_url'],
            'source_entry_id': entry['source_entry_id'], 'provenance': entry['provenance'],
            'excerpt': _excerpt(entry['text'], terms),
        })
    return result


class ArchiveSnapshot:
    """A private frozen in-memory copy, safe across worker threads.

    The persistent archive may live on cluster storage shared by different hosts,
    so it uses rollback journalling, not WAL. SQLite's backup API copies one
    consistent database state; the source connection closes before a call starts.
    """
    def __init__(self, path):
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(':memory:', timeout=30, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        try:
            source = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', timeout=30, uri=True)
            try:
                source.backup(self._connection)
            finally:
                source.close()
            self._connection.execute('PRAGMA query_only=ON')
            self.count = self._connection.execute('SELECT count(*) FROM entries').fetchone()[0]
        except Exception:
            self._connection.close()
            raise

    def search(self, query, history=()):
        with self._lock:
            if self._connection is None:
                raise ValueError('The archive call snapshot is closed.')
            return _matches(self._connection, query, history)

    def close(self):
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None


class ArchiveDatabase:
    """Persistent source records and lexical index; no network or model dependency."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self.path = self.directory / DATABASE_NAME
        if self.path.is_symlink():
            raise ValueError('The local archive database must not be a symbolic link.')
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        self.path.chmod(0o600)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        try:
            mode = self._connection.execute('PRAGMA journal_mode=DELETE').fetchone()[0]
            if mode.lower() != 'delete':
                raise ValueError('The local archive needs working SQLite rollback journalling and filesystem locking.')
            self._connection.execute('PRAGMA busy_timeout=30000')
            self._connection.executescript('''
                CREATE TABLE IF NOT EXISTS archive_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS entries(
                    rowid INTEGER PRIMARY KEY,
                    id TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    text TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    source_entry_id TEXT,
                    provenance TEXT NOT NULL,
                    text_hash TEXT NOT NULL,
                    UNIQUE(source_url,text_hash)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS archive_fts USING fts5(
                    title_terms,body_terms,tokenize='unicode61 remove_diacritics 2'
                );
            ''')
            self._migrate()
        except sqlite3.Error:
            self._connection.close()
            raise ValueError('The local archive requires SQLite FTS5 and working filesystem locking; its database could not be opened.') from None
        except Exception:
            self._connection.close()
            raise

    def _migrate(self):
        connection = self._connection
        connection.execute('BEGIN IMMEDIATE')
        try:
            version = connection.execute("SELECT value FROM archive_meta WHERE key='schema'").fetchone()
            if version is not None and version[0] != '1':
                raise ValueError('The local archive database has an unsupported schema version.')
            connection.execute("INSERT OR IGNORE INTO archive_meta VALUES('schema','1')")
            migrated = connection.execute("SELECT value FROM archive_meta WHERE key='legacy_migrated'").fetchone()
            if migrated is None:
                legacy = self.directory / MANIFEST_NAME
                if legacy.exists():
                    if not legacy.is_file() or legacy.stat().st_size > 2_000_000:
                        raise ValueError('The existing selected-story archive is too large to migrate safely.')
                    try:
                        entries = _Manifest.model_validate_json(legacy.read_text(encoding='utf-8')).entries
                    except (ValueError, UnicodeError):
                        raise ValueError('The existing selected-story archive is invalid; it was not changed.') from None
                    total = self._count(connection)
                    for entry in entries:
                        added, _ = self._insert(connection, entry, total)
                        total += int(added)
                connection.execute("INSERT INTO archive_meta VALUES('legacy_migrated','1')")
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    @staticmethod
    def _count(connection):
        return connection.execute('SELECT count(*) FROM entries').fetchone()[0]

    @staticmethod
    def _insert(connection, entry, total):
        digest = hashlib.sha256(entry.text.encode('utf-8')).hexdigest()
        duplicate = connection.execute(
            'SELECT * FROM entries WHERE source_url=? AND text_hash=?', (entry.source_url, digest)).fetchone()
        if duplicate is not None:
            if duplicate['text'] != entry.text:
                raise ValueError('The archive found a conflicting text fingerprint.')
            return False, _record(duplicate)
        if total >= MAX_DATABASE_ENTRIES:
            raise OverflowError('The local archive has reached its entry limit.')
        cursor = connection.execute(
            'INSERT INTO entries(id,title,text,source_url,source_entry_id,provenance,text_hash) VALUES(?,?,?,?,?,?,?)',
            (entry.id, entry.title, entry.text, entry.source_url, entry.source_entry_id, entry.provenance, digest))
        connection.execute('INSERT INTO archive_fts(rowid,title_terms,body_terms) VALUES(?,?,?)',
                           (cursor.lastrowid, ' '.join(_terms(entry.title)), ' '.join(_terms(entry.text))))
        return True, entry.model_dump()

    def add(self, *, text, source_url, title='', source_entry_id=None, provenance='pasted'):
        if not isinstance(title, str):
            raise ValueError('Use a plain-text title.')
        entry = ArchiveEntry(id=uuid.uuid4().hex, title=title.strip() or 'Selected Queering the Map passage',
                             text=text, source_url=source_url, source_entry_id=source_entry_id, provenance=provenance)
        if not title.strip() and entry.source_entry_id:
            entry = entry.model_copy(update={'title': f'Queering the Map #{entry.source_entry_id}'})
        with self._lock:
            self._connection.execute('BEGIN IMMEDIATE')
            try:
                _, saved = self._insert(self._connection, entry, self._count(self._connection))
                self._connection.commit()
                return saved
            except Exception:
                self._connection.rollback()
                raise

    def remove(self, entry_id):
        with self._lock:
            self._connection.execute('BEGIN IMMEDIATE')
            try:
                row = self._connection.execute('SELECT rowid FROM entries WHERE id=?', (entry_id,)).fetchone()
                if row is None:
                    self._connection.rollback()
                    return False
                self._connection.execute('DELETE FROM archive_fts WHERE rowid=?', (row[0],))
                self._connection.execute('DELETE FROM entries WHERE rowid=?', (row[0],))
                self._connection.commit()
                return True
            except Exception:
                self._connection.rollback()
                raise

    def list(self):
        """Legacy response bound; larger clients must use page()."""
        return self.page(limit=50)['entries']

    def count(self):
        with self._lock:
            return self._count(self._connection)

    def page(self, query='', offset=0, limit=25):
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 2**63 - 1:
            raise ValueError('Use a nonnegative archive page offset.')
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValueError('Use an archive page size between 1 and 100.')
        _, expression = _fts_query(query)
        with self._lock:
            connection = self._connection
            total = self._count(connection)
            if expression:
                matched = connection.execute('SELECT count(*) FROM archive_fts WHERE archive_fts MATCH ?', (expression,)).fetchone()[0]
                rows = connection.execute(
                    'SELECT e.* FROM archive_fts JOIN entries AS e ON e.rowid=archive_fts.rowid '
                    'WHERE archive_fts MATCH ? ORDER BY bm25(archive_fts,2.0,1.0),e.rowid LIMIT ? OFFSET ?',
                    (expression, limit, offset)).fetchall()
            else:
                matched = total if not query.strip() else 0
                rows = connection.execute('SELECT * FROM entries ORDER BY rowid LIMIT ? OFFSET ?',
                                          (limit, offset)).fetchall() if matched else []
            return {'entries': [_record(row) for row in rows], 'total': total,
                    'matched': matched, 'offset': offset, 'limit': limit}

    def snapshot(self, ids):
        selected = ArchiveSettings(entry_ids=list(ids)).entry_ids
        with self._lock:
            result = []
            for entry_id in selected:
                row = self._connection.execute('SELECT * FROM entries WHERE id=?', (entry_id,)).fetchone()
                if row is None:
                    raise KeyError(entry_id)
                result.append(ArchiveEntry.model_validate(_record(row)))
            return tuple(result)

    def open_snapshot(self):
        return ArchiveSnapshot(self.path)

    @staticmethod
    def _import_entry(record):
        if not isinstance(record, dict):
            raise ValueError('Use an archive record object.')
        raw_id = record.get('source_entry_id', record.get('short_id'))
        supplied_id = record.get('id')
        local_id = supplied_id if isinstance(supplied_id, str) and _OPAQUE_ID.fullmatch(supplied_id) else None
        if raw_id in (None, '') and supplied_id not in (None, '') and local_id is None:
            raw_id = supplied_id
        if raw_id not in (None, ''):
            if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)) or not _SOURCE_ID.fullmatch(str(raw_id).strip()):
                raise ValueError('Use a positive numeric source ID.')
            raw_id = str(int(str(raw_id).strip()))
            if raw_id == '0':
                raise ValueError('Use a positive numeric source ID.')
        else:
            raw_id = None
        if 'text' in record:
            text = record['text']
        else:
            text = description_text(record.get('description'))
        source = record.get('source_url')
        if source in (None, ''):
            source = 'https://www.queeringthemap.com/' + ('moment/' + raw_id if raw_id else '')
        title = record.get('title')
        if title in (None, ''):
            title = f'Queering the Map #{raw_id}' if raw_id else 'Imported Queering the Map passage'
        # Uploaded provenance is not proof that a connector fetched this text.
        entry = ArchiveEntry(id=local_id or uuid.uuid4().hex, title=title, text=text,
                             source_url=source, source_entry_id=raw_id, provenance='imported')
        return entry

    @staticmethod
    def _lines(path):
        total = 0
        with path.open('rb') as stream:
            index = 0
            while True:
                raw = stream.readline(MAX_IMPORT_LINE_BYTES + 1)
                if not raw:
                    break
                index += 1
                total += len(raw)
                if total > MAX_IMPORT_BYTES or len(raw) > MAX_IMPORT_LINE_BYTES:
                    raise ValueError(f'Archive import row {index} exceeds its file or line limit.')
                try:
                    yield raw.decode('utf-8-sig' if index == 1 else 'utf-8')
                except UnicodeError:
                    raise ValueError(f'Archive import row {index} is not UTF-8 text.') from None

    @staticmethod
    def _json_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('The JSON archive has duplicate object keys.')
            result[key] = value
        return result

    @classmethod
    def _import_records(cls, path, format):
        if format == 'json':
            try:
                with path.open('rb') as stream:
                    raw = stream.read(MAX_IMPORT_BYTES + 1)
                if len(raw) > MAX_IMPORT_BYTES:
                    raise ValueError()
                value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=cls._json_object)
            except (ValueError, UnicodeError):
                raise ValueError('Archive import row 1 is not a supported JSON file.') from None
            if isinstance(value, list):
                yield from enumerate(value, 1)
            elif isinstance(value, dict) and isinstance(value.get('entries'), list):
                yield from enumerate(value['entries'], 1)
            elif isinstance(value, dict) and all(_SOURCE_ID.fullmatch(key) and isinstance(text, str) for key, text in value.items()):
                for index, (source_id, text) in enumerate(value.items(), 1):
                    # The official ID->text export contains contributors' plain
                    # textarea input, not HTML. Preserve angle-bracket emoticons
                    # and literal tags; HTMLParser's EOF rules vary by Python.
                    yield index, {'source_entry_id': source_id, 'text': text}
            else:
                raise ValueError('Archive import row 1 has an unsupported JSON structure.')
        elif format == 'jsonl':
            for index, line in enumerate(cls._lines(path), 1):
                if not line.strip():
                    continue
                try:
                    yield index, json.loads(line, object_pairs_hook=cls._json_object)
                except ValueError:
                    raise ValueError(f'Archive import row {index} is not a JSON record.') from None
        else:
            reader = csv.DictReader(cls._lines(path))
            try:
                if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames) or not {'text', 'description'}.intersection(reader.fieldnames):
                    raise ValueError('Archive import row 1 needs CSV headers including text or description.')
                for index, record in enumerate(reader, 2):
                    if None in record or len(record) > 100:
                        raise ValueError(f'Archive import row {index} has invalid CSV columns.')
                    yield index, record
            except csv.Error:
                raise ValueError(f'Archive import row {reader.line_num or 1} is not a valid CSV record.') from None

    def import_file(self, path, format='auto'):
        path = Path(path)
        if not path.is_file() or path.stat().st_size > MAX_IMPORT_BYTES:
            raise ValueError('Use a local archive file no larger than 128 MiB.')
        if format == 'auto':
            format = {'.json': 'json', '.jsonl': 'jsonl', '.ndjson': 'jsonl', '.csv': 'csv'}.get(path.suffix.lower())
        if format not in ('json', 'jsonl', 'csv'):
            raise ValueError('Choose a JSON, JSONL or CSV archive file.')
        with self._lock:
            connection = self._connection
            connection.execute('BEGIN IMMEDIATE')
            added = duplicates = 0
            total = self._count(connection)
            try:
                for index, record in self._import_records(path, format):
                    try:
                        entry = self._import_entry(record)
                        fresh, _ = self._insert(connection, entry, total + added)
                    except (ValueError, TypeError, sqlite3.IntegrityError):
                        raise ValueError(f'Archive import row {index} is invalid or conflicts with a saved entry.') from None
                    added += int(fresh)
                    duplicates += int(not fresh)
                connection.commit()
                return {'added': added, 'duplicates': duplicates, 'total': total + added}
            except Exception:
                connection.rollback()
                raise

    def close(self):
        with self._lock:
            self._connection.close()
