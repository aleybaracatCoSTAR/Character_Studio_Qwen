"""Resumable full museum metadata imports, separate from bounded topic searches.

Official snapshot files are streamed to disk and parsed in batches. Only image
URLs are stored. Page cursors advance after database commits; replaying a batch
after a crash is safe because trusted imports update stable source identities.
"""
from __future__ import annotations

import csv
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import tarfile
import tempfile
import threading
import time

import httpx

from .museum_common import MuseumImportError, SOURCES, FULL_MUSEUM_ORDER, museum_record
from .museum_import import MuseumHTTP
from . import museum_cleveland


BATCH_SIZE = 500
MAX_ASSET_BYTES = 2 * 1024 * 1024 * 1024
MAX_RECORD_BYTES = 8 * 1024 * 1024
MAX_EXPANDED_BYTES = 32 * 1024 * 1024 * 1024
MAX_SOURCE_RECORDS = 10_000_000
_SNAPSHOTS = {
    'met':('met.csv', 'https://raw.githubusercontent.com/metmuseum/openaccess/master/MetObjects.csv',
           'https://media.githubusercontent.com/media/metmuseum/openaccess/master/MetObjects.csv'),
    'cleveland':('cleveland.csv', 'https://raw.githubusercontent.com/ClevelandMuseumArt/openaccess/master/data.csv',
                 'https://media.githubusercontent.com/media/ClevelandMuseumArt/openaccess/master/data.csv'),
    'aic':('aic.tar.bz2', None, 'https://artic-api-data.s3.amazonaws.com/artic-api-data.tar.bz2'),
}
_STATS = ('processed', 'added', 'updated', 'duplicates', 'images')
_MESSAGES = {
    'checkpoint_invalid':'The saved import checkpoint is invalid. Restart this source with --refresh.',
    'asset_changed':'The museum snapshot changed. Restart this source with --refresh.',
    'invalid_response':'The official dataset could not be read completely; its saved batches can be resumed.',
    'source_changed':'The live catalogue changed during enumeration. Resume or restart this source.',
    'incomplete_catalogue':'The source ended before its published record count was reached.',
    'storage_full':'The local metadata database is full.',
    'storage_error':'A metadata batch could not be saved.',
    'rate_limited':'The museum is busy; resume this import later.',
    'timeout':'The museum request timed out; resume this import later.',
    'unavailable':'The official source is unavailable; resume this import later.',
}


class BulkStopped(RuntimeError):
    pass


def _check_stop(stop):
    if stop is not None and stop.is_set(): raise BulkStopped()


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(',', ':'))
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def _fresh(source):
    return {'v':1, 'source':source, 'status':'queued', 'message':'Queued.',
            **dict.fromkeys(_STATS, 0), 'total_expected':None, 'cursor':None, 'asset':None}


def read_museum_checkpoint(directory, source):
    if source not in SOURCES: raise MuseumImportError(source, 'invalid_input')
    path = Path(directory) / 'museum-imports' / (source + '.json')
    if not path.exists(): return _fresh(source)
    try:
        if path.stat().st_size > 64 * 1024: raise ValueError()
        state = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(state, dict) or state.get('v') != 1 or state.get('source') != source
                or any(type(state.get(key)) is not int or not 0 <= state[key] <= MAX_SOURCE_RECORDS for key in _STATS)
                or state.get('status') not in ('queued', 'running', 'paused', 'failed', 'completed')
                or (state.get('total_expected') is not None and
                    (type(state['total_expected']) is not int or not 0 <= state['total_expected'] <= MAX_SOURCE_RECORDS))):
            raise ValueError()
        if state['status'] == 'running':
            state['status'] = 'paused'; state['message'] = 'Previous import stopped; ready to resume.'
        return state
    except (ValueError, OSError, UnicodeError):
        raise MuseumImportError(source, 'checkpoint_invalid') from None


class FullJSON:
    """Keep official JSON guards/retries, without a ten-minute whole-job cap."""
    def __init__(self, source, stop=None):
        self.source, self.stop = source, stop
        self.requests = 0
        def sleep(seconds):
            if stop is not None:
                if stop.wait(seconds): raise BulkStopped()
            else: time.sleep(seconds)
        self.transport = MuseumHTTP(source, sleep=sleep, check_stop=lambda:_check_stop(self.stop))

    def __call__(self, url, params=None, headers=None):
        _check_stop(self.stop)
        self.transport.started = self.transport.clock()
        self.transport.requests = self.transport.bytes = 0
        try:
            result = self.transport(url, params=params, headers=headers)
            _check_stop(self.stop)
            return result
        finally:
            self.requests += self.transport.requests

    def close(self): self.transport.close()

    def xml(self, url, params=None, headers=None):
        if self.source!='rijksmuseum' or url!='https://data.rijksmuseum.nl/oai':
            raise MuseumImportError(self.source,'invalid_link')
        for attempt in range(3):
            _check_stop(self.stop)
            if self.transport.last_request is not None:
                remaining=.2-(self.transport.clock()-self.transport.last_request)
                if remaining>0: self.transport.sleep(remaining)
            self.transport.last_request=self.transport.clock(); self.requests+=1
            try:
                with self.transport.client.stream('GET',url,params=params,headers={
                    'Accept':'application/xml','User-Agent':'CharacterStudioBulkCatalogue/1.0'},
                    follow_redirects=False) as response:
                    if response.status_code in (429,500,502,503,504):
                        if attempt==2: raise MuseumImportError(self.source,'rate_limited' if response.status_code==429 else 'unavailable')
                        retry=response.headers.get('retry-after','')
                        delay=min(int(retry),30) if re.fullmatch(r'[0-9]{1,5}',retry) else 1+attempt
                        self.transport.sleep(delay); continue
                    if response.status_code!=200: raise MuseumImportError(self.source,'unavailable')
                    content_type=response.headers.get('content-type','').split(';',1)[0].strip().lower()
                    if content_type not in ('application/xml','text/xml'): raise MuseumImportError(self.source,'invalid_response')
                    body=bytearray()
                    for chunk in response.iter_bytes():
                        _check_stop(self.stop)
                        if len(body)+len(chunk)>MAX_RECORD_BYTES: raise MuseumImportError(self.source,'invalid_response')
                        body.extend(chunk)
                    return bytes(body)
            except httpx.TimeoutException:
                _check_stop(self.stop)
                if attempt==2: raise MuseumImportError(self.source,'timeout') from None
            except httpx.HTTPError:
                _check_stop(self.stop)
                raise MuseumImportError(self.source,'unavailable') from None
        raise MuseumImportError(self.source,'unavailable')


FullHTTP = FullJSON


class SnapshotDownload:
    """Only fixed official metadata assets; resumable downloads and pinned hashes."""
    def __init__(self, source, directory, stop=None, progress=None):
        self.source, self.directory, self.stop, self.progress = source, Path(directory), stop, progress
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.client = httpx.Client(timeout=httpx.Timeout(60, connect=15), follow_redirects=False, trust_env=False)

    def close(self): self.client.close()

    def _request(self, method, url, headers=None):
        allowed = set(value for spec in _SNAPSHOTS.values() for value in spec[1:] if value)
        if url not in allowed: raise MuseumImportError(self.source, 'invalid_link')
        for attempt in range(4):
            _check_stop(self.stop)
            response = None
            try:
                request = self.client.build_request(method, url, headers={
                    'User-Agent':'CharacterStudioBulkCatalogue/1.0', 'Accept-Encoding':'identity', **(headers or {})})
                response = self.client.send(request, stream=True, follow_redirects=False)
                if response.status_code in (429, 500, 502, 503, 504):
                    value = response.headers.get('retry-after', '')
                    delay = min(int(value), 30) if re.fullmatch(r'[0-9]{1,5}', value) else min(2 ** attempt, 10)
                    response.close()
                    if attempt == 3: raise MuseumImportError(self.source, 'rate_limited' if response.status_code == 429 else 'unavailable')
                    if self.stop is not None:
                        if self.stop.wait(delay): raise BulkStopped()
                    else: time.sleep(delay)
                    continue
                if response.status_code == 412:
                    response.close(); raise MuseumImportError(self.source, 'asset_changed')
                if response.status_code not in (200, 206):
                    response.close(); raise MuseumImportError(self.source, 'unavailable')
                return response
            except httpx.HTTPError as error:
                _check_stop(self.stop)
                if response is not None: response.close()
                if attempt == 3:
                    raise MuseumImportError(self.source, 'timeout' if isinstance(error,httpx.TimeoutException) else 'unavailable') from None
        raise MuseumImportError(self.source, 'unavailable')

    def descriptor(self):
        name, pointer, url = _SNAPSHOTS[self.source]
        if pointer:
            response = self._request('GET', pointer)
            try:
                raw = bytearray()
                for chunk in response.iter_bytes():
                    if len(raw) + len(chunk) > 4096: raise MuseumImportError(self.source, 'invalid_response')
                    raw.extend(chunk)
                match = re.fullmatch(rb'version https://git-lfs.github.com/spec/v1\noid sha256:([a-f0-9]{64})\nsize ([0-9]+)\n?', bytes(raw))
                if not match: raise MuseumImportError(self.source, 'invalid_response')
                size, digest = int(match[2]), match[1].decode()
                descriptor = {'url':url, 'size':size, 'sha256':digest, 'etag':None, 'version':'sha256:' + digest}
            finally: response.close()
        else:
            response = self._request('HEAD', url)
            try:
                size = int(response.headers.get('content-length', '0'))
                etag = response.headers.get('etag', '')
                if not etag or len(etag) > 256 or any(ord(c)<32 for c in etag):
                    raise MuseumImportError(self.source, 'invalid_response')
                descriptor = {'url':url, 'size':size, 'sha256':None, 'etag':etag,
                              'version':'etag:' + etag, 'last_modified':response.headers.get('last-modified','')}
            finally: response.close()
        if not 0 < descriptor['size'] <= MAX_ASSET_BYTES: raise MuseumImportError(self.source, 'invalid_response')
        return descriptor

    def _hash(self, path):
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda:stream.read(4 * 1024 * 1024), b''):
                _check_stop(self.stop); digest.update(chunk)
        return digest.hexdigest()

    def ensure(self, descriptor):
        _, _, official = _SNAPSHOTS[self.source]
        if (not isinstance(descriptor,dict) or descriptor.get('url') != official
                or type(descriptor.get('size')) is not int or not 0 < descriptor['size'] <= MAX_ASSET_BYTES
                or not isinstance(descriptor.get('version'),str)):
            raise MuseumImportError(self.source, 'checkpoint_invalid')
        expected = descriptor.get('sha256')
        if expected is not None and (not isinstance(expected,str) or not re.fullmatch(r'[a-f0-9]{64}',expected)):
            raise MuseumImportError(self.source, 'checkpoint_invalid')
        path = self.directory / _SNAPSHOTS[self.source][0]
        if path.is_file() and path.stat().st_size == descriptor['size']:
            actual = self._hash(path)
            if expected is None or actual == expected:
                descriptor['sha256'] = actual
                return path
            raise MuseumImportError(self.source, 'asset_changed')
        partial = path.with_name(path.name + '.part')
        for attempt in range(4):
            _check_stop(self.stop)
            offset = partial.stat().st_size if partial.exists() else 0
            if offset > descriptor['size']: raise MuseumImportError(self.source, 'asset_changed')
            headers = {'Range':f'bytes={offset}-'} if offset else {}
            if descriptor.get('etag'): headers['If-Match'] = descriptor['etag']
            response = self._request('GET', descriptor['url'], headers)
            try:
                mode = 'wb'
                if response.status_code == 206:
                    span = re.fullmatch(r'bytes ([0-9]+)-([0-9]+)/([0-9]+)', response.headers.get('content-range',''))
                    if not span or int(span[1]) != offset or int(span[3]) != descriptor['size']:
                        raise MuseumImportError(self.source, 'invalid_response')
                    mode = 'ab' if offset else 'wb'
                with partial.open(mode) as stream:
                    last_notice = 0.0
                    for chunk in response.iter_bytes(1024 * 1024):
                        _check_stop(self.stop)
                        stream.write(chunk)
                        if stream.tell() > descriptor['size']: raise MuseumImportError(self.source, 'invalid_response')
                        now = time.monotonic()
                        if self.progress and now-last_notice >= .75:
                            self.progress(stream.tell(),descriptor['size']); last_notice = now
                    stream.flush(); os.fsync(stream.fileno())
                if partial.stat().st_size != descriptor['size']:
                    if attempt < 3: continue
                    raise MuseumImportError(self.source, 'invalid_response')
                actual = self._hash(partial)
                if expected and actual != expected: raise MuseumImportError(self.source, 'asset_changed')
                descriptor['sha256'] = actual
                os.replace(partial, path)
                path.chmod(0o600)
                return path
            except httpx.HTTPError:
                _check_stop(self.stop)
                if attempt == 3: raise MuseumImportError(self.source, 'unavailable') from None
            finally: response.close()
        raise MuseumImportError(self.source, 'unavailable')


def _met_record(row):
    identity = row.get('Object ID', '')
    if not re.fullmatch(r'[0-9]{1,12}', identity): raise MuseumImportError('met','invalid_response')
    metadata = {key:row.get(column,'') for key,column in (
        ('artist','Artist Display Name'),('date','Object Date'),('medium','Medium'),('dimensions','Dimensions'),
        ('credit_line','Credit Line'),('accession_number','Object Number'),('classification','Classification'))}
    metadata['rights'] = ('Catalogue metadata: CC0. The CSV contains no image URLs; '
        'image availability is checked on request. Source public-domain flag: ' + row.get('Is Public Domain','unknown') + '.')
    details = '\n'.join(label + ': ' + row[column] for column,label in (
        ('Object Name','Object type'),('Department','Department'),('Culture','Culture'),('Period','Period'),
        ('Dynasty','Dynasty'),('Reign','Reign'),('Portfolio','Portfolio'),('Artist Display Bio','Artist biography'),
        ('Tags','Subject tags')) if row.get(column))
    return museum_record('met',int(identity),row.get('Title'),
        'https://www.metmuseum.org/art/collection/search/' + str(int(identity)),metadata=metadata,description=details)


def _cleveland_record(row):
    identity = row.get('id','')
    if not re.fullmatch(r'[0-9]{1,12}', identity): raise MuseumImportError('cleveland','invalid_response')
    value = dict(row)
    value.update(id=int(identity), creators=[{'description':row['creators']}] if row.get('creators') else [],
                 images={'web':{'url':row.get('image_web','')}}, culture=[row['culture']] if row.get('culture') else [])
    return museum_cleveland._record(value)


def _aic_record(value, identity):
    if not isinstance(value,dict) or type(value.get('id')) is not int or value['id'] != identity:
        raise MuseumImportError('aic','invalid_response')
    image_id = value.get('image_id')
    eligible = value.get('is_public_domain') is True
    image = ('https://www.artic.edu/iiif/2/' + image_id + '/full/400,/0/default.jpg'
             if eligible and isinstance(image_id,str) and re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',image_id) else None)
    thumbnail = value.get('thumbnail')
    return museum_record('aic',identity,value.get('title'),'https://www.artic.edu/artworks/' + str(identity),
        image_url=image,image_alt=thumbnail.get('alt_text','') if isinstance(thumbnail,dict) else '',
        metadata={key:value.get(column) for key,column in (
            ('artist','artist_display'),('date','date_display'),('medium','medium_display'),('dimensions','dimensions'),
            ('credit_line','credit_line'),('accession_number','main_reference_number'),('classification','artwork_type_title'))} |
            {'rights':'Catalogue metadata: CC0. Image: public domain (museum flag).' if eligible
                      else 'Catalogue metadata: CC0. No eligible image supplied.'})


def _snapshot_records(source, path, stop):
    if source in ('met','cleveland'):
        required = {'Object ID','Title'} if source=='met' else {'id','title','url','share_license_status','image_web'}
        with path.open('r',encoding='utf-8-sig',newline='') as stream:
            reader = csv.DictReader(stream)
            if not reader.fieldnames or not required.issubset(reader.fieldnames) or len(reader.fieldnames)!=len(set(reader.fieldnames)):
                raise MuseumImportError(source,'invalid_response')
            for row in reader:
                _check_stop(stop)
                if None in row or any(value is None for value in row.values()): raise MuseumImportError(source,'invalid_response')
                yield row
    else:
        expanded = 0
        with tarfile.open(path, mode='r|bz2') as archive:
            for member in archive:
                _check_stop(stop)
                expanded += max(member.size,0)
                if expanded > MAX_EXPANDED_BYTES: raise MuseumImportError(source,'invalid_response')
                parts = PurePosixPath(member.name).parts
                if len(parts)<3 or parts[-3:-1] != ('json','artworks') or not member.isfile(): continue
                match = re.fullmatch(r'([0-9]{1,12})\.json', parts[-1])
                if not match or '..' in parts or not 0 < member.size <= MAX_RECORD_BYTES:
                    raise MuseumImportError(source,'invalid_response')
                file = archive.extractfile(member)
                if file is None: raise MuseumImportError(source,'invalid_response')
                with file: value = json.loads(file.read(MAX_RECORD_BYTES+1))
                if isinstance(value,dict) and isinstance(value.get('data'),dict): value = value['data']
                yield (int(match[1]),value)


def snapshot_pages(source, path, revision, cursor=None, stop=None):
    if source not in _SNAPSHOTS: raise MuseumImportError(source,'invalid_input')
    offset = 0
    if cursor is not None:
        if (not isinstance(cursor,dict) or set(cursor)!={'v','source','revision','processed'} or cursor.get('v')!=1
                or cursor.get('source')!=source or cursor.get('revision')!=revision
                or type(cursor.get('processed')) is not int or not 0 <= cursor['processed'] <= MAX_SOURCE_RECORDS):
            raise MuseumImportError(source,'invalid_cursor')
        offset = cursor['processed']
    processed, batch = 0, []
    try:
        for raw in _snapshot_records(source,Path(path),stop):
            processed += 1
            if processed > MAX_SOURCE_RECORDS: raise MuseumImportError(source,'invalid_response')
            if processed <= offset: continue
            value = (_met_record(raw) if source=='met' else _cleveland_record(raw) if source=='cleveland'
                     else _aic_record(raw[1],raw[0]))
            batch.append(value)
            if len(batch) >= BATCH_SIZE:
                yield batch, {'v':1,'source':source,'revision':revision,'processed':processed}, None
                batch = []
        if processed < offset: raise MuseumImportError(source,'source_changed')
        yield batch, None, processed
    except (csv.Error, tarfile.TarError, EOFError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        raise MuseumImportError(source,'invalid_response') from None


def _source_ids(source):
    values = list(FULL_MUSEUM_ORDER) if source=='all' else [source] if isinstance(source,str) else source
    if (not isinstance(values,(list,tuple)) or not values or len(values)>len(SOURCES)
            or any(not isinstance(value,str) or value not in SOURCES for value in values)
            or len(set(values))!=len(values)): raise MuseumImportError('museum','invalid_input')
    return list(values)


def _public(state):
    return {key:state.get(key) for key in ('source','status','message',*_STATS,'total_expected','collection','image_coverage')}


def run_museum_import(store, source, *, progress=None, stop=None, refresh=False):
    sources = _source_ids(source)
    stop = stop or threading.Event()
    directory = Path(store.directory) / 'museum-imports'
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    states = {value:(_fresh(value) if refresh else read_museum_checkpoint(store.directory,value)) for value in sources}
    errors = []
    def report(current, status='running', message=None):
        event = {'source':current,'current_source':current,'status':status,
                 **{key:sum(state[key] for state in states.values()) for key in _STATS},
                 'total_expected':sum(state['total_expected'] for state in states.values())
                    if all(state.get('total_expected') is not None for state in states.values()) else None,
                 'results':[_public(state) for state in states.values()], 'errors':list(errors),
                 'message':message or states[current]['message']}
        if progress: progress(event)
        return event
    with (directory/'full-import.lock').open('a+b') as lock:
        try: fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise MuseumImportError(sources[0],'import_busy') from None
        for current in sources:
            state = states[current]
            checkpoint = directory/(current+'.json')
            transport = None
            try:
                _check_stop(stop)
                if state['status']=='completed':
                    state['message']='This official snapshot is already imported; use --refresh for a new snapshot.'
                    report(current); continue
                state['status']='running'; state['message']='Preparing the full official metadata collection.'
                _atomic_json(checkpoint,state); report(current)
                if current in _SNAPSHOTS:
                    if refresh:
                        asset_path=directory/'assets'/_SNAPSHOTS[current][0]
                        asset_path.unlink(missing_ok=True)
                        asset_path.with_name(asset_path.name+'.part').unlink(missing_ok=True)
                    def downloaded(size,total):
                        state['message']=f'Downloading official metadata: {size//1048576} / {max(1,total//1048576)} MiB.'
                        report(current)
                    transport = SnapshotDownload(current,directory/'assets',stop,downloaded)
                    if state.get('asset') is None:
                        state['asset']=transport.descriptor(); _atomic_json(checkpoint,state)
                    path = transport.ensure(state['asset']); _atomic_json(checkpoint,state)
                    state['image_coverage']='checked_on_request' if current=='met' else 'eligible_source_urls'
                    pages = snapshot_pages(current,path,state['asset']['version'],state.get('cursor'),stop)
                else:
                    from .museum_bulk_linked import iter_pages
                    transport = FullJSON(current,stop)
                    state['image_coverage']='checked_on_request' if current=='getty' else 'eligible_source_urls'
                    pages = iter_pages(current,transport,state.get('cursor'))
                terminal = False
                for records, cursor, expected in pages:
                    _check_stop(stop)
                    if not isinstance(records,list) or len(records)>max(BATCH_SIZE,100):
                        raise MuseumImportError(current,'invalid_response')
                    new_processed=state['processed']+len(records)
                    if cursor is None and expected is not None and new_processed!=expected:
                        raise MuseumImportError(current,'incomplete_catalogue')
                    source_info = SOURCES[current]
                    result = store.import_records(records,collection_id=source_info.collection_id,label=source_info.label,
                        source_kind='museum-'+current,source_url=source_info.source_url,update_existing=True)
                    state['processed'] = new_processed
                    for key in ('added','updated','duplicates'): state[key] += result.get(key,0)
                    state['images'] += sum(bool(record.get('image_url')) for record in records)
                    state['cursor']=cursor; state['total_expected']=expected
                    state['collection']=result['collection']
                    state['message']=f"Imported {state['processed']:,} metadata records from this source."
                    if cursor is None:
                        terminal=True; state['status']='completed'
                        state['message']='Full official metadata snapshot imported. Image files were not downloaded.'
                    _atomic_json(checkpoint,state); report(current)
                    if terminal: break
                if not terminal: raise MuseumImportError(current,'incomplete_catalogue')
            except BulkStopped:
                state['status']='paused'; state['message']='Import paused; committed batches will be resumed.'
                _atomic_json(checkpoint,state); report(current,'paused'); break
            except (MuseumImportError,OSError,ValueError,OverflowError,sqlite3.Error) as error:
                reason = (error.reason if isinstance(error,MuseumImportError) else 'storage_full' if isinstance(error,OverflowError)
                          else 'storage_error' if isinstance(error,(OSError,sqlite3.Error)) else 'invalid_response')
                state['status']='failed'; state['message']=_MESSAGES.get(reason,_MESSAGES['unavailable'])
                errors.append({'source':current,'reason':reason,'message':state['message']})
                _atomic_json(checkpoint,state); report(current)
            finally:
                if transport is not None: transport.close()
        status = 'paused' if stop.is_set() else 'failed' if errors else 'completed'
        result = report(current,status,'All requested source snapshots are imported.' if status=='completed'
            else 'Import paused; resume to continue.' if status=='paused' else 'Some sources remain incomplete; resume to retry them.')
        return {'sources':sources,**result}
