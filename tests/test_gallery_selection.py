"""Gallery follow-ups select vetted catalogue records instead of searching rejected titles."""
import asyncio
from copy import deepcopy
import json
import tempfile
import unittest

from live_voice.archive import ArchiveSettings
from live_voice.artwork_requests import image_correction_request, image_reference_selection, image_request
from live_voice.collections import CollectionDatabase
from live_voice.session import LiveSession
from studio.characters import example
from test_artwork_dialogue import DialogueProviders
from test_artwork_session import COLLECTION, ObservedSnapshot, artwork


def musician(number, title, medium):
    item = artwork(number, 'Fixture artist', title)
    item['text'] = f'Catalogue topic: musicians. Supplied record for {title}.'
    item['metadata']['medium'] = medium
    return item


class GallerySelectionHelpersTests(unittest.TestCase):
    def setUp(self):
        self.matches = [musician(1, 'The Expulsion from Eden', 'cut paper'),
                        musician(2, 'Musicians (Musikanten)', 'woodcut'),
                        musician(3, 'Three Musicians', 'engraving')]

    def test_reported_spoken_medium_selects_only_the_known_woodcut(self):
        for text in ('and I see the wood cut.', 'Can I see the woodcut?', 'Show me the woodcut.'):
            with self.subTest(text=text):
                request = image_request(text)
                self.assertIsNotNone(request)
                self.assertEqual([item['id'] for item in image_reference_selection(request,self.matches)], [self.matches[1]['id']])

    def test_correction_excludes_the_rejected_title_and_preserves_music_ambiguity(self):
        request = image_correction_request("you're showing me the expulsion from Eden not the music one")
        self.assertEqual(request['query'], 'the music one')
        self.assertEqual([item['id'] for item in image_reference_selection(request,self.matches)],
                         [self.matches[1]['id'],self.matches[2]['id']])
        self.assertEqual(image_correction_request('I asked for the woodcut, not Eden')['query'], 'the woodcut')

    def test_unknown_new_title_searches_fresh_while_unresolved_medium_never_reuses_wrong_work(self):
        request = image_request('Show me an image of Water Lilies.')
        self.assertIsNone(image_reference_selection(request,self.matches))
        self.assertEqual(image_reference_selection(image_request('Show me the woodcut.'),self.matches[:1]), [])
        self.assertIsNone(image_request('I see the image.'))
        self.assertIsNone(image_correction_request('Tell me about Eden.'))

    def test_same_medium_is_not_resolved_by_ranking_and_inputs_are_unchanged(self):
        self.matches[2]['metadata']['medium'] = 'woodcut'
        before = deepcopy(self.matches)
        self.assertEqual(len(image_reference_selection(image_request('Show me the woodcut.'),self.matches)),2)
        self.assertEqual(self.matches,before)


class GallerySelectionSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = CollectionDatabase(self.directory.name)
        self.store.import_records([
            musician(1,'The Expulsion from Eden','cut paper'),
            musician(2,'Musicians (Musikanten)','woodcut'),
            musician(3,'Three Musicians','engraving')],
            collection_id=COLLECTION,label='National Gallery of Art',source_kind='nga')
        self.providers = DialogueProviders()
        self.events = []
        async def send_json(event): self.events.append(deepcopy(event))
        async def send_bytes(packet): pass
        self.snapshot = ObservedSnapshot(self.store.open_snapshot([COLLECTION]))
        self.session = LiveSession(self.providers,example(),send_json,send_bytes,
            archive=ArchiveSettings(enabled=True,scope='library',collection_ids=[COLLECTION]),
            archive_search=self.snapshot,gallery_greeting=True)

    async def asyncTearDown(self):
        await self.session.close(); self.store.close(); self.directory.cleanup()

    async def turn(self,text,*,spoken=False):
        offset = len(self.events)
        if spoken:
            self.providers.recognized_text = text
            await self.session.begin(pcm=b'\x01\x00'*160)
        else:
            await self.session.begin(text=text)
        await asyncio.wait_for(self.session.task,2)
        self.assertEqual(self.events[-1]['type'],'response_done')
        self.session.history.acknowledge(self.session.turn_id,0)
        self.session.playback_done(self.session.turn_id)
        return next(event for event in self.events[offset:] if event['type']=='archive_sources')

    def status(self,match,status='loaded',**updates):
        return {'type':'image_status','turn_id':self.session.turn_id,'status':status,
                'loaded_count':1 if status=='loaded' else 0,'total_count':1,
                'selected':{'collection_id':match['collection_id'],'id':match['id']},**updates}

    async def test_spoken_wood_cut_and_wrong_image_repair_publish_the_same_exact_source(self):
        first = await self.turn('Are there artworks about musicians?')
        self.assertEqual(len(first['matches']),3)
        self.assertIs(first['gallery_display'],True)
        eden = next(match for match in first['matches'] if match['title']=='The Expulsion from Eden')
        self.assertTrue(self.session.image_status(self.status(eden)))
        searches = len(self.snapshot.searches)
        shown = await self.turn('and I see the wood cut.',spoken=True)
        self.assertEqual([match['title'] for match in shown['matches']],['Musicians (Musikanten)'])
        self.assertIs(shown['image_request'],True)
        self.assertEqual(len(self.snapshot.searches),searches)
        self.assertEqual(self.providers.stt.calls,[b'\x01\x00'*160])
        self.assertTrue(self.session.image_status(self.status(shown['matches'][0])))
        repaired = await self.turn("you're showing me the expulsion from Eden not the music one")
        self.assertEqual(repaired['matches'],shown['matches'])
        self.assertIs(repaired['image_request'],True)
        self.assertEqual(len(self.snapshot.searches),searches)
        self.assertEqual(json.loads(self.providers.actor[-1]['kwargs']['archive'][1]['content'])['references'],shown['matches'])

    async def test_gallery_ack_identifies_one_central_image_among_three_and_next_prompt_is_truthful(self):
        source = await self.turn('musicians')
        match = source['matches'][1]
        self.assertEqual(self.session.image_display['total_count'],3)
        self.assertTrue(self.session.image_status(self.status(match)))
        self.assertEqual(self.session.image_display['selected_key'],(COLLECTION,match['id']))
        await self.turn('What does it look like?')
        context = self.providers.actor[-1]['kwargs']['archive']
        display = json.loads(context[1]['content'])['gallery_display']
        identity = {'collection_id':COLLECTION,'id':match['id']}
        self.assertEqual(display,{'status':'loaded','selected':identity,'requested':identity})
        self.assertEqual(len(json.loads(context[1]['content'])['references']),1)
        self.assertIn('Do not invent depicted instruments',context[0]['content'])
        self.assertIn('Never say "you are looking at"',context[0]['content'])
        self.assertIn('browser reported that the matching image display loaded',context[0]['content'])

    async def test_reordered_same_candidates_preserve_the_exact_stage_selection_and_prompt_identity(self):
        source = await self.turn('musicians')
        match = source['matches'][1]
        self.assertTrue(self.session.image_status(self.status(match)))
        reordered = [source['matches'][2],source['matches'][0],source['matches'][1]]
        self.snapshot.search = lambda query,history=(): deepcopy(reordered)
        following = await self.turn('Find more musicians')
        identity = {'collection_id':COLLECTION,'id':match['id']}
        self.assertEqual(following['matches'],reordered)
        self.assertEqual(following['gallery_selected'],identity)
        context = json.loads(self.providers.actor[-1]['kwargs']['archive'][1]['content'])
        self.assertEqual(context['gallery_display'],{'status':'loaded','selected':identity,'requested':identity})

    async def test_plural_description_retains_candidates_with_one_explicit_central_selection(self):
        source = await self.turn('musicians')
        match = source['matches'][1]
        self.assertTrue(self.session.image_status(self.status(match)))
        following = await self.turn('What do they look like?')
        self.assertEqual(len(following['matches']),3)
        self.assertEqual(following['gallery_selected'],{'collection_id':COLLECTION,'id':match['id']})

    async def test_unavailable_medium_has_no_substitute_and_does_not_search_old_title(self):
        await self.turn('The Expulsion from Eden')
        searches = len(self.snapshot.searches)
        shown = await self.turn('Can I see the woodcut?')
        self.assertEqual(shown['matches'],[])
        self.assertIs(shown['image_request'],True)
        self.assertEqual(len(self.snapshot.searches),searches)
        self.assertIn('no available source image',self.providers.actor[-1]['kwargs']['archive'][-1]['content'])

    async def test_real_record_shape_without_open_image_keeps_woodcut_and_never_substitutes_eden(self):
        record = musician(4,'Musicians (Musikanten)','woodcut')
        record['image_url'] = None
        record['metadata'] = {'artist':'Wilhelm Grimm','medium':'woodcut','accession_number':'1955.5.21'}
        record['text'] = 'Title: Musicians (Musikanten)\nMedium: woodcut\nAccession number: 1955.5.21'
        self.store.import_records([record],collection_id=COLLECTION,label='National Gallery of Art',source_kind='nga')
        # Reopen the frozen call snapshot after importing the metadata-only work.
        self.session.archive_search.close()
        self.session.archive_search = self.snapshot = ObservedSnapshot(self.store.open_snapshot([COLLECTION]))
        await self.turn('Wilhelm Grimm')
        shown = await self.turn('and I see the wood cut.',spoken=True)
        self.assertEqual([match['title'] for match in shown['matches']],['Musicians (Musikanten)'])
        self.assertIsNone(shown['matches'][0]['image_url'])
        context = self.providers.actor[-1]['kwargs']['archive']
        self.assertIn('none includes an eligible image URL',context[-1]['content'])
        self.assertIn('Accession numbers are museum inventory identifiers, not creation dates',context[0]['content'])
        self.assertFalse(self.session.image_status(self.status(shown['matches'][0])))

    async def test_gallery_identity_feedback_rejects_unknown_sources_and_stale_turns(self):
        source = await self.turn('musicians')
        match = source['matches'][0]
        for selected in ({'collection_id':COLLECTION,'id':'f'*32},
                         {'collection_id':'other-museum','id':match['id']}, [], 'private'):
            event = self.status(match,selected=selected)
            self.assertFalse(self.session.image_status(event))
        self.assertFalse(self.session.image_status(self.status(match,total_count=2)))
        self.assertFalse(self.session.image_status(self.status(match,turn_id=999)))
        self.assertTrue(self.session.image_status(self.status(match)))
        self.assertFalse(self.session.image_status(self.status(match,status='unavailable')))


if __name__=='__main__': unittest.main()
