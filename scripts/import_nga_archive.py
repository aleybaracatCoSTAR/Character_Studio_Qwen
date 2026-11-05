"""Import pinned NGA collection metadata and image links; no images or models load."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import uuid

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REVISION = '68d372221c7a848b932ce07c1e1f8c293c646688'
SOURCE_URL = 'https://github.com/NationalGalleryOfArt/opendata'
COLLECTION_ID = 'national-gallery-of-art'
LABEL = 'National Gallery of Art'
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 384 * 1024 * 1024
MAX_OBJECTS = 500_000
MAX_IMAGES = 1_000_000
MAX_FIELD_CHARACTERS = 2 * 1024 * 1024
# Git blob hashes and lengths from the official pinned repository tree.
FILES = {
    'objects.csv': {'bytes':82_660_804, 'git_blob_sha1':'5bd07fe48b6ebfc745cbcb025f3ab45f50bcc2ee'},
    'published_images.csv': {'bytes':92_023_508, 'git_blob_sha1':'03e653fb80aecae72a3453d77047bf5705e6697d'},
}
OBJECT_HEADERS = frozenset(('objectid','title','attribution','displaydate','medium',
                            'classification','creditline','accessionnum'))
IMAGE_HEADERS = frozenset(('uuid','iiifurl','iiifthumburl','viewtype','sequence','openaccess',
                           'depictstmsobjectid','assistivetext'))
_ID = re.compile(r'[0-9]{1,12}\Z', re.ASCII)
_IMAGE_ID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z', re.ASCII)
_HTML = re.compile(r'<\s*/?\s*(?:i|em|b|strong|p|br|div|span|sup|sub|a|ul|ol|li|script|style)\b[^>]*>', re.I)
_CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
_TEXT_FIELDS = (('Title','title'),('Artist / attribution','attribution'),('Date','displaydate'),
    ('Medium','medium'),('Dimensions','dimensions'),('Classification','classification'),
    ('Subclass','subclassification'),('Accession number','accessionnum'),('Credit','creditline'),
    ('Provenance','provenancetext'),('Inscriptions','inscription'),('Markings','markings'),
    ('Watermarks','watermarks'),('Portfolio','portfolio'),('Series','series'),('Volume','volume'))
_METADATA = {'artist':('attribution',700),'date':('displaydate',250),'medium':('medium',700),
    'dimensions':('dimensions',500),'credit_line':('creditline',500),
    'accession_number':('accessionnum',120),'classification':('classification',200)}


class NGAImportError(ValueError):
    """Fixed importer diagnostics, never artwork text or upstream response bodies."""


_SAFE_ERRORS = {
    'Choose one of the two supported NGA CSV files.':'unsupported_source',
    'The NGA cache must not use symbolic links.':'cache_symlink',
    'The official NGA download failed; cached files were kept.':'download_http',
    'The official NGA download did not return a CSV file.':'download_content_type',
    'The NGA download exceeded its file-size limit.':'download_size',
    'The NGA download exceeded its total-size limit.':'download_total_size',
    'The NGA download did not match the pinned official file.':'download_integrity',
    'Use two nonempty NGA CSV files within the import-size limits.':'input_size',
    'The NGA images CSV exceeds its row limit.':'image_row_limit',
    'The NGA objects CSV exceeds its row limit.':'object_row_limit',
}
_ROW_ERRORS = {
    'has an invalid object ID':'invalid_object_id',
    'has invalid columns':'invalid_columns',
    'has an invalid rights flag':'invalid_rights',
    'has an invalid official image link':'invalid_image_url',
    'repeats an object ID':'duplicate_object_id',
}
_STAGES = frozenset(('prepare_source','open_database','import_records','close_database'))


def safe_diagnostic(error, stage):
    """Whitelist fixed reasons and numeric row/error codes; never echo raw errors."""
    chosen_stage=getattr(error,'nga_import_stage',stage)
    result={'stage':chosen_stage if chosen_stage in _STAGES else 'import_records',
            'reason':'import_failed'}
    if type(error) is NGAImportError:
        text=str(error)
        if text in _SAFE_ERRORS:result['reason']=_SAFE_ERRORS[text]
        match=re.fullmatch(r'The (objects|images) CSV (.+) at row ([1-9][0-9]{0,8})\.',text)
        if match and match[2] in _ROW_ERRORS:
            result.update(reason=_ROW_ERRORS[match[2]],
                          file='objects.csv' if match[1]=='objects' else 'published_images.csv',row=int(match[3]))
        match=re.fullmatch(r'The (objects|images) CSV (has missing or duplicate headers|is not readable bounded UTF-8 CSV)\.',text)
        if match:result.update(reason='invalid_headers' if match[2].startswith('has ') else 'invalid_csv',
                               file='objects.csv' if match[1]=='objects' else 'published_images.csv')
    elif type(error) is ValueError:
        match=re.fullmatch(r'Collection import row ([1-9][0-9]{0,8}) is invalid or conflicts with a saved record\.',str(error))
        if match:result.update(reason='invalid_collection_record',row=int(match[1]))
    if isinstance(error,sqlite3.Error):
        result['reason']='sqlite_error'
        code=getattr(error,'sqlite_errorcode',None)
        if type(code) is int and 0<=code<=2**31-1:result['sqlite_code']=code
    elif isinstance(error,OSError):
        result['reason']='filesystem_error'
        code=getattr(error,'errno',None)
        if type(code) is int and 0<=code<=4096:result['errno']=code
    return result


def _mark_stage(error, stage):
    try:error.nga_import_stage=stage
    except Exception:pass


def default_directory() -> Path:
    return Path(os.environ.get('LIVE_ARCHIVE_DIR') or
                str(Path(os.environ.get('LIVE_VOICE_STORE') or
                         Path.home()/'.local/share/character-studio')/'archive'))


def source_url(filename):
    if filename not in FILES: raise NGAImportError('Choose one of the two supported NGA CSV files.')
    return f'https://raw.githubusercontent.com/NationalGalleryOfArt/opendata/{REVISION}/data/{filename}'


def _digests(path, expected):
    size = path.stat().st_size
    if not 0 < size <= MAX_FILE_BYTES or size != expected['bytes']: return None
    blob = hashlib.sha1(usedforsecurity=False)
    blob.update(f'blob {size}\0'.encode('ascii'))
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for part in iter(lambda:stream.read(65536), b''):
            blob.update(part); digest.update(part)
    if blob.hexdigest() != expected['git_blob_sha1']: return None
    return {'bytes':size,'sha256':digest.hexdigest(),'git_blob_sha1':blob.hexdigest()}


def source_verification(objects, images):
    """Distinguish an exact official snapshot from compatible local CSV files."""
    verified=all(_digests(Path(path),FILES[name]) is not None
                 for name,path in (('objects.csv',objects),('published_images.csv',images)))
    return {'source_verified':verified,'source_revision':REVISION if verified else None,
            'configured_revision':REVISION}


def _atomic_metadata(path, value):
    fd, name = tempfile.mkstemp(prefix='.nga-metadata-',dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            os.fchmod(stream.fileno(),0o600)
            json.dump(value,stream,sort_keys=True);stream.write('\n');stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally: temporary.unlink(missing_ok=True)


def download_sources(directory, *, client=None):
    """Download only two fixed official blobs, reusing byte-verified pinned caches."""
    cache = Path(directory)
    for component in ('imports','nga',REVISION):
        cache = cache/component
        if cache.is_symlink(): raise NGAImportError('The NGA cache must not use symbolic links.')
        cache.mkdir(parents=True,exist_ok=True,mode=0o700)
    total = 0
    paths = {}
    owned = client is None
    if owned: client = httpx.Client(timeout=httpx.Timeout(90,connect=10),follow_redirects=False,trust_env=False)
    try:
        for filename, expected in FILES.items():
            destination = cache/filename
            metadata = destination.with_suffix('.metadata.json')
            if destination.is_symlink() or metadata.is_symlink():
                raise NGAImportError('The NGA cache must not use symbolic links.')
            verified = _digests(destination,expected) if destination.is_file() else None
            if verified is None:
                fd, name = tempfile.mkstemp(prefix='.nga-download-',dir=cache)
                temporary = Path(name)
                try:
                    with os.fdopen(fd,'wb') as stream:
                        os.fchmod(stream.fileno(),0o600)
                        with client.stream('GET',source_url(filename),follow_redirects=False,
                                headers={'Accept':'text/csv,text/plain','User-Agent':'CharacterStudio-NGA-import/1.0'}) as response:
                            if response.status_code != 200:
                                raise NGAImportError('The official NGA download failed; cached files were kept.')
                            kind = response.headers.get('content-type','').split(';')[0].strip().lower()
                            if kind not in ('text/csv','text/plain','application/octet-stream'):
                                raise NGAImportError('The official NGA download did not return a CSV file.')
                            length = response.headers.get('content-length')
                            if length is not None and (not length.isascii() or not length.isdigit()
                                                       or int(length)>MAX_FILE_BYTES):
                                raise NGAImportError('The NGA download exceeded its file-size limit.')
                            count = 0
                            for part in response.iter_bytes(65536):
                                count += len(part)
                                if count>MAX_FILE_BYTES or total+count>MAX_TOTAL_BYTES:
                                    raise NGAImportError('The NGA download exceeded its file-size limit.')
                                stream.write(part)
                        stream.flush();os.fsync(stream.fileno())
                    verified = _digests(temporary,expected)
                    if verified is None:
                        raise NGAImportError('The NGA download did not match the pinned official file.')
                    os.replace(temporary,destination)
                except httpx.HTTPError:
                    raise NGAImportError('The official NGA download failed; cached files were kept.') from None
                finally: temporary.unlink(missing_ok=True)
            total += verified['bytes']
            if total>MAX_TOTAL_BYTES: raise NGAImportError('The NGA download exceeded its total-size limit.')
            _atomic_metadata(metadata,{'schema':1,'revision':REVISION,'source_url':source_url(filename),
                'metadata_license':'CC0-1.0','image_policy':'Only openaccess=1 image links are selected.',
                'checked_at':datetime.now(timezone.utc).isoformat(),**verified})
            paths[filename] = destination
        return paths['objects.csv'],paths['published_images.csv']
    finally:
        if owned: client.close()


class _Plain(HTMLParser):
    def __init__(self): super().__init__(convert_charrefs=True);self.parts=[];self.skip=0
    def handle_starttag(self, tag, attrs):
        if tag in ('script','style'): self.skip+=1
        elif not self.skip and tag in ('br','p','div','li'): self.parts.append('\n')
    def handle_endtag(self, tag):
        if tag in ('script','style') and self.skip:self.skip-=1
        elif not self.skip and tag in ('p','div','li'):self.parts.append('\n')
    def handle_data(self, text):
        if not self.skip:self.parts.append(text)


def _plain(value):
    value = value or ''
    if _HTML.search(value):
        parser=_Plain();parser.feed(value);parser.close();value=''.join(parser.parts)
    return _CONTROL.sub('',unescape(value)).strip()


def _limit(value, maximum):
    if len(value)<=maximum:return value,False
    marker='… [excerpt]'
    return value[:maximum-len(marker)].rstrip()+marker,True


@contextmanager
def _rows(path, required, name):
    old_limit=csv.field_size_limit()
    csv.field_size_limit(MAX_FIELD_CHARACTERS)
    try:
        with Path(path).open('r',encoding='utf-8-sig',newline='') as stream:
            reader=csv.DictReader(stream,strict=True)
            headers=reader.fieldnames
            if not headers or len(headers)!=len(set(headers)) or not required.issubset(headers):
                raise NGAImportError(f'The {name} CSV has missing or duplicate headers.')
            yield reader
    except (csv.Error,UnicodeError):
        raise NGAImportError(f'The {name} CSV is not readable bounded UTF-8 CSV.') from None
    finally:csv.field_size_limit(old_limit)


def _object_id(value, name, row):
    # Zero is a real identifier in the pinned source: accession 1937.1.2.c.
    # Only a blank image-owner field represents an unlinked image here.
    if not isinstance(value,str) or not _ID.fullmatch(value.strip()):
        raise NGAImportError(f'The {name} CSV has an invalid object ID at row {row}.')
    return str(int(value))


def _input_sizes(objects, images):
    sizes=[Path(path).stat().st_size for path in (objects,images)]
    if any(not 0<size<=MAX_FILE_BYTES for size in sizes) or sum(sizes)>MAX_TOTAL_BYTES:
        raise NGAImportError('Use two nonempty NGA CSV files within the import-size limits.')


def iter_records(objects, images, *, stats=None):
    """Stream artwork rows; retain only the best eligible thumbnail per artwork."""
    _input_sizes(objects,images)
    if stats is None:stats={}
    stats.update(objects_read=0,images_read=0,images_unlinked=0,titles_shortened=0,text_excerpted=0,
                 metadata_excerpted=0,image_alt_shortened=0)
    selected={}
    with _rows(images,IMAGE_HEADERS,'images') as reader:
        for number,row in enumerate(reader,2):
            stats['images_read']+=1
            if stats['images_read']>MAX_IMAGES:raise NGAImportError('The NGA images CSV exceeds its row limit.')
            if None in row or any(value is None for value in row.values()):
                raise NGAImportError(f'The images CSV has invalid columns at row {number}.')
            if row['openaccess'] not in ('0','1'):
                raise NGAImportError(f'The images CSV has an invalid rights flag at row {number}.')
            if not row['depictstmsobjectid'].strip():
                stats['images_unlinked']+=1
                continue
            object_id=_object_id(row['depictstmsobjectid'],'images',number)
            if row['openaccess']=='0':continue
            image_id=row['uuid'].strip()
            base=f'https://api.nga.gov/iiif/{image_id}'
            thumbnail=base+'/full/!200,200/0/default.jpg'
            if (not _IMAGE_ID.fullmatch(image_id) or row['iiifurl']!=base
                    or row['iiifthumburl']!=thumbnail or row['viewtype'] not in ('primary','alternate')):
                raise NGAImportError(f'The images CSV has an invalid official image link at row {number}.')
            sequence=row['sequence'].strip()
            order=int(sequence) if re.fullmatch(r'[0-9]{1,9}',sequence,re.ASCII) else 10**12
            rank=(row['viewtype']!='primary',order,image_id)
            if object_id not in selected or rank<selected[object_id][0]:
                alt,shortened=_limit(_plain(row['assistivetext']),500)
                selected[object_id]=(rank,thumbnail,alt,shortened)
    seen=set()
    with _rows(objects,OBJECT_HEADERS,'objects') as reader:
        for number,row in enumerate(reader,2):
            stats['objects_read']+=1
            if stats['objects_read']>MAX_OBJECTS:raise NGAImportError('The NGA objects CSV exceeds its row limit.')
            if None in row or any(value is None for value in row.values()):
                raise NGAImportError(f'The objects CSV has invalid columns at row {number}.')
            object_id=_object_id(row['objectid'],'objects',number)
            if object_id in seen:raise NGAImportError(f'The objects CSV repeats an object ID at row {number}.')
            seen.add(object_id)
            plain={key:_plain(row.get(key,'')) for _,key in _TEXT_FIELDS}
            full_title=plain['title'] or f'Untitled artwork (NGA {object_id})'
            title,shortened=_limit(full_title,240);stats['titles_shortened']+=int(shortened)
            text='\n'.join(f'{label}: {plain[key]}' for label,key in _TEXT_FIELDS if plain[key])
            if not text:text=f'Title: {full_title}\nNGA object ID: {object_id}'
            text,excerpted=_limit(text,12000);stats['text_excerpted']+=int(excerpted)
            metadata={}
            for key,(field,bound) in _METADATA.items():
                value,shortened=_limit(plain[field],bound)
                if value:metadata[key]=value
                stats['metadata_excerpted']+=int(shortened)
            image=selected.get(object_id)
            metadata['rights']=('Metadata: CC0-1.0. Selected image: CC0-1.0 (NGA open access).'
                                if image else 'Metadata: CC0-1.0. No open-access image selected.')
            if image:stats['image_alt_shortened']+=int(image[3])
            yield {'id':uuid.uuid5(uuid.NAMESPACE_URL,f'{COLLECTION_ID}:{object_id}').hex,
                'title':title,'text':text,'source_entry_id':object_id,
                'source_url':f'https://www.nga.gov/collection/art-object-page.{object_id}.html',
                'image_url':image[1] if image else None,'image_alt':image[2] if image else '',
                'metadata':metadata}


def import_sources(directory, objects, images, *, database_factory=None):
    if database_factory is None:
        from live_voice.collections import CollectionDatabase
        database_factory=CollectionDatabase
    stats={};failed=False
    try:provenance=source_verification(objects,images)
    except Exception as error:
        _mark_stage(error,'prepare_source');raise
    try:database=database_factory(Path(directory))
    except Exception as error:
        _mark_stage(error,'open_database');raise
    try:
        result=database.import_records(iter_records(objects,images,stats=stats),
            collection_id=COLLECTION_ID,label=LABEL,source_kind='nga',source_url=SOURCE_URL)
        return {**result,**stats,**provenance}
    except BaseException as error:
        failed=True;_mark_stage(error,'import_records');raise
    finally:
        try:database.close()
        except Exception as error:
            if not failed:
                _mark_stage(error,'close_database');raise


def main(argv=None, *, client=None, database_factory=None):
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--download',action='store_true',help='Reuse or download the two pinned official CSVs.')
    mode.add_argument('--objects',type=Path,help='Local NGA objects.csv.')
    parser.add_argument('--images',type=Path,help='Local NGA published_images.csv, with --objects.')
    parser.add_argument('--archive-dir',type=Path,default=None)
    args=parser.parse_args(argv)
    if bool(args.objects)!=bool(args.images):parser.error('Use --objects and --images together; --download takes neither.')
    directory=args.archive_dir or default_directory()
    stage='prepare_source'
    try:
        objects,images=download_sources(directory,client=client) if args.download else (args.objects,args.images)
        _input_sizes(objects,images)
        stage='import_records'
        result=import_sources(directory,objects,images,database_factory=database_factory)
    except (NGAImportError,OSError,sqlite3.Error,ValueError,OverflowError) as error:
        diagnostic=safe_diagnostic(error,stage)
        print(f'NGA import failed at {diagnostic["stage"]}. Check the CSV headers, pinned downloads, size limits and archive-directory permissions.',file=sys.stderr)
        print(json.dumps(diagnostic,sort_keys=True),file=sys.stderr)
        return 1
    print(json.dumps(result,ensure_ascii=True,sort_keys=True))
    return 0


if __name__=='__main__':raise SystemExit(main())
