"""Useful import diagnoses without printing stories, SQL or filesystem errors."""
from contextlib import redirect_stderr, redirect_stdout
import errno
from io import StringIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts import import_qtm_archive as importer


SECRET = 'CONFIDENTIAL_CONTRIBUTOR_STORY'


def wrapped_sqlite(error):
    try:
        try:
            raise error
        except sqlite3.Error:
            raise ValueError(SECRET) from None
    except ValueError as outer:
        return outer


def sqlite_error(code, text=SECRET):
    error = sqlite3.OperationalError(text)
    error.sqlite_errorcode = code
    error.sqlite_errorname = SECRET
    return error


class SafeDiagnosticTests(unittest.TestCase):
    def test_actual_sqlite_constraint_retains_verified_extended_name_without_values(self):
        connection = sqlite3.connect(':memory:')
        try:
            connection.execute('CREATE TABLE probe(value TEXT UNIQUE)')
            connection.execute('INSERT INTO probe VALUES(?)',(SECRET,))
            try:
                connection.execute('INSERT INTO probe VALUES(?)',(SECRET,))
            except sqlite3.IntegrityError as error:
                report = importer.safe_diagnostic(error,'import_records')
            else:
                self.fail('Expected an actual SQLite uniqueness error.')
            self.assertEqual(report['sqlite_extended_name'],'SQLITE_CONSTRAINT_UNIQUE')
            self.assertNotIn(SECRET,json.dumps(report))
        finally:
            connection.close()

    def test_suppressed_sqlite_context_keeps_only_codes_and_fixed_messages(self):
        for code, reason in ((sqlite3.SQLITE_BUSY, 'sqlite_busy'),
                             (sqlite3.SQLITE_LOCKED, 'sqlite_locked'),
                             (sqlite3.SQLITE_READONLY, 'sqlite_readonly'),
                             (sqlite3.SQLITE_CANTOPEN, 'sqlite_cantopen'),
                             (sqlite3.SQLITE_FULL, 'sqlite_full'),
                             (sqlite3.SQLITE_IOERR | (7 << 8), 'sqlite_io'),
                             (sqlite3.SQLITE_CORRUPT, 'sqlite_corrupt'),
                             (sqlite3.SQLITE_NOTADB, 'sqlite_not_database')):
            error = wrapped_sqlite(sqlite_error(code))
            self.assertTrue(error.__suppress_context__)
            with self.subTest(code=code):
                report = importer.safe_diagnostic(error, 'open_database')
                self.assertEqual(report['reason'], reason)
                self.assertEqual(report['stage'], 'open_database')
                self.assertEqual(report['sqlite_code'], code)
                self.assertEqual(report['sqlite_primary_code'], code & 255)
                self.assertNotIn(SECRET, json.dumps(report))

    def test_exact_fts5_error_is_distinct_from_other_sql_errors(self):
        for text, expected in (('no such module: fts5', 'fts5_unavailable'),
                               ('no such module: fts5 ' + SECRET, 'sqlite_error'),
                               ('no such column: ' + SECRET, 'sqlite_error')):
            with self.subTest(expected=expected):
                report = importer.safe_diagnostic(wrapped_sqlite(sqlite3.OperationalError(text)), 'open_database')
                self.assertEqual(report['reason'], expected)
                self.assertNotIn(SECRET, json.dumps(report))

    def test_os_errors_use_errno_and_do_not_echo_paths_or_messages(self):
        for number, expected in ((errno.EACCES, 'permission_denied'),
                                 (errno.EPERM, 'permission_denied'),
                                 (errno.EROFS, 'readonly_filesystem'),
                                 (errno.ENOSPC, 'storage_full'),
                                 (errno.ENOENT, 'missing_path'),
                                 (errno.ENOTDIR, 'wrong_path_type'),
                                 (errno.EIO, 'filesystem_io'),
                                 (errno.EBUSY, 'filesystem_busy')):
            with self.subTest(errno=number):
                report = importer.safe_diagnostic(OSError(number, SECRET, '/'+SECRET), 'prepare_source')
                self.assertEqual(report['reason'], expected)
                self.assertEqual(report['errno'], number)
                self.assertNotIn(SECRET, json.dumps(report))

    def test_only_exact_owned_validation_messages_and_row_numbers_are_exposed(self):
        cases = (
            ('The existing selected-story archive is invalid; it was not changed.', 'legacy_invalid', None),
            ('The existing selected-story archive is too large to migrate safely.', 'legacy_size_limit', None),
            ('The local archive database has an unsupported schema version.', 'unsupported_schema', None),
            ('Archive import row 321 is invalid or conflicts with a saved entry.', 'invalid_import_row', 321),
            ('Archive import row 1 is not a supported JSON file.', 'invalid_import_row', 1),
            ('Archive import row 321 is invalid or conflicts with a saved entry. '+SECRET, 'import_failed', None),
            ('Archive import row 321 '+SECRET, 'import_failed', None),
            (SECRET, 'import_failed', None),
        )
        for text, expected, row in cases:
            with self.subTest(expected=expected, row=row):
                report = importer.safe_diagnostic(ValueError(text), 'import_records')
                self.assertEqual(report['reason'], expected)
                self.assertEqual(report.get('row'), row)
                self.assertNotIn(SECRET, json.dumps(report))
        class ValidationError(ValueError):
            def __str__(self): raise AssertionError('Pydantic-like details must not be rendered.')
        self.assertEqual(importer.safe_diagnostic(ValidationError(), 'import_records')['reason'], 'import_failed')

    def test_chain_traversal_is_bounded_and_handles_cycles(self):
        root = ValueError(SECRET)
        current = root
        for _ in range(6):
            child = ValueError(SECRET)
            current.__cause__ = child
            current = child
        current.__cause__ = sqlite_error(sqlite3.SQLITE_BUSY)
        self.assertEqual(importer.safe_diagnostic(root, 'open_database')['reason'], 'import_failed')
        root.__cause__ = root
        self.assertEqual(importer.safe_diagnostic(root, SECRET)['stage'], 'unknown')

    def test_download_error_uses_fixed_messages_not_arbitrary_details(self):
        for error, reason in ((importer.DownloadError(SECRET), 'download_failed'),
                              (importer.DownloadError('The official export was not downloaded (HTTP 503). The previous snapshot was kept.'), 'download_http'),
                              (importer.DownloadError('The official export did not return JSON. The previous snapshot was kept.'), 'download_format')):
            with self.subTest(reason=reason):
                report = importer.safe_diagnostic(error, 'prepare_source')
                self.assertEqual(report['reason'], reason)
                self.assertNotIn(SECRET, json.dumps(report))

    def test_download_network_cause_is_not_misclassified_as_local_filesystem(self):
        try:
            try: raise OSError(errno.EIO, SECRET)
            except OSError: raise importer.DownloadError(SECRET) from None
        except importer.DownloadError as error:
            report = importer.safe_diagnostic(error, 'prepare_source')
        self.assertEqual(report['reason'], 'download_failed')
        self.assertNotIn(SECRET, json.dumps(report))

    def test_row_survives_wrapped_sqlite_integrity_failure_without_raw_sql(self):
        failure = sqlite3.IntegrityError(SECRET)
        failure.sqlite_errorcode = sqlite3.SQLITE_CONSTRAINT_UNIQUE
        failure.sqlite_errorname = SECRET
        try:
            try: raise failure
            except sqlite3.Error:
                raise ValueError('Archive import row 42 is invalid or conflicts with a saved entry.') from None
        except ValueError as error:
            report = importer.safe_diagnostic(error, 'import_records')
        self.assertEqual(report['row'], 42)
        self.assertEqual(report['sqlite_code'], sqlite3.SQLITE_CONSTRAINT_UNIQUE)
        self.assertEqual(report['reason'], 'sqlite_error')
        self.assertNotIn(SECRET, json.dumps(report))


class CLIStageTests(unittest.TestCase):
    def invoke(self, factory, *, source_error=None):
        events = []
        def prepare(*_args, **_kwargs):
            events.append('source')
            if source_error: raise source_error
            return Path('/tmp/verified-descriptions.json')
        with redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as error:
            with patch.object(importer, 'download_snapshot', side_effect=prepare):
                status = importer.main(['--download','--directory','/tmp/visible-archive'], database_factory=factory)
        stderr = error.getvalue()
        diagnostic = next((json.loads(line.split(': ', 1)[1]) for line in stderr.splitlines()
                           if line.startswith('Archive import diagnostic: ')), None)
        self.assertNotIn(SECRET, output.getvalue() + stderr)
        return status, output.getvalue(), stderr, diagnostic, events

    def test_prepare_source_failure_has_directory_and_stage_and_never_opens_db(self):
        def forbidden(_directory): raise AssertionError('No database after source failure.')
        result, stdout, _, diagnostic, events = self.invoke(forbidden, source_error=OSError(errno.ENOSPC, SECRET))
        self.assertEqual(result, 1)
        self.assertIn('/tmp/visible-archive', stdout)
        self.assertIn('prepare_source', stdout)
        self.assertEqual(diagnostic['stage'], 'prepare_source')
        self.assertEqual(diagnostic['reason'], 'storage_full')
        self.assertEqual(events, ['source'])

    def test_database_constructor_failure_identifies_open_database_stage(self):
        def failure(_directory): raise wrapped_sqlite(sqlite_error(sqlite3.SQLITE_BUSY))
        result, _, _, diagnostic, _ = self.invoke(failure)
        self.assertEqual(result, 1)
        self.assertEqual(diagnostic['stage'], 'open_database')
        self.assertEqual(diagnostic['reason'], 'sqlite_busy')

    def test_success_reports_all_phases_and_preserves_the_existing_counts(self):
        closed = []
        class Database:
            def __init__(self, _directory): pass
            def import_file(self, *_args, **_kwargs): return {'added':12,'duplicates':3,'total':15}
            def close(self): closed.append(True)
        result, stdout, stderr, diagnostic, _ = self.invoke(Database)
        self.assertEqual(result, 0)
        positions = [stdout.index('stage: '+phase) for phase in ('prepare_source','open_database','import_records','close_database')]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('Imported 12 new stories; 3 duplicates. Database contains 15 stories.', stdout)
        self.assertEqual(closed, [True])
        self.assertEqual(stderr, '')
        self.assertIsNone(diagnostic)

    def test_import_failure_takes_precedence_over_a_second_close_failure(self):
        closed = []
        class Database:
            def __init__(self, _directory): pass
            def import_file(self, *_args, **_kwargs):
                raise ValueError('Archive import row 99 is invalid or conflicts with a saved entry.')
            def close(self):
                closed.append(True)
                raise sqlite_error(sqlite3.SQLITE_IOERR)
        result, stdout, _, diagnostic, _ = self.invoke(Database)
        self.assertEqual(result, 1)
        self.assertEqual(diagnostic['stage'], 'import_records')
        self.assertEqual(diagnostic['reason'], 'invalid_import_row')
        self.assertEqual(diagnostic['row'], 99)
        self.assertIn('stage: close_database', stdout)
        self.assertEqual(closed, [True])

    def test_close_failure_after_success_is_identified_without_a_success_claim(self):
        class Database:
            def __init__(self, _directory): pass
            def import_file(self, *_args, **_kwargs): return {'added':1,'duplicates':0,'total':1}
            def close(self): raise sqlite_error(sqlite3.SQLITE_IOERR)
        result, stdout, _, diagnostic, _ = self.invoke(Database)
        self.assertEqual(result, 1)
        self.assertEqual(diagnostic['stage'], 'close_database')
        self.assertEqual(diagnostic['reason'], 'sqlite_io')
        self.assertNotIn('Imported 1 new stories', stdout)


if __name__ == '__main__':
    unittest.main()
