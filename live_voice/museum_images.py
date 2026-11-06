"""Small, fixed-origin thumbnail relay for saved, eligible Chicago artworks."""
import asyncio
from collections import OrderedDict
import re
import time

import httpx


_ID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z')
MAX_IMAGE_BYTES = 1024 * 1024
MAX_CACHE_BYTES = 16 * 1024 * 1024
MAX_CACHE_IMAGES = 32


class MuseumImageUnavailable(ValueError): pass


class MuseumImages:
    def __init__(self, *, client=None, clock=time.monotonic):
        self.client = client
        self.owned = client is None
        self.clock = clock
        self.cache = OrderedDict()
        self.cache_bytes = 0
        self.lock = asyncio.Lock()
        self.last_request = None

    async def _download(self,url):
        async with self.client.stream('GET',url,follow_redirects=False,
                headers={'Referer':'https://www.artic.edu/', 'Accept':'image/jpeg',
                         'User-Agent':'CharacterStudioMuseumImages/1.0'}) as response:
            if response.status_code!=200 or response.headers.get('content-type','').split(';',1)[0]!='image/jpeg':
                raise MuseumImageUnavailable()
            raw = bytearray()
            async for part in response.aiter_bytes():
                if len(raw)+len(part)>MAX_IMAGE_BYTES: raise MuseumImageUnavailable()
                raw.extend(part)
        data = bytes(raw)
        if not data.startswith(b'\xff\xd8\xff') or len(data)<4: raise MuseumImageUnavailable()
        return data

    async def fetch(self, store, identifier):
        if not isinstance(identifier,str) or not _ID.fullmatch(identifier):
            raise MuseumImageUnavailable()
        url = f'https://www.artic.edu/iiif/2/{identifier}/full/400,/0/default.jpg'
        if not await asyncio.to_thread(store.has_source_image,'art-institute-of-chicago','museum-aic',url):
            raise MuseumImageUnavailable()
        async with self.lock:
            cached = self.cache.get(identifier)
            if cached and self.clock()-cached[0]<900:
                self.cache.move_to_end(identifier)
                return cached[1]
            if cached:
                self.cache_bytes -= len(cached[1]); del self.cache[identifier]
            if self.last_request is not None:
                pause = 1.05-(self.clock()-self.last_request)
                if pause>0: await asyncio.sleep(pause)
            self.last_request = self.clock()
            if self.client is None:
                self.client = httpx.AsyncClient(timeout=httpx.Timeout(15,connect=5),follow_redirects=False,trust_env=False)
            try:
                data = await asyncio.wait_for(self._download(url),timeout=25)
            except (httpx.HTTPError,asyncio.TimeoutError):
                raise MuseumImageUnavailable() from None
            while self.cache and (len(self.cache)>=MAX_CACHE_IMAGES or self.cache_bytes+len(data)>MAX_CACHE_BYTES):
                _, (_,old) = self.cache.popitem(last=False); self.cache_bytes -= len(old)
            self.cache[identifier] = (self.clock(),data); self.cache_bytes += len(data)
            return data

    async def close(self):
        if self.owned and self.client is not None: await self.client.aclose()
        self.cache.clear(); self.cache_bytes = 0
