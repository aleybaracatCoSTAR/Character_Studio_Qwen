"""Real catalogue browsing keeps the subject across image retries and short speech."""
import asyncio
from copy import deepcopy
import json
import tempfile
import unittest

from live_voice.archive import ArchiveSettings
from live_voice.collections import CollectionDatabase
from live_voice.director import DirectorSettings
from live_voice.session import LiveSession
from studio.characters import example
from test_artwork_dialogue import DialogueProviders
from test_artwork_session import COLLECTION, ObservedSnapshot, artwork


def identity(match): return (match['collection_id'], match['id'])


def colorful_records():
    missing = artwork(1, 'Fixture painter', 'Colorful', image=False)
    missing['text'] = 'Colorful painting. Colorful catalogue record.'
    valid = []
    for number in range(2, 8):
        item = artwork(number, 'Fixture painter', f'Quiet light {number}')
        item['metadata']['medium'] = 'oil on canvas'
        # These explicit source descriptions allow a colour-evidence fallback;
        # neither the title nor its oil medium proves any visible colours.
        item['image_alt'] = f'Red and blue geometric shapes, catalogue image {number}.'
        item['text'] = f'Painting catalogue description {number}. Red and blue geometric shapes.'
        valid.append(item)
    drawing = artwork(8, 'Fixture draughtsperson', 'Colorful drawing')
    drawing['metadata']['medium'] = 'graphite on paper'
    drawing['metadata']['classification'] = 'Drawing'
    drawing['text'] = 'Colorful drawing. Red and blue catalogue reference.'
    luck = artwork(9, 'Fixture artist', "Fisherman's Luck")
    luck['text'] = 'A catalogue record for the title Fisherman\'s Luck.'
    um = artwork(10, 'Fixture artist', 'Um')
    um['text'] = 'A catalogue record for the artwork titled Um.'
    return [missing, *valid, drawing, luck, um]


class FilterObservedSnapshot(ObservedSnapshot):
    def __init__(self, snapshot):
        super().__init__(snapshot)
        self.filtered_searches = []

    def search(self, query, history=(), **filters):
        self.filtered_searches.append({'query':query, 'history':deepcopy(history), 'filters':deepcopy(filters)})
        return super().search(query, history, **filters)


class BrowsingSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = CollectionDatabase(self.directory.name)
        self.store.import_records(colorful_records(), collection_id=COLLECTION,
            label='National Gallery of Art', source_kind='nga')
        self.providers = DialogueProviders()
        self.snapshot = FilterObservedSnapshot(self.store.open_snapshot([COLLECTION]))
        self.events = []
        async def send_json(value): self.events.append(deepcopy(value))
        async def send_bytes(value): pass
        self.session = LiveSession(self.providers, example(), send_json, send_bytes,
            archive=ArchiveSettings(enabled=True, scope='library', collection_ids=[COLLECTION]),
            archive_search=self.snapshot, director=DirectorSettings(enabled=True), gallery_greeting=True)

    async def asyncTearDown(self):
        await self.session.close()
        self.store.close()
        self.directory.cleanup()

    async def turn(self, text, *, spoken=False):
        offset = len(self.events)
        if spoken:
            self.providers.recognized_text = text
            await self.session.begin(pcm=b'\x01\x00' * (160 + self.session.turn_id), endpoint_ms=400)
        else:
            await self.session.begin(text=text)
        await asyncio.wait_for(self.session.task, 2)
        events = self.events[offset:]
        self.assertEqual(events[-1]['type'], 'response_done')
        self.assertTrue(self.session.history.acknowledge(self.session.turn_id, 0))
        self.assertTrue(self.session.playback_done(self.session.turn_id))
        source = next(event for event in events if event['type']=='archive_sources')
        actor = self.providers.actor[-1]['kwargs']['archive']
        self.assertEqual(actor, self.providers.director[-1]['kwargs']['archive'])
        self.assertEqual(json.loads(actor[1]['content'])['references'], source['matches'])
        return source, actor

    def status(self, match, *, status='unavailable'):
        return {'type':'image_status', 'turn_id':self.session.turn_id, 'status':status,
                'loaded_count':1 if status=='loaded' else 0,
                'total_count':int(bool(match.get('image_url'))),
                'selected':{'collection_id':match['collection_id'], 'id':match['id']}}

    def assert_painting_images(self, matches):
        self.assertTrue(matches)
        for match in matches:
            self.assertIsInstance(match['image_url'], str)
            self.assertEqual(match['metadata']['medium'], 'oil on canvas')
            self.assertNotIn(match['title'], ('Colorful drawing', "Fisherman's Luck", 'Um'))

    async def reported_dialogue(self, *, spoken):
        original = "I'm not sure to be honest, but I'm interested in colorful paintings."
        first, _ = await self.turn(original, spoken=spoken)
        # The image-less exact lexical match outranks colour-description-only
        # paintings before an explicit image constraint is requested.
        self.assertEqual([match['title'] for match in first['matches']], ['Colorful'])
        self.assertIsNone(first['matches'][0]['image_url'])
        first_key = identity(first['matches'][0])
        more, context = await self.turn('Can you show me more artworks with pictures?', spoken=spoken)
        self.assert_painting_images(more['matches'])
        self.assertEqual(len(more['matches']), 3)
        self.assertNotIn(first_key, {identity(match) for match in more['matches']})
        search = self.snapshot.filtered_searches[-1]
        self.assertEqual(search['query'], 'colorful')
        self.assertTrue(search['filters']['require_images'])
        self.assertEqual(search['filters']['medium'], 'painting')
        self.assertIn(first_key, search['filters']['exclude_keys'])
        self.assertIn('No background catalogue search continues', context[-1]['content'])
        self.assertIn('do not promise to look later', context[-1]['content'])
        self.assertIn('Do not infer colours', context[-1]['content'])
        count = len(self.snapshot.filtered_searches)
        status, status_context = await self.turn('Any luck?', spoken=spoken)
        self.assertEqual(len(self.snapshot.filtered_searches), count)
        self.assertEqual(status['matches'], more['matches'])
        self.assertEqual(json.loads(status_context[1]['content'])['browsing']['intent'], 'status')
        self.assertNotIn("Fisherman's Luck", {match['title'] for match in status['matches']})
        failed = status['matches'][1]
        self.assertTrue(self.session.image_status(self.status(failed)))
        negative, negative_context = await self.turn("I don't think it's available.", spoken=spoken)
        self.assert_painting_images(negative['matches'])
        self.assertEqual(len(self.snapshot.filtered_searches), count + 1)
        negative_filter = self.snapshot.filtered_searches[-1]['filters']
        self.assertEqual(negative_filter['exclude_keys'], frozenset((identity(failed),)))
        self.assertNotIn(identity(failed), {identity(match) for match in negative['matches']})
        self.assertEqual(json.loads(negative_context[1]['content'])['browsing']['intent'], 'availability')
        earlier = {identity(match) for source in (first, more, status, negative) for match in source['matches']}
        another, another_context = await self.turn('Show me another colorful painting.', spoken=spoken)
        self.assert_painting_images(another['matches'])
        self.assertFalse(earlier & {identity(match) for match in another['matches']})
        issued = self.snapshot.filtered_searches[-1]
        self.assertTrue(issued['filters']['require_images'])
        self.assertEqual((issued['query'], issued['filters']['medium']), ('colorful', 'painting'))
        count = len(self.snapshot.filtered_searches)
        filler, filler_context = await self.turn('Um', spoken=spoken)
        self.assertEqual(len(self.snapshot.filtered_searches), count)
        self.assertEqual(filler['matches'], another['matches'])
        self.assertNotIn('image_request', filler)
        self.assertEqual(json.loads(filler_context[1]['content'])['browsing']['intent'], 'hesitation')
        self.assertIn('do not recommend a new artwork', filler_context[-1]['content'])
        self.assertTrue(all(search['query']=='colorful' for search in self.snapshot.filtered_searches))
        self.assertEqual(len(self.providers.stt.calls), 6 if spoken else 0)

    async def test_reported_typed_dialogue_advances_actual_image_paintings_without_losing_topic(self):
        await self.reported_dialogue(spoken=False)

    async def test_reported_recognized_dialogue_has_identical_browsing_and_image_constraints(self):
        await self.reported_dialogue(spoken=True)

    async def test_metadata_only_selected_zero_image_feedback_excludes_just_that_record(self):
        first, _ = await self.turn('colorful paintings')
        selected = first['matches'][0]
        self.assertIsNone(selected['image_url'])
        self.assertTrue(self.session.image_status(self.status(selected)))
        self.assertEqual(self.session.artwork_browsing.failed_keys, (identity(selected),))
        following, _ = await self.turn('No, it is not available.')
        self.assert_painting_images(following['matches'])
        self.assertEqual(self.snapshot.filtered_searches[-1]['filters']['exclude_keys'], frozenset((identity(selected),)))

    async def test_user_unavailable_report_without_browser_error_uses_current_requested_selection_only(self):
        first, _ = await self.turn('Show me colorful paintings with images.')
        self.assert_painting_images(first['matches'])
        selected = first['gallery_selected']
        key = (selected['collection_id'], selected['id'])
        following, _ = await self.turn('No, it is not available.')
        self.assertEqual(self.snapshot.filtered_searches[-1]['filters']['exclude_keys'], frozenset((key,)))
        self.assertNotIn(key, {identity(match) for match in following['matches']})
        self.assertEqual(self.session.artwork_browsing.failed_keys, (key,))

    async def test_exhausted_more_does_not_resurrect_seen_images_on_um_or_any_luck(self):
        await self.turn('Show me colorful paintings with images.')
        for _ in range(5):
            source, _ = await self.turn('Another one with a picture, please.')
            if not source['matches']: break
        self.assertEqual(source['matches'], [])
        searches = len(self.snapshot.filtered_searches)
        filler, _ = await self.turn('Um...')
        status, context = await self.turn('Any luck in that?')
        self.assertEqual(filler['matches'], [])
        self.assertEqual(status['matches'], [])
        self.assertEqual(len(self.snapshot.filtered_searches), searches)
        browsing = json.loads(context[1]['content'])['browsing']
        self.assertEqual((browsing['query'], browsing['medium'], browsing['matching_records']), ('colorful', 'painting', 0))
        self.assertFalse(browsing['search_completed'])
        self.assertIn('No background catalogue search continues', context[-1]['content'])

