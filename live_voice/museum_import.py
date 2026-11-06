"""Explicit, bounded imports from five official museum catalogues.

These requests happen before a call. Retrieval during conversation remains local.
"""
from __future__ import annotations

import importlib
import json
import re
import time
from urllib.parse import urlsplit

import httpx

from .museum_common import MuseumImportError, SOURCES, validate_import_input


MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_REQUESTS = 2500
MAX_IMPORT_SECONDS = 600
MAX_JSON_DEPTH = 64
MAX_JSON_VALUES = 250_000
_PATHS = {
    'met':('collectionapi.metmuseum.org', re.compile(r'/public/collection/(?:v1\.1/search|v1/objects/[0-9]{1,12})/?\Z')),
    'aic':('api.artic.edu', re.compile(r'/api/v1/artworks(?:/search|/[0-9]{1,12})?/?\Z')),
    'cleveland':('openaccess-api.clevelandart.org', re.compile(r'/api/artworks(?:/[0-9]{1,12})?/?\Z')),
    'rijksmuseum':('data.rijksmuseum.nl', re.compile(r'/(?:search/collection|[0-9]{1,30})/?\Z')),
    'getty':('data.getty.edu', re.compile(r'/(?:museum/collection/(?:sparql|object/[a-f0-9-]{36})|media/image/[a-f0-9-]{36})/?\Z')),
}
ERROR_MESSAGES = {
    'invalid_input':'Choose a museum, an artist or subject, and a valid result limit.',
    'rate_limited':'The museum is busy. Wait a little and try this museum again.',
    'timeout':'The museum did not respond in time. Try a smaller import.',
    'request_limit':'This search needed too many requests. Try a narrower artist or subject.',
    'invalid_response':'The museum returned a catalogue format this importer could not read.',
    'invalid_link':'The museum returned an unsupported record link.',
    'unavailable':'The museum catalogue is temporarily unavailable. Try again later.',
    'storage_conflict':'A saved record has changed upstream. The existing collection was kept; see the museum import guide.',
    'storage_full':'The local collections database has reached its record limit.',
    'storage_error':'The collection could not be saved. Check the archive storage.',
}


def museum_error(error, provider):
    reason = error.reason if isinstance(error,MuseumImportError) else 'unavailable'
    return {'source':provider, 'reason':reason,
            'message':ERROR_MESSAGES.get(reason,ERROR_MESSAGES['unavailable'])}


def _bounded_structure(value):
    """Apply the same nesting limit on Python versions with different decoders."""
    stack, visited = [iter((value,))], 0
    while stack:
        try: current = next(stack[-1])
        except StopIteration:
            stack.pop(); continue
        visited += 1
        if visited>MAX_JSON_VALUES: raise ValueError('Catalogue JSON exceeds its structure limit.')
        if isinstance(current,(dict,list)):
            if len(stack)>MAX_JSON_DEPTH: raise ValueError('Catalogue JSON exceeds its nesting limit.')
            stack.append(iter(current.values()) if isinstance(current,dict) else iter(current))
    return value


def _invalid_constant(value): raise ValueError('Use standard JSON values.')


class MuseumHTTP:
    """A single-source read-only client; allowlisted URLs, no redirects, bounded JSON."""
    def __init__(self, provider, *, client=None, clock=time.monotonic, sleep=time.sleep):
        if provider not in SOURCES: raise MuseumImportError(provider,'invalid_input')
        self.provider = provider
        self.client = client or httpx.Client(timeout=httpx.Timeout(30,connect=10),follow_redirects=False,trust_env=False)
        self.owned = client is None
        self.clock, self.sleep = clock, sleep
        self.started, self.last_request, self.requests = clock(), None, 0
        self.bytes = 0

    def _url(self, url):
        try:
            parts = urlsplit(url)
            host, path = _PATHS[self.provider]
            if (parts.scheme!='https' or parts.netloc!=host or not path.fullmatch(parts.path)
                    or parts.fragment or len(url)>12_000 or any(ord(c)<32 or ord(c)==127 for c in url)):
                raise ValueError()
        except (ValueError,TypeError):
            raise MuseumImportError(self.provider,'invalid_link') from None

    def __call__(self, url, params=None, headers=None):
        self._url(url)
        selected_headers = {'Accept':'application/json',
            'User-Agent':'CharacterStudioMuseumImporter/1.0 (https://github.com/aleybaracatCoSTAR/Character_Studio_Qwen)'}
        if self.provider=='aic': selected_headers['AIC-User-Agent'] = selected_headers['User-Agent']
        if headers:
            if not isinstance(headers,dict) or set(headers)-{'Accept'}:
                raise MuseumImportError(self.provider,'invalid_input')
            selected_headers.update(headers)
        for attempt in range(3):
            if self.requests>=MAX_REQUESTS: raise MuseumImportError(self.provider,'request_limit')
            if self.clock()-self.started>=MAX_IMPORT_SECONDS: raise MuseumImportError(self.provider,'timeout')
            interval = 1.05 if self.provider=='aic' else 0.2
            if self.last_request is not None:
                remaining = interval-(self.clock()-self.last_request)
                if remaining>0: self.sleep(remaining)
            self.last_request = self.clock(); self.requests += 1
            try:
                with self.client.stream('GET',url,params=params,headers=selected_headers,follow_redirects=False) as response:
                    if response.status_code==429 or response.status_code in (500,502,503,504):
                        if attempt==2:
                            raise MuseumImportError(self.provider,'rate_limited' if response.status_code==429 else 'unavailable')
                        retry = response.headers.get('retry-after','')
                        delay = min(float(retry),5) if re.fullmatch(r'[0-9]{1,3}',retry) else 1+attempt
                        self.sleep(delay)
                        continue
                    if response.status_code!=200: raise MuseumImportError(self.provider,'unavailable')
                    content_type = response.headers.get('content-type','').split(';',1)[0].strip().lower()
                    if content_type not in ('application/json','application/ld+json','application/sparql-results+json'):
                        raise MuseumImportError(self.provider,'invalid_response')
                    raw = bytearray()
                    for part in response.iter_bytes():
                        if self.clock()-self.started>=MAX_IMPORT_SECONDS:
                            raise MuseumImportError(self.provider,'timeout')
                        if len(raw)+len(part)>MAX_RESPONSE_BYTES or self.bytes+len(part)>MAX_TOTAL_BYTES:
                            raise MuseumImportError(self.provider,'invalid_response')
                        self.bytes += len(part)
                        raw.extend(part)
                    value = _bounded_structure(json.loads(raw,parse_constant=_invalid_constant))
                    if not isinstance(value,dict): raise MuseumImportError(self.provider,'invalid_response')
                    return value
            except httpx.TimeoutException:
                if attempt==2: raise MuseumImportError(self.provider,'timeout') from None
            except (httpx.HTTPError,ValueError,UnicodeError,RecursionError) as error:
                if isinstance(error,MuseumImportError): raise
                raise MuseumImportError(self.provider,'invalid_response' if isinstance(error,(ValueError,UnicodeError,RecursionError)) else 'unavailable') from None
        raise MuseumImportError(self.provider,'unavailable')

    def close(self):
        if self.owned: self.client.close()


class MuseumImporter:
    def __init__(self, client_factory=MuseumHTTP): self.client_factory=client_factory

    def import_collection(self, store, provider, query, limit):
        query, limit = validate_import_input(provider,query,limit)
        source = SOURCES[provider]
        adapter = importlib.import_module('.museum_'+provider,__package__)
        transport = self.client_factory(provider)
        try:
            records, seen = [], set()
            try:
                for record in adapter.iter_records(transport,query,limit):
                    if not isinstance(record,dict) or len(records)>=limit:
                        raise MuseumImportError(provider,'invalid_response')
                    identity = record.get('id')
                    if identity in seen: raise MuseumImportError(provider,'invalid_response')
                    seen.add(identity); records.append(record)
            except MuseumImportError: raise
            except (ValueError,TypeError,KeyError,AttributeError,RecursionError):
                raise MuseumImportError(provider,'invalid_response') from None
            try:
                result = store.import_records(records,collection_id=source.collection_id,
                    label=source.label,source_kind='museum-'+provider,source_url=source.source_url)
            except OverflowError:
                raise MuseumImportError(provider,'storage_full') from None
            except ValueError:
                raise MuseumImportError(provider,'storage_conflict') from None
            return {'source':provider, **result, 'query':query, 'limit':limit,
                    'imported_scope':'matching_records', 'requests':getattr(transport,'requests',0)}
        finally: transport.close()
