"""Full collection jobs return promptly, report real progress and resume safely."""
import json
from pathlib import Path
import tempfile
import threading
import unittest

from fastapi.testclient import TestClient

from live_voice.app import create_app
from live_voice.archive_database import ArchiveDatabase
from live_voice.collections import CollectionDatabase
from live_voice.museum_jobs import MuseumJobs
from test_collection_integration import Providers


class FullMuseumAPITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); root = Path(self.temporary.name)
        self.art = CollectionDatabase(root/'art'); self.qtm = ArchiveDatabase(root/'qtm')
        self.started = threading.Event(); self.finished = threading.Event()
        self.headers = {'origin':'http://testserver'}
        self.calls = []

    def tearDown(self):
        self.art.close(); self.qtm.close(); self.temporary.cleanup()

    def runner(self,store,source,*,progress,stop):
        self.calls.append(source)
        progress({'current_source':'met','processed':300,'added':290,'duplicates':10,'total_expected':500000})
        self.started.set(); stop.wait(3)
        self.finished.set()
        return {'status':'paused','processed':300,'results':[],'errors':[]}

    def app(self):
        return create_app(preview=True,providers=Providers(),collections_store=self.art,
            archive_store=self.qtm,museum_bulk_runner=self.runner)

    def test_background_progress_stop_and_resume_without_loading_any_models(self):
        with TestClient(self.app()) as client:
            self.assertIsNone(client.get('/api/archive/museums/imports/current').json()['job'])
            response = client.post('/api/archive/museums/import',headers=self.headers,json={'source':'all','scope':'full'})
            self.assertEqual(response.status_code,202)
            self.assertTrue(self.started.wait(1))
            job = client.get('/api/archive/museums/imports/current').json()['job']
            self.assertEqual(job['processed'],300); self.assertEqual(len(job['sources']),5)
            self.assertEqual(job['total_expected'],500000); self.assertEqual(job['status'],'running')
            duplicate = client.post('/api/archive/museums/import',headers=self.headers,json={'source':'all','scope':'full'})
            self.assertEqual(duplicate.status_code,409)
            self.assertEqual(client.post('/api/archive/museums/imports/not-current/stop',headers=self.headers).status_code,404)
            self.assertEqual(client.post('/api/archive/museums/imports/'+job['id']+'/stop').status_code,403)
            self.assertEqual(client.post('/api/archive/museums/imports/'+job['id']+'/stop',headers=self.headers).status_code,200)
            self.assertTrue(self.finished.wait(1))
        job = MuseumJobs(self.art).current()
        self.assertEqual(job['status'],'paused')
        self.assertEqual(job['processed'],300)

    def test_invalid_full_input_does_not_start_downloads(self):
        with TestClient(self.app()) as client:
            self.assertTrue(client.get('/api/archive/museums').json()['full_collection_import'])
            for value in ({'source':'all','scope':'full','url':'https://elsewhere.test'},
                          {'source':'all','scope':'matching'}, {'source':'unknown','scope':'full'},
                          {'source':'met','scope':'full','limit':25},
                          {'source':'met','scope':'full','query':'Monet'}):
                self.assertEqual(client.post('/api/archive/museums/import',headers=self.headers,json=value).status_code,400)
        self.assertEqual(self.calls,[])

    def test_interrupted_running_job_is_paused_when_server_reopens(self):
        path = self.art.directory/'museum-import-job.json'
        path.write_text(json.dumps({'id':'a'*32,'status':'running','sources':['met'],'processed':1200}))
        current = MuseumJobs(self.art).current()
        self.assertEqual(current['status'],'paused'); self.assertEqual(current['processed'],1200)
        self.assertIn('Resume',current['message'])
