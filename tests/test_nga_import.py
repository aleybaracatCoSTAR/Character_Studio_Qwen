"""Pinned public metadata ingestion with small synthetic CSVs and mocked HTTP."""
from contextlib import redirect_stderr, redirect_stdout
import csv
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import uuid

import httpx

from live_voice.collections import CollectionDatabase
from scripts import import_nga_archive as importer


OBJECT_COLUMNS = ['objectid','uuid','accessioned','accessionnum','title','attribution',
    'displaydate','medium','dimensions','classification','subclassification','creditline',
    'provenancetext','inscription','markings','watermarks','portfolio','series','volume']
IMAGE_COLUMNS = ['uuid','iiifurl','iiifthumburl','viewtype','sequence','width','height',
                 'maxpixels','openaccess','created','modified','depictstmsobjectid','assistivetext']


def csv_body(columns, records):
    stream = StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    for record in records: writer.writerow({key:record.get(key,'') for key in columns})
    return stream.getvalue().encode('utf-8')


def artwork(id=1, **changes):
    return {'objectid':str(id),'title':'A synthetic café painting — étude', 'attribution':'Invented Artist',
            'displaydate':'about 1900','medium':'oil on canvas','dimensions':'30 × 40 cm',
            'classification':'Painting','creditline':'Synthetic collection credit',
            'accessionnum':f'1900.1.{id}','provenancetext':'Synthetic provenance.\nA second line.',**changes}


def image(object_id=1, suffix=1, **changes):
    image_id=f'00000000-0000-0000-0000-{suffix:012d}'
    base=f'https://api.nga.gov/iiif/{image_id}'
    return {'uuid':image_id,'iiifurl':base,'iiifthumburl':base+'/full/!200,200/0/default.jpg',
            'viewtype':'primary','sequence':'1','openaccess':'1','depictstmsobjectid':str(object_id),
            'assistivetext':'A synthetic painting with blue skies.',**changes}


def blob_spec(data):
    digest=hashlib.sha1(usedforsecurity=False)
    digest.update(f'blob {len(data)}\0'.encode());digest.update(data)
    return {'bytes':len(data),'git_blob_sha1':digest.hexdigest()}


class SourceFixture(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)

    def sources(self, objects, images=()):
        object_path=self.root/'objects.csv';image_path=self.root/'published_images.csv'
        object_path.write_bytes(csv_body(OBJECT_COLUMNS,objects))
        image_path.write_bytes(csv_body(IMAGE_COLUMNS,images))
        return object_path,image_path


class SourceTests(SourceFixture):
    def test_join_keeps_artwork_metadata_and_unicode_without_requiring_an_image(self):
        objects,images=self.sources([artwork(),artwork(2,title='Second work')],[image()])
        counts={};rows=list(importer.iter_records(objects,images,stats=counts))
        self.assertEqual(len(rows),2)
        first,second=rows
        self.assertEqual(first['source_entry_id'],'1')
        self.assertEqual(first['id'],uuid.uuid5(uuid.NAMESPACE_URL,importer.COLLECTION_ID+':1').hex)
        self.assertEqual(first['source_url'],'https://www.nga.gov/collection/art-object-page.1.html')
        self.assertEqual(first['image_url'],image()['iiifthumburl'])
        self.assertEqual(first['metadata']['artist'],'Invented Artist')
        self.assertEqual(first['metadata']['date'],'about 1900')
        self.assertEqual(first['metadata']['credit_line'],'Synthetic collection credit')
        self.assertIn('café',first['text']);self.assertIn('30 × 40 cm',first['text'])
        self.assertIn('A second line.',first['text']);self.assertIn('Selected image: CC0-1.0',first['metadata']['rights'])
        self.assertIsNone(second['image_url']);self.assertEqual(second['image_alt'],'')
        self.assertIn('No open-access image',second['metadata']['rights'])
        self.assertEqual(counts['objects_read'],2);self.assertEqual(counts['images_read'],1)

    def test_restricted_images_are_not_selected_or_relabelled_as_open_access(self):
        restricted=image(suffix=1,openaccess='0')
        alternate=image(suffix=2,viewtype='alternate',sequence='10')
        paths=self.sources([artwork(),artwork(2)],
                           [restricted,alternate,image(2,suffix=3,openaccess='0')])
        rows=list(importer.iter_records(*paths))
        self.assertEqual(rows[0]['image_url'],alternate['iiifthumburl'])
        self.assertIsNone(rows[1]['image_url'])

    def test_real_zero_object_id_joins_its_image_while_only_blank_image_owners_are_unlinked(self):
        paths=self.sources([artwork(0,title='Synthetic zero-ID artwork'),artwork(1)],
                           [image(0),image(depictstmsobjectid=''),image(depictstmsobjectid='  ')])
        counts={};rows=list(importer.iter_records(*paths,stats=counts))
        self.assertEqual(len(rows),2);self.assertEqual(rows[0]['source_entry_id'],'0')
        self.assertEqual(rows[0]['image_url'],image(0)['iiifthumburl'])
        self.assertTrue(rows[0]['source_url'].endswith('.0.html'))
        self.assertEqual(counts['images_unlinked'],2);self.assertEqual(counts['images_read'],3)
        for owner in ('-1','NaN','PRIVATE OWNER','１２'):
            for openaccess in ('0','1'):
                paths=self.sources([artwork()],[image(depictstmsobjectid=owner,openaccess=openaccess)])
                with self.subTest(owner=owner,rights=openaccess),self.assertRaises(importer.NGAImportError) as caught:
                    list(importer.iter_records(*paths))
                self.assertEqual(importer.safe_diagnostic(caught.exception,'import_records'),
                    {'stage':'import_records','reason':'invalid_object_id','file':'published_images.csv','row':2})

    def test_primary_then_numeric_sequence_then_uuid_selection_is_independent_of_input_order(self):
        candidates=[image(suffix=7,sequence='10'),image(suffix=8,sequence='2'),
                    image(suffix=6,sequence='2'),image(suffix=1,viewtype='alternate',sequence='0')]
        expected=image(suffix=6,sequence='2')['iiifthumburl']
        for order in (candidates,list(reversed(candidates)),candidates[2:]+candidates[:2]):
            with self.subTest(order=[row['uuid'] for row in order]):
                self.assertEqual(list(importer.iter_records(*self.sources([artwork()],order)))[0]['image_url'],expected)

    def test_long_fields_are_explicit_excerpts_while_complete_long_title_remains_in_text(self):
        title='A long synthetic title '+('é'*400)
        record=artwork(title=title,attribution='Artist '*300,medium='paint '*300,
                       provenancetext='A documented synthetic history. '*900)
        paths=self.sources([record],[image(assistivetext='A very long synthetic description. '*40)])
        counts={};entry=list(importer.iter_records(*paths,stats=counts))[0]
        self.assertLessEqual(len(entry['title']),240);self.assertIn('[excerpt]',entry['title'])
        self.assertIn(title,entry['text']);self.assertLessEqual(len(entry['text']),12000)
        self.assertTrue(entry['text'].endswith('[excerpt]'))
        self.assertLessEqual(len(entry['image_alt']),500)
        self.assertLessEqual(sum(map(len,entry['metadata'].values())),4000)
        self.assertEqual(counts['titles_shortened'],1);self.assertEqual(counts['text_excerpted'],1)
        self.assertGreater(counts['metadata_excerpted'],0);self.assertEqual(counts['image_alt_shortened'],1)

    def test_declared_markup_becomes_text_and_literal_comparison_symbols_survive(self):
        paths=self.sources([artwork(title='<i>Blue</i> &amp; gold',medium='size <3 mm',
            provenancetext='<p>First.</p><script>PRIVATE SCRIPT</script><p>Second.</p>')])
        entry=list(importer.iter_records(*paths))[0]
        self.assertEqual(entry['title'],'Blue & gold');self.assertEqual(entry['metadata']['medium'],'size <3 mm')
        self.assertNotIn('PRIVATE SCRIPT',entry['text']);self.assertIn('Second.',entry['text'])

    def test_headers_columns_id_duplicates_and_row_bounds_fail_without_echoing_data(self):
        objects,images=self.sources([artwork()])
        for body in (b'objectid,PRIVATE HEADER\n1,PRIVATE ARTWORK\n',
                     csv_body(OBJECT_COLUMNS,[artwork(objectid='PRIVATE ARTWORK')]),
                     csv_body(OBJECT_COLUMNS,[artwork(),artwork()]),
                     csv_body(OBJECT_COLUMNS,[artwork()])+b'1,PRIVATE EXTRA\n'):
            objects.write_bytes(body)
            with self.subTest(body_size=len(body)),self.assertRaises(importer.NGAImportError) as failure:
                list(importer.iter_records(objects,images))
            self.assertNotIn('PRIVATE',str(failure.exception))
        objects,images=self.sources([artwork(),artwork(2)])
        with patch.object(importer,'MAX_OBJECTS',1),self.assertRaises(importer.NGAImportError):
            list(importer.iter_records(objects,images))

    def test_open_access_flags_cannot_authorize_arbitrary_image_urls_or_invalid_guids(self):
        for changes in ({'iiifthumburl':'https://example.invalid/private'},
                        {'iiifurl':'http://127.0.0.1/secret'}, {'uuid':'../../private'},
                        {'openaccess':'true'}):
            paths=self.sources([artwork()],[image(**changes)])
            with self.subTest(changes=list(changes)),self.assertRaises(importer.NGAImportError):
                list(importer.iter_records(*paths))

    def test_real_database_reimport_is_idempotent_searchable_and_a_late_bad_row_rolls_back(self):
        directory=self.root/'archive'
        paths=self.sources([artwork(91)],[image(91)])
        first=importer.import_sources(directory,*paths)
        second=importer.import_sources(directory,*paths)
        self.assertEqual((first['added'],first['duplicates'],first['total']),(1,0,1))
        self.assertEqual((second['added'],second['duplicates'],second['total']),(0,1,1))
        self.assertFalse(first['source_verified']);self.assertIsNone(first['source_revision'])
        self.assertEqual(first['configured_revision'],importer.REVISION);self.assertNotIn('revision',first)
        database=CollectionDatabase(directory)
        try:
            result=database.page([importer.COLLECTION_ID],query='café')
            self.assertEqual(result['matched'],1);self.assertEqual(result['entries'][0]['provenance'],'imported')
            before=database.page([importer.COLLECTION_ID])
        finally:database.close()
        paths=self.sources([artwork(92),artwork(objectid='PRIVATE INVALID ID')])
        with self.assertRaises(ValueError):importer.import_sources(directory,*paths)
        database=CollectionDatabase(directory)
        try:self.assertEqual(database.page([importer.COLLECTION_ID]),before)
        finally:database.close()


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)/'archive'
        self.bodies={'objects.csv':csv_body(OBJECT_COLUMNS,[artwork()]),
                     'published_images.csv':csv_body(IMAGE_COLUMNS,[image()])}
        self.specs={name:blob_spec(body) for name,body in self.bodies.items()}
        self.patch=patch.object(importer,'FILES',self.specs);self.patch.start();self.addCleanup(self.patch.stop)

    def client(self, serve):
        client=httpx.Client(transport=httpx.MockTransport(serve),follow_redirects=True)
        self.addCleanup(client.close);return client

    def success(self, request):
        filename=request.url.path.rsplit('/',1)[-1]
        self.assertIn(filename,self.bodies);self.assertEqual(str(request.url),importer.source_url(filename))
        return httpx.Response(200,content=self.bodies[filename],headers={'Content-Type':'text/plain; charset=utf-8'})

    def test_two_fixed_official_blobs_are_private_verified_cached_and_do_not_download_images(self):
        requests=[]
        def serve(request):requests.append(str(request.url));return self.success(request)
        paths=importer.download_sources(self.root,client=self.client(serve))
        self.assertEqual(len(requests),2)
        self.assertTrue(all('/'+importer.REVISION+'/data/' in url for url in requests))
        for path in paths:
            self.assertEqual(path.parent,self.root/'imports'/'nga'/importer.REVISION)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
            self.assertEqual(path.read_bytes(),self.bodies[path.name])
            metadata=path.with_suffix('.metadata.json')
            self.assertEqual(stat.S_IMODE(metadata.stat().st_mode),0o600)
            info=json.loads(metadata.read_text())
            self.assertEqual(info['git_blob_sha1'],self.specs[path.name]['git_blob_sha1'])
            self.assertEqual(info['sha256'],hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(info['revision'],importer.REVISION);self.assertNotIn('café',metadata.read_text())
        def forbidden(_):raise AssertionError('Verified caches must not make HTTP requests.')
        self.assertEqual(importer.download_sources(self.root,client=self.client(forbidden)),paths)

    def test_only_both_exact_pinned_files_can_report_a_verified_source_revision(self):
        paths=importer.download_sources(self.root,client=self.client(self.success))
        expected={'source_verified':True,'source_revision':importer.REVISION,
                  'configured_revision':importer.REVISION}
        self.assertEqual(importer.source_verification(*paths),expected)
        for index in (0,1):
            original=paths[index].read_bytes()
            paths[index].write_bytes(original.replace(b'1',b'2',1))
            try:
                actual=importer.source_verification(*paths)
                self.assertFalse(actual['source_verified']);self.assertIsNone(actual['source_revision'])
                self.assertEqual(actual['configured_revision'],importer.REVISION)
            finally:paths[index].write_bytes(original)

    def test_cache_metadata_cannot_authorize_tampered_bytes_or_a_different_source(self):
        paths=importer.download_sources(self.root,client=self.client(self.success))
        paths[0].write_bytes(paths[0].read_bytes().replace(b'1900',b'1999',1))
        info_path=paths[0].with_suffix('.metadata.json')
        info=json.loads(info_path.read_text());info['sha256']=hashlib.sha256(paths[0].read_bytes()).hexdigest()
        info['source_url']='https://example.invalid/not-nga';info_path.write_text(json.dumps(info))
        requests=[]
        def serve(request):requests.append(str(request.url));return self.success(request)
        importer.download_sources(self.root,client=self.client(serve))
        self.assertEqual(len(requests),1);self.assertEqual(paths[0].read_bytes(),self.bodies['objects.csv'])
        self.assertEqual(json.loads(info_path.read_text())['source_url'],importer.source_url('objects.csv'))

    def test_redirect_bad_status_content_hash_and_content_length_never_replace_cached_files(self):
        paths=importer.download_sources(self.root,client=self.client(self.success))
        paths[0].write_bytes(b'previous local bytes');old=paths[0].read_bytes()
        original_metadata=paths[0].with_suffix('.metadata.json').read_bytes()
        cases=[httpx.Response(302,headers={'Location':'http://127.0.0.1/private'}),
            httpx.Response(503,text='PRIVATE SERVER ERROR'),
            httpx.Response(200,text='PRIVATE HTML',headers={'Content-Type':'text/html'}),
            httpx.Response(200,text='PRIVATE WRONG CSV',headers={'Content-Type':'text/plain'}),
            httpx.Response(200,content=b'x',headers={'Content-Type':'text/plain','Content-Length':str(importer.MAX_FILE_BYTES+1)})]
        for reply in cases:
            requests=[]
            def serve(request):requests.append(request);return reply
            with self.subTest(status=reply.status_code),self.assertRaises(importer.NGAImportError) as failure:
                importer.download_sources(self.root,client=self.client(serve))
            self.assertEqual(len(requests),1);self.assertNotIn('PRIVATE',str(failure.exception))
            self.assertEqual(paths[0].read_bytes(),old)
            self.assertEqual(paths[0].with_suffix('.metadata.json').read_bytes(),original_metadata)
            self.assertFalse(list(paths[0].parent.glob('.nga-download-*')))

    def test_streaming_and_total_size_limits_stop_before_a_partial_file_is_committed(self):
        class Broken(httpx.SyncByteStream):
            def __iter__(self):
                yield b'partial synthetic CSV\n'
                raise httpx.ReadError('PRIVATE TRANSPORT DETAILS')
        responses=(httpx.Response(200,stream=Broken(),headers={'Content-Type':'text/csv'}),
                   httpx.Response(200,stream=httpx.ByteStream(b'x'*101),headers={'Content-Type':'text/csv'}))
        for reply in responses:
            with self.subTest(stream=type(reply.stream).__name__),patch.object(importer,'MAX_FILE_BYTES',100),self.assertRaises(importer.NGAImportError):
                importer.download_sources(self.root,client=self.client(lambda _:reply))
            cache=self.root/'imports'/'nga'/importer.REVISION
            self.assertFalse((cache/'objects.csv').exists());self.assertFalse(list(cache.glob('.nga-download-*')))
        bound=len(self.bodies['objects.csv'])+len(self.bodies['published_images.csv'])-1
        with patch.object(importer,'MAX_TOTAL_BYTES',bound),self.assertRaises(importer.NGAImportError):
            importer.download_sources(self.root,client=self.client(self.success))
        self.assertFalse((cache/'published_images.csv').exists())

    def test_symbolic_link_cache_is_rejected_before_network(self):
        imports=self.root/'imports';imports.parent.mkdir(parents=True)
        outside=Path(self.temporary.name)/'outside';outside.mkdir();imports.symlink_to(outside,target_is_directory=True)
        def forbidden(_):raise AssertionError('Symlink rejection must precede HTTP.')
        with self.assertRaises(importer.NGAImportError):importer.download_sources(self.root,client=self.client(forbidden))
        self.assertEqual(list(outside.iterdir()),[])


class CLITests(SourceFixture):
    def test_local_cli_and_default_directory_route_without_network_and_print_only_counts(self):
        paths=self.sources([artwork()],[image()]);out,err=StringIO(),StringIO()
        directory=self.root/'chosen archive'
        with patch.dict(os.environ,{'LIVE_ARCHIVE_DIR':str(directory)}),redirect_stdout(out),redirect_stderr(err):
            status=importer.main(['--objects',str(paths[0]),'--images',str(paths[1])])
        self.assertEqual(status,0,err.getvalue());info=json.loads(out.getvalue())
        self.assertEqual(info['added'],1);self.assertEqual(info['total'],1)
        self.assertEqual(info['configured_revision'],importer.REVISION)
        self.assertFalse(info['source_verified']);self.assertIsNone(info['source_revision'])
        self.assertNotIn('revision',info)
        for private in ('café','Invented Artist','Synthetic provenance'):self.assertNotIn(private,out.getvalue()+err.getvalue())
        self.assertTrue(directory.is_dir())

    def test_bad_input_or_database_error_is_safe_and_a_close_failure_does_not_replace_the_primary_failure(self):
        paths=self.sources([artwork()])
        class BrokenDatabase:
            def __init__(self,directory):pass
            def import_records(self,*args,**kwargs):raise ValueError('PRIVATE RECORD')
            def close(self):raise OSError('PRIVATE CLOSE')
        with self.assertRaisesRegex(ValueError,'PRIVATE RECORD'):
            importer.import_sources(self.root,*paths,database_factory=BrokenDatabase)
        out,err=StringIO(),StringIO()
        with redirect_stdout(out),redirect_stderr(err):
            status=importer.main(['--objects',str(paths[0]),'--images',str(paths[1]),'--archive-dir',str(self.root)],
                                 database_factory=BrokenDatabase)
        self.assertEqual(status,1);self.assertEqual(out.getvalue(),'')
        self.assertIn('import_records',err.getvalue());self.assertNotIn('PRIVATE',err.getvalue())

    def test_invalid_artwork_still_fails_with_fixed_file_and_row_diagnostics(self):
        for identifier in ('','-1','PRIVATE ARTWORK'):
            paths=self.sources([artwork(1),artwork(objectid=identifier)])
            out,err=StringIO(),StringIO()
            with redirect_stdout(out),redirect_stderr(err):
                status=importer.main(['--objects',str(paths[0]),'--images',str(paths[1]),
                                      '--archive-dir',str(self.root/'archive')])
            self.assertEqual(status,1);self.assertEqual(out.getvalue(),'')
            self.assertEqual(json.loads(err.getvalue().splitlines()[-1]),
                {'stage':'import_records','reason':'invalid_object_id','file':'objects.csv','row':3})
            self.assertNotIn('PRIVATE',err.getvalue())

    def test_open_and_close_failures_name_the_correct_stage_without_reflecting_private_errors(self):
        paths=self.sources([artwork()])
        class OpenFailure:
            def __init__(self,directory):raise OSError('PRIVATE DATABASE PATH')
        class CloseFailure:
            def __init__(self,directory):pass
            def import_records(self,*args,**kwargs):return {'added':0,'duplicates':0,'total':0,'collection':{}}
            def close(self):raise OSError('PRIVATE CLOSE PATH')
        for factory,stage in ((OpenFailure,'open_database'),(CloseFailure,'close_database')):
            out,err=StringIO(),StringIO()
            with self.subTest(stage=stage),redirect_stdout(out),redirect_stderr(err):
                status=importer.main(['--objects',str(paths[0]),'--images',str(paths[1])],database_factory=factory)
            self.assertEqual(status,1);self.assertEqual(out.getvalue(),'')
            self.assertIn('failed at '+stage,err.getvalue())
            self.assertEqual(json.loads(err.getvalue().splitlines()[-1]),{'stage':stage,'reason':'filesystem_error'})
            self.assertNotIn('PRIVATE',err.getvalue())


if __name__=='__main__':unittest.main()
