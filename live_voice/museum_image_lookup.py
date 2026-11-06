"""Resolve retrieved Met/Getty image links omitted from bulk metadata, never pixels."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import copy
import json
import re
import time
import uuid

import httpx

from .museum_common import MuseumImportError
from .museum_met import OBJECT_URL, _record
from . import museum_getty


class MuseumImageLookup:
    def __init__(self, *, client=None, clock=time.monotonic):
        self.client = client
        self.owned = client is None
        self.clock = clock
        self.cache = OrderedDict()
        self.lock = asyncio.Lock()

    def _remember(self,key,record):
        self.cache[key] = (self.clock(),record)
        self.cache.move_to_end(key)
        while len(self.cache)>512: self.cache.popitem(last=False)

    async def _met(self, identity):
        async with self.lock:
            cached = self.cache.get(identity)
            if cached and self.clock()-cached[0] < (86400 if cached[1] else 300):
                self.cache.move_to_end(identity)
                return cached[1]
            if self.client is None:
                self.client = httpx.AsyncClient(timeout=httpx.Timeout(5,connect=3),
                    follow_redirects=False,trust_env=False)
            record = None
            try:
                async with asyncio.timeout(6):
                    async with self.client.stream('GET',OBJECT_URL+identity,
                            headers={'Accept':'application/json','User-Agent':'CharacterStudioImageLookup/1.0'}) as response:
                        if response.status_code != 200: raise MuseumImportError('met','unavailable')
                        raw = bytearray()
                        async for part in response.aiter_bytes():
                            if len(raw)+len(part)>1024*1024: raise MuseumImportError('met','invalid_response')
                            raw.extend(part)
                        record = _record(json.loads(raw),int(identity))
            except (httpx.HTTPError,TimeoutError,ValueError,TypeError,RecursionError,MuseumImportError):
                record = None
            except asyncio.CancelledError:
                self._remember(identity,None)
                raise
            self._remember(identity,record)
            return record

    async def _json(self,url):
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=httpx.Timeout(5,connect=3),follow_redirects=False,trust_env=False)
        async with self.client.stream('GET',url,headers={'Accept':'application/json','User-Agent':'CharacterStudioImageLookup/1.0'}) as response:
            if response.status_code != 200: raise MuseumImportError('getty','unavailable')
            raw = bytearray()
            async for part in response.aiter_bytes():
                if len(raw)+len(part)>1024*1024: raise MuseumImportError('getty','invalid_response')
                raw.extend(part)
            value = json.loads(raw)
            if not isinstance(value,dict): raise MuseumImportError('getty','invalid_response')
            return value

    async def _getty(self,identity):
        key = ('getty',identity)
        async with self.lock:
            cached = self.cache.get(key)
            if cached and self.clock()-cached[0]<(86400 if cached[1] else 300):
                self.cache.move_to_end(key); return cached[1]
            try:
                async with asyncio.timeout(10):
                    object_url = 'https://data.getty.edu/museum/collection/object/'+identity
                    data = await self._json(object_url)
                    values = {object_url:data}
                    reference = next((item for item in museum_getty._items(data.get('shows'))
                        if isinstance(item,dict) and 'http://vocab.getty.edu/aat/300215302' in museum_getty._classes(item)),None)
                    if reference is not None:
                        image_id = museum_getty._identifier(reference.get('id'),museum_getty._MEDIA)
                        image_url = 'https://data.getty.edu/media/image/'+image_id
                        values[image_url] = await self._json(image_url)
                    def fetched(url):
                        if url not in values: raise MuseumImportError('getty','invalid_link')
                        return values[url]
                    record = museum_getty._record(fetched,identity)
            except (httpx.HTTPError,TimeoutError,ValueError,TypeError,RecursionError,KeyError):
                record = None
            except asyncio.CancelledError:
                self._remember(key,None)
                raise
            self._remember(key,record)
            return record

    async def resolve(self, matches):
        result = copy.deepcopy(list(matches)[:3])
        try:
            # A missing external image must not add three independent timeouts
            # to a spoken reply. Already resolved links survive this deadline.
            async with asyncio.timeout(6):
                await self._resolve(result)
        except TimeoutError:
            pass
        return result

    async def _resolve(self,result):
        for match in result:
            if not isinstance(match,dict) or match.get('image_url'):
                continue
            identity = match.get('source_entry_id')
            if (match.get('source_kind') == 'museum-met' and match.get('collection_id') == 'metropolitan-museum-of-art'
                    and isinstance(identity,str) and re.fullmatch(r'[1-9][0-9]{0,11}',identity)
                    and match.get('id') == uuid.uuid5(uuid.NAMESPACE_URL,'met:'+identity).hex
                    and match.get('source_url') == 'https://www.metmuseum.org/art/collection/search/'+identity):
                record = await self._met(identity)
            elif (match.get('source_kind') == 'museum-getty' and match.get('collection_id') == 'getty-museum'
                    and isinstance(identity,str) and re.fullmatch(museum_getty._UUID,identity)
                    and match.get('id') == uuid.uuid5(uuid.NAMESPACE_URL,'getty:'+identity).hex):
                record = await self._getty(identity)
            else:
                continue
            if record and record.get('image_url'):
                match['image_url'] = record['image_url']
                match['image_alt'] = record['image_alt']
                match['metadata'] = {**match.get('metadata',{}),'rights':record['metadata']['rights']}

    async def close(self):
        if self.owned and self.client is not None: await self.client.aclose()
