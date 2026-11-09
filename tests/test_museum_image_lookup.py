"""Met CSV results receive only official, eligible, object-matched image links."""
import copy
import asyncio
import json
import unittest

import httpx

from live_voice.museum_common import museum_record
from live_voice.museum_image_lookup import MuseumImageLookup
from live_voice.archive import ArchiveSettings
from live_voice.collections import CollectionDatabase
from live_voice.director import DirectorSettings
from live_voice.session import LiveSession
from studio.characters import example
from test_archive_session import ArchiveProviders
import tempfile
from unittest.mock import patch


class MetImageLookupTests(unittest.IsolatedAsyncioTestCase):
    def record(self,identity='436535'):
        return {**museum_record('met',identity,'An artwork','https://www.metmuseum.org/art/collection/search/'+identity),
                'source_kind':'museum-met','collection_id':'metropolitan-museum-of-art','citation_id':'test','excerpt':'Catalogue facts'}

    async def test_resolves_images_and_preserves_catalogue_context_then_uses_cache(self):
        calls = []
        def response(request):
            calls.append(str(request.url))
            return httpx.Response(200,json={'objectID':436535,'isPublicDomain':True,'title':'API title',
                'primaryImageSmall':'https://images.metmuseum.org/CRDImages/ep/web-large/test.jpg'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            lookup = MuseumImageLookup(client=client)
            original = self.record(); matches = await lookup.resolve([original])
            self.assertIsNone(original['image_url'])
            self.assertTrue(matches[0]['image_url'].startswith('https://images.metmuseum.org/'))
            for key in ('title','id','source_url','citation_id','excerpt'):
                self.assertEqual(matches[0][key],original[key])
            self.assertEqual(await lookup.resolve([original]),matches)
            self.assertEqual(len(calls),1)

    async def test_forged_ids_custom_sources_and_existing_images_do_not_fetch(self):
        calls = []
        def response(request): calls.append(request); return httpx.Response(500)
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            lookup = MuseumImageLookup(client=client)
            for change in ({'source_kind':'custom'},{'id':'a'*32},{'source_url':'https://elsewhere.test'},
                           {'source_entry_id':'../private'},{'image_url':'https://images.metmuseum.org/already.jpg'}):
                record = {**self.record(),**change}
                self.assertEqual(await lookup.resolve([record]),[record])
            self.assertEqual(calls,[])

    async def test_non_public_mismatched_unavailable_and_external_image_links_remain_absent(self):
        replies = [
            {'objectID':436535,'isPublicDomain':False,'primaryImage':'https://images.metmuseum.org/CRDImages/x.jpg'},
            {'objectID':9,'isPublicDomain':True,'primaryImage':'https://images.metmuseum.org/CRDImages/x.jpg'},
            {'objectID':436535,'isPublicDomain':True,'primaryImage':'https://elsewhere.test/x.jpg'},
        ]
        for reply in replies:
            with self.subTest(reply=reply):
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,json=reply))) as client:
                    self.assertIsNone((await MuseumImageLookup(client=client).resolve([self.record()]))[0]['image_url'])

    async def test_temporary_failures_are_cached_and_expire_without_repeated_reply_delays(self):
        for failure in ('404','timeout','oversize'):
            requests = []; clock = [0]
            def response(request):
                requests.append(request)
                if failure == 'timeout': raise httpx.ReadTimeout('test')
                return httpx.Response(404) if failure == '404' else httpx.Response(200,content=b'x'*(1024*1024+1))
            async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
                lookup = MuseumImageLookup(client=client,clock=lambda:clock[0])
                for _ in range(2): self.assertIsNone((await lookup.resolve([self.record()]))[0]['image_url'])
                self.assertEqual(len(requests),1)
                clock[0] = 301
                await lookup.resolve([self.record()]); self.assertEqual(len(requests),2)

    async def test_whole_reply_lookup_has_one_deadline_not_three_timeouts(self):
        requests = []
        async def response(request):
            requests.append(request)
            await asyncio.sleep(2)
            return httpx.Response(404)
        real_timeout = asyncio.timeout
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            lookup = MuseumImageLookup(client=client)
            with patch('live_voice.museum_image_lookup.asyncio.timeout',side_effect=lambda delay:real_timeout(min(delay,.02))):
                result = await lookup.resolve([self.record(str(number)) for number in (1,2,3)])
            self.assertEqual(len(result),3); self.assertEqual(len(requests),1)
            self.assertIsNone(lookup.cache['1'][1])

    async def test_getty_bulk_metadata_resolves_the_separate_cc0_media_record_on_request(self):
        identity = '00004bde-6797-4bb8-8496-df451c2947c7'
        image_id = '11111111-1111-1111-1111-111111111111'
        object_url = 'https://data.getty.edu/museum/collection/object/'+identity
        media_url = 'https://data.getty.edu/media/image/'+image_id
        source = {**museum_record('getty',identity,'A Getty artwork',object_url),
            'source_kind':'museum-getty','collection_id':'getty-museum'}
        requests = []
        def response(request):
            requests.append(str(request.url))
            if str(request.url) == object_url:
                return httpx.Response(200,json={'id':object_url,'type':'HumanMadeObject','_label':'A Getty artwork',
                    'shows':[{'id':media_url,'classified_as':[{'id':'http://vocab.getty.edu/aat/300215302'}]}]})
            return httpx.Response(200,json={'id':media_url,'type':'VisualItem',
                'subject_to':[{'classified_as':[{'id':'https://creativecommons.org/publicdomain/zero/1.0/'}]}],
                'digitally_shown_by':[{'access_point':[{'id':'https://media.getty.edu/iiif/image/'+image_id}]}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            lookup = MuseumImageLookup(client=client)
            resolved = await lookup.resolve([source])
            self.assertEqual(requests,[object_url,media_url])
            self.assertTrue(resolved[0]['image_url'].startswith('https://media.getty.edu/iiif/image/'))
            self.assertIn('publicdomain/zero/1.0',resolved[0]['metadata']['rights'])
            self.assertEqual(resolved[0]['id'],source['id'])
            self.assertEqual(await lookup.resolve([source]),resolved)
            self.assertEqual(len(requests),2)

    async def test_resolved_link_is_shared_by_actor_director_and_image_event(self):
        with tempfile.TemporaryDirectory() as folder:
            store = CollectionDatabase(folder)
            source = self.record()
            source['metadata']['artist'] = 'Claude Monet'
            for key in ('source_kind','collection_id','citation_id','excerpt'): source.pop(key)
            store.import_records([source],collection_id='metropolitan-museum-of-art',label='Met',source_kind='museum-met')
            snapshot = store.open_snapshot(['metropolitan-museum-of-art'])
            def response(request):
                return httpx.Response(200,json={'objectID':436535,'isPublicDomain':True,'title':'An artwork',
                    'primaryImage':'https://images.metmuseum.org/CRDImages/ep/web-large/test.jpg'})
            async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
                events = []
                async def send(value): events.append(copy.deepcopy(value))
                async def audio(value): pass
                providers = ArchiveProviders()
                session = LiveSession(providers,example(),send,audio,director=DirectorSettings(enabled=True),
                    archive=ArchiveSettings(enabled=True,scope='library',collection_ids=['metropolitan-museum-of-art']),
                    archive_search=snapshot,image_lookup=MuseumImageLookup(client=client))
                try:
                    await session.begin(text='Show me an image of Monet.')
                    await asyncio.wait_for(session.task,2)
                    event = next(value for value in events if value['type']=='archive_sources')
                    self.assertTrue(event['matches'][0]['image_url'])
                    actor = providers.actor[0]['kwargs']['archive']
                    self.assertEqual(providers.director[0]['kwargs']['archive'],actor)
                    self.assertEqual(json.loads(actor[1]['content'])['references'],event['matches'])
                finally: await session.close()
            store.close()

    async def test_broad_colour_browsing_does_not_refill_with_lazy_metadata_or_call_object_api(self):
        with tempfile.TemporaryDirectory() as folder:
            store=CollectionDatabase(folder)
            source=self.record()
            source['title']='Colorful'
            source['text']='Colorful painting from the supplied catalogue.'
            source['metadata'].update(artist='Claude Monet',medium='oil on canvas')
            for key in ('source_kind','collection_id','citation_id','excerpt'):source.pop(key)
            store.import_records([source],collection_id='metropolitan-museum-of-art',label='Met',source_kind='museum-met')
            snapshot=store.open_snapshot(['metropolitan-museum-of-art'])
            requests=[]
            def response(request):
                requests.append(str(request.url))
                return httpx.Response(200,json={'objectID':436535,'isPublicDomain':True,'title':'Colorful',
                    'primaryImage':'https://images.metmuseum.org/CRDImages/ep/web-large/test.jpg'})
            async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
                events=[]
                async def send(value):events.append(copy.deepcopy(value))
                async def audio(value):pass
                session=LiveSession(ArchiveProviders(),example(),send,audio,
                    archive=ArchiveSettings(enabled=True,scope='library',collection_ids=['metropolitan-museum-of-art']),
                    archive_search=snapshot,image_lookup=MuseumImageLookup(client=client))
                try:
                    await session.begin(text='Show me colorful paintings with pictures.')
                    await asyncio.wait_for(session.task,2)
                    event=next(value for value in events if value['type']=='archive_sources')
                    self.assertEqual(event['matches'],[])
                    self.assertEqual(requests,[])
                    self.assertEqual(events[-1]['type'],'response_done')
                finally:await session.close()
            store.close()

    async def test_named_lazy_image_failure_never_publishes_an_unresolved_url(self):
        with tempfile.TemporaryDirectory() as folder:
            store=CollectionDatabase(folder)
            source=self.record()
            source['metadata']['artist']='Claude Monet'
            for key in ('source_kind','collection_id','citation_id','excerpt'):source.pop(key)
            store.import_records([source],collection_id='metropolitan-museum-of-art',label='Met',source_kind='museum-met')
            snapshot=store.open_snapshot(['metropolitan-museum-of-art'])
            requests=[]
            def response(request):
                requests.append(str(request.url));return httpx.Response(404)
            async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
                events=[]
                async def send(value):events.append(copy.deepcopy(value))
                async def audio(value):pass
                session=LiveSession(ArchiveProviders(),example(),send,audio,
                    archive=ArchiveSettings(enabled=True,scope='library',collection_ids=['metropolitan-museum-of-art']),
                    archive_search=snapshot,image_lookup=MuseumImageLookup(client=client))
                try:
                    await session.begin(text='Show me an image of Monet.')
                    await asyncio.wait_for(session.task,2)
                    event=next(value for value in events if value['type']=='archive_sources')
                    self.assertEqual(event['matches'],[])
                    self.assertEqual(len(requests),1)
                    self.assertEqual(events[-1]['type'],'response_done')
                finally:await session.close()
            store.close()
