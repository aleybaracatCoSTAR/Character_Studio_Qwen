"""Cancellation must not abandon archive transactions or snapshot resources."""
import asyncio
from pathlib import Path
import tempfile
import threading
import unittest

import httpx

from live_voice.app import _archive_job, create_app
from live_voice.archive_database import ArchiveDatabase


async def event_ready(event):
    for _ in range(1000):
        if event.is_set(): return
        await asyncio.sleep(.001)
    raise AssertionError('Archive test worker did not reach its expected boundary.')


class ArchiveJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_result_kwargs_and_worker_exception_are_preserved(self):
        caller_thread = threading.get_ident()
        def work(value, *, suffix):
            self.assertNotEqual(threading.get_ident(), caller_thread)
            return value + suffix
        self.assertEqual(await _archive_job(work, 'value', suffix='!'), 'value!')
        def fail(): raise ValueError('synthetic failure')
        with self.assertRaisesRegex(ValueError, 'synthetic failure'):
            await _archive_job(fail)

    async def test_repeated_cancel_holds_lock_and_upload_until_worker_finishes(self):
        started, release = threading.Event(), threading.Event()
        lock, cleanup, read = asyncio.Lock(), [], []
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'upload.json'; path.write_text('synthetic fixture')
            def work():
                started.set(); release.wait(3)
                read.append(path.read_text())
            async def route():
                async with lock:
                    try: await _archive_job(work)
                    finally: path.unlink(); cleanup.append(True)
            task = asyncio.create_task(route())
            try:
                await event_ready(started)
                task.cancel(); await asyncio.sleep(.005)
                task.cancel(); await asyncio.sleep(.005)
                self.assertTrue(lock.locked())
                self.assertTrue(path.exists())
                self.assertEqual(cleanup, [])
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError): await task
            self.assertEqual(read, ['synthetic fixture'])
            self.assertEqual(cleanup, [True])
            self.assertFalse(lock.locked())

    async def test_worker_failure_after_cancel_does_not_replace_cancellation(self):
        started, release = threading.Event(), threading.Event()
        def fail():
            started.set(); release.wait(3)
            raise RuntimeError('synthetic worker failure')
        task = asyncio.create_task(_archive_job(fail))
        await event_ready(started)
        task.cancel(); await asyncio.sleep(.005)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError): await task

    async def test_cancelled_snapshot_is_disposed_once_even_when_cancelled_during_disposal(self):
        started, release = threading.Event(), threading.Event()
        disposing, dispose_release = threading.Event(), threading.Event()
        closed = []
        class Snapshot:
            def close(self):
                disposing.set(); dispose_release.wait(3); closed.append(True)
        snapshot = Snapshot()
        def make():
            started.set(); release.wait(3); return snapshot
        task = asyncio.create_task(_archive_job(make, dispose=lambda result:result.close()))
        try:
            await event_ready(started); task.cancel(); await asyncio.sleep(.005)
            release.set(); await event_ready(disposing)
            task.cancel(); await asyncio.sleep(.005)
            self.assertFalse(task.done())
            self.assertEqual(closed, [])
        finally:
            release.set(); dispose_release.set()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(closed, [True])

    async def test_successful_snapshot_remains_owned_by_caller(self):
        disposed = []
        result = object()
        self.assertIs(await _archive_job(lambda:result, dispose=lambda value:disposed.append(value)), result)
        self.assertEqual(disposed, [])

    async def test_import_route_cancellation_keeps_tempfile_until_transaction_is_done(self):
        started, release = threading.Event(), threading.Event()
        paths = []
        class BlockingDatabase(ArchiveDatabase):
            def import_file(self, path, format='auto'):
                paths.append(Path(path)); started.set(); release.wait(3)
                return super().import_file(path, format=format)
        with tempfile.TemporaryDirectory() as folder:
            store = BlockingDatabase(folder)
            try:
                app = create_app(preview=True, providers=object(), archive_store=store)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
                    task = asyncio.create_task(client.post('/api/archive/import',
                        headers={'Origin':'http://testserver','Content-Type':'application/octet-stream',
                                 'X-Archive-Filename':'stories.json'},
                        content=b'{"123":"Synthetic library words."}'))
                    try:
                        await event_ready(started)
                        task.cancel(); await asyncio.sleep(.01)
                        self.assertFalse(task.done())
                        self.assertTrue(paths[0].exists())
                    finally:
                        release.set()
                        with self.assertRaises(asyncio.CancelledError): await task
                self.assertFalse(paths[0].exists())
                self.assertEqual(store.count(), 1)  # The complete transaction finishes before cleanup.
            finally:
                release.set(); store.close()


if __name__ == '__main__':
    unittest.main()
