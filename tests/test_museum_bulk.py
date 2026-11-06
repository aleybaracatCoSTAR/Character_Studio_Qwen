"""Full imports stream, checkpoint only committed batches, and report real EOF."""
import csv
import fcntl
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx

from live_voice import museum_bulk as bulk
from live_voice.collections import CollectionDatabase
from live_voice.museum_common import MuseumImportError


def met_row(number=1):
    return {'Object ID':str(number), 'Title':f'Met artwork {number}', 'Artist Display Name':'An Artist',
            'Object Date':'1900', 'Is Public Domain':'True', 'Medium':'Oil on canvas',
            'Dimensions':'10 × 20 cm', 'Credit Line':'A gift', 'Object Number':f'1900.{number}',
            'Classification':'Painting'}


def write_csv(path, rows):
    with path.open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)
    return path


class FixtureDownload:
    paths={}
    def __init__(self,source,directory,stop=None,progress=None): self.source=source
    def descriptor(self):
        return {'url':bulk._SNAPSHOTS[self.source][2], 'size':self.paths[self.source].stat().st_size,
                'version':'fixture-v1','sha256':None,'etag':None}
    def ensure(self,descriptor): return self.paths[self.source]
    def close(self): pass


class BulkCoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.root=Path(self.temporary.name)
        self.store=CollectionDatabase(self.root/'database')
        FixtureDownload.paths={'met':write_csv(self.root/'met.csv',[met_row(number) for number in range(1,4)])}
    def tearDown(self):
        self.store.close(); self.temporary.cleanup()

    def run_import(self,source='met',**kwargs):
        with patch.object(bulk,'SnapshotDownload',FixtureDownload),patch.object(bulk,'BATCH_SIZE',2):
            return bulk.run_museum_import(self.store,source,**kwargs)

    def test_complete_requires_real_eof_counts_and_a_repeated_run_does_not_reimport(self):
        progress=[]
        result=self.run_import(progress=progress.append)
        self.assertEqual((result['status'],result['processed'],result['added']),('completed',3,3))
        self.assertEqual(result['total_expected'],3)
        self.assertEqual(self.store.page(['metropolitan-museum-of-art'])['total'],3)
        self.assertTrue(any(event['processed']==2 and event['status']=='running' for event in progress))
        checkpoint=bulk.read_museum_checkpoint(self.store.directory,'met')
        self.assertEqual(checkpoint['status'],'completed'); self.assertIsNone(checkpoint['cursor'])
        repeated=self.run_import()
        self.assertEqual(repeated['processed'],3)
        self.assertEqual(self.store.count(),3)

    def test_pause_after_commit_and_resume_skip_committed_rows(self):
        stop=threading.Event()
        def progress(event):
            if event['processed']==2: stop.set()
        paused=self.run_import(progress=progress,stop=stop)
        self.assertEqual(paused['status'],'paused')
        self.assertEqual(self.store.count(),2)
        checkpoint=bulk.read_museum_checkpoint(self.store.directory,'met')
        self.assertEqual(checkpoint['cursor']['processed'],2)
        resumed=self.run_import()
        self.assertEqual((resumed['status'],resumed['processed'],resumed['added']),('completed',3,3))
        self.assertEqual(resumed['duplicates'],0)

    def test_replaying_a_committed_batch_after_a_lost_checkpoint_is_idempotent(self):
        first=bulk._met_record(met_row())
        self.store.import_records([first],collection_id='metropolitan-museum-of-art',label='The Metropolitan Museum of Art',
                                  source_kind='museum-met',update_existing=True)
        result=self.run_import()
        self.assertEqual(result['status'],'completed')
        self.assertEqual((result['processed'],result['added'],result['duplicates']),(3,2,1))
        self.assertEqual(self.store.count(),3)

    def test_premature_terminal_count_never_commits_or_marks_complete(self):
        pages=[([bulk._met_record(met_row())],None,5)]
        with patch.object(bulk,'snapshot_pages',return_value=iter(pages)):
            result=self.run_import()
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['errors'][0]['reason'],'incomplete_catalogue')
        self.assertEqual(self.store.count(),0)
        self.assertEqual(bulk.read_museum_checkpoint(self.store.directory,'met')['processed'],0)

    def test_one_source_error_does_not_claim_success_or_skip_the_next_source(self):
        FixtureDownload.paths['cleveland']=self.root/'unused.csv'
        FixtureDownload.paths['cleveland'].write_text('unused')
        def pages(source,*args,**kwargs):
            if source=='met': raise MuseumImportError('met','unavailable')
            yield [],None,0
        with patch.object(bulk,'snapshot_pages',side_effect=pages): result=self.run_import(['met','cleveland'])
        self.assertEqual(result['status'],'failed')
        self.assertEqual([item['status'] for item in result['results']],['failed','completed'])
        self.assertEqual([item['source'] for item in result['errors']],['met'])

    def test_a_second_cli_or_server_worker_cannot_hold_the_same_import_lock(self):
        directory=self.store.directory/'museum-imports'
        directory.mkdir()
        with (directory/'full-import.lock').open('a+b') as held:
            fcntl.flock(held.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(MuseumImportError) as error: self.run_import()
            self.assertEqual(error.exception.reason,'import_busy')
        self.assertEqual(self.store.count(),0)


class SnapshotParserTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(); self.root=Path(self.temporary.name)
    def tearDown(self): self.temporary.cleanup()

    def test_met_csv_has_full_metadata_but_never_invents_image_urls(self):
        row=met_row(); row['Title']='A title\nwith a newline'
        path=write_csv(self.root/'met.csv',[row])
        records,cursor,total=list(bulk.snapshot_pages('met',path,'v1'))[0]
        self.assertEqual((cursor,total),(None,1))
        self.assertIsNone(records[0]['image_url'])
        self.assertIn('image availability is checked on request',records[0]['metadata']['rights'])
        self.assertEqual(records[0]['source_entry_id'],'1')
        self.assertEqual(records[0]['title'],'A title with a newline')

    def test_cleveland_flat_csv_links_images_only_for_exact_cc0(self):
        rows=[{'id':str(number),'title':'Garden','url':f'https://www.clevelandart.org/art/1900.{number}',
               'share_license_status':license,'image_web':f'https://openaccess-cdn.clevelandart.org/1900.{number}/web.jpg',
               'creators':'An Artist, artist','technique':'Oil on canvas','culture':'French'}
              for number,license in ((1,'CC0'),(2,'Copyrighted'))]
        records,_,total=list(bulk.snapshot_pages('cleveland',write_csv(self.root/'cma.csv',rows),'v1'))[0]
        self.assertEqual(total,2)
        self.assertTrue(records[0]['image_url']); self.assertIsNone(records[1]['image_url'])
        self.assertEqual(records[0]['metadata']['artist'],'An Artist, artist')

    def test_chicago_tar_is_read_without_extracting_and_checks_record_identity(self):
        path=self.root/'aic.tar.bz2'
        value={'id':123,'title':'An Artwork','artist_display':'An Artist','image_id':'00000000-0000-0000-0000-000000000001',
               'is_public_domain':True,'medium_display':'Oil on canvas'}
        with tarfile.open(path,'w:bz2') as archive:
            raw=json.dumps({'data':value}).encode()
            member=tarfile.TarInfo('snapshot/json/artworks/123.json'); member.size=len(raw)
            archive.addfile(member,io.BytesIO(raw))
            ignored=tarfile.TarInfo('snapshot/json/agents/1.json'); ignored.size=2
            archive.addfile(ignored,io.BytesIO(b'{}'))
        records,_,total=list(bulk.snapshot_pages('aic',path,'v1'))[0]
        self.assertEqual(total,1); self.assertEqual(records[0]['source_entry_id'],'123')
        self.assertIn('/iiif/2/',records[0]['image_url'])
        self.assertFalse((self.root/'snapshot').exists())
        value['id']=999
        with tarfile.open(path,'w:bz2') as archive:
            raw=json.dumps(value).encode(); member=tarfile.TarInfo('json/artworks/123.json'); member.size=len(raw)
            archive.addfile(member,io.BytesIO(raw))
        with self.assertRaises(MuseumImportError): list(bulk.snapshot_pages('aic',path,'v1'))

    def test_snapshot_cursor_is_revision_bound_and_eof_cannot_truncate_a_resume(self):
        path=write_csv(self.root/'met.csv',[met_row()])
        cursor={'v':1,'source':'met','revision':'other','processed':1}
        with self.assertRaises(MuseumImportError): list(bulk.snapshot_pages('met',path,'v1',cursor))
        cursor.update(revision='v1',processed=2)
        with self.assertRaises(MuseumImportError): list(bulk.snapshot_pages('met',path,'v1',cursor))


class DownloadAndTransportTests(unittest.TestCase):
    def test_stop_during_the_first_read_timeout_does_not_retry_or_turn_into_a_failure(self):
        for mode in ('json','xml'):
            with self.subTest(mode=mode):
                stop=threading.Event(); calls=[]
                def timeout(request):
                    calls.append(request); stop.set()
                    raise httpx.ReadTimeout('Synthetic stalled response',request=request)
                fetch=bulk.FullHTTP('rijksmuseum',stop)
                fetch.transport.client.close(); fetch.transport.client=httpx.Client(transport=httpx.MockTransport(timeout))
                try:
                    with self.assertRaises(bulk.BulkStopped):
                        if mode=='json': fetch('https://data.rijksmuseum.nl/search/collection')
                        else: fetch.xml('https://data.rijksmuseum.nl/oai')
                    self.assertEqual(len(calls),1)
                    self.assertEqual(fetch.requests,1)
                finally: fetch.close()

    def test_snapshot_range_resume_validates_length_and_sha256_without_buffering_asset(self):
        body=b'official metadata fixture\n'*100
        digest=hashlib.sha256(body).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/'met.csv.part').write_bytes(body[:20])
            seen=[]
            def handler(request):
                seen.append(request)
                self.assertEqual(request.headers['range'],'bytes=20-')
                return httpx.Response(206,headers={'content-range':f'bytes 20-{len(body)-1}/{len(body)}'},content=body[20:])
            download=bulk.SnapshotDownload('met',root)
            download.client.close(); download.client=httpx.Client(transport=httpx.MockTransport(handler))
            try:
                path=download.ensure({'url':bulk._SNAPSHOTS['met'][2],'size':len(body),'sha256':digest,'etag':None,'version':'sha256:'+digest})
                self.assertEqual(path.read_bytes(),body); self.assertFalse((root/'met.csv.part').exists())
                self.assertEqual(len(seen),1)
            finally: download.close()

    def test_partial_response_or_arbitrary_download_url_cannot_become_a_valid_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            download=bulk.SnapshotDownload('met',directory)
            download.client.close()
            download.client=httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(200,content=b'partial')))
            try:
                descriptor={'url':bulk._SNAPSHOTS['met'][2],'size':50,'sha256':None,'etag':None,'version':'test'}
                with self.assertRaises(MuseumImportError): download.ensure(descriptor)
                self.assertFalse((Path(directory)/'met.csv').exists())
                with self.assertRaises(MuseumImportError): download.ensure({**descriptor,'url':'https://127.0.0.1/private'})
            finally: download.close()

    def test_full_transport_keeps_request_guards_and_supports_bounded_rijks_xml(self):
        def handler(request):
            if request.url.path=='/oai': return httpx.Response(200,headers={'content-type':'application/xml'},content=b'<OAI-PMH/>')
            return httpx.Response(200,headers={'content-type':'application/json'},json={'data':'ok'})
        fetch=bulk.FullHTTP('rijksmuseum')
        fetch.transport.client.close(); fetch.transport.client=httpx.Client(transport=httpx.MockTransport(handler))
        fetch.transport.sleep=lambda seconds:None
        try:
            self.assertEqual(fetch('https://data.rijksmuseum.nl/search/collection'),{'data':'ok'})
            self.assertEqual(fetch.xml('https://data.rijksmuseum.nl/oai'),b'<OAI-PMH/>')
            self.assertEqual(fetch.requests,2)
            with self.assertRaises(MuseumImportError): fetch.xml('https://evil.example/oai')
            with self.assertRaises(MuseumImportError): fetch('https://127.0.0.1/private')
        finally: fetch.close()


if __name__=='__main__': unittest.main()
