"""Image requests use real local retrieval and the normal voice session pipeline."""
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
from test_archive_session import ArchiveProviders


COLLECTION = 'national-gallery-of-art'
PCM = b'\x01\x00' * 160


def artwork(number, artist, title, *, image=True):
    return {'id':f'{number:032x}', 'title':title, 'text':f'A catalogue record for {title} by {artist}.',
            'source_url':f'https://www.nga.gov/artworks/{number}', 'source_entry_id':str(number),
            'image_url':f'https://api.nga.gov/iiif/art-{number}/full/!600,600/0/default.jpg' if image else None,
            'image_alt':title, 'metadata':{'artist':artist, 'date':'1900', 'medium':'Oil on canvas', 'rights':'CC0'}}


class ObservedSnapshot:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.count = snapshot.count
        self.searches = []
        self.closed = False

    def search(self, query, history=()):
        self.searches.append((query, deepcopy(history)))
        return self.snapshot.search(query, history)

    def close(self):
        self.closed = True
        self.snapshot.close()


class ArtworkSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = CollectionDatabase(self.directory.name)
        self.store.import_records([
            artwork(1, 'Claude Monet', 'Water Lilies'),
            artwork(2, 'Pierre-Auguste Renoir', 'Dance in the Garden'),
            artwork(3, 'Jean-Baptiste-Siméon Chardin', 'The House of Cards', image=False)],
            collection_id=COLLECTION, label='National Gallery of Art', source_kind='nga')
        self.providers = ArchiveProviders()
        self.microphone_text = 'Show me the image.'
        async def transcribe(pcm):
            self.providers.stt.calls.append(pcm)
            return self.microphone_text
        self.providers.stt.transcribe = transcribe
        self.sessions = []
        self.events = {}
        self.snapshots = {}

    async def asyncTearDown(self):
        for session in self.sessions: await session.close()
        self.store.close()
        self.directory.cleanup()

    def session(self, *, enabled=True, on_send=None):
        events = []
        async def send_json(event):
            if on_send is not None: await on_send(event)
            events.append(deepcopy(event))
        async def send_bytes(packet): pass
        snapshot = ObservedSnapshot(self.store.open_snapshot([COLLECTION])) if enabled else None
        settings = ArchiveSettings(enabled=enabled, scope='library', collection_ids=[COLLECTION])
        session = LiveSession(self.providers, example(), send_json, send_bytes,
            director=DirectorSettings(enabled=True), archive=settings, archive_search=snapshot)
        self.sessions.append(session)
        self.events[session] = events
        self.snapshots[session] = snapshot
        return session

    async def turn(self, session, text=None, *, pcm=None, action=False):
        start = len(self.events[session])
        await session.begin(text=text, pcm=pcm, action=action)
        await asyncio.wait_for(session.task, 2)
        events = self.events[session][start:]
        self.assertEqual(events[-1]['type'], 'response_done')
        self.assertTrue(session.history.acknowledge(session.turn_id, 0))
        self.assertTrue(session.playback_done(session.turn_id))
        return events

    @staticmethod
    def source(events): return next(event for event in events if event['type'] == 'archive_sources')

    def assert_same_grounding(self, source, index):
        actor = self.providers.actor[index]['kwargs']['archive']
        director = self.providers.director[index]['kwargs']['archive']
        self.assertEqual(actor, director)
        references = json.loads(actor[1]['content'])['references']
        self.assertEqual(references, source['matches'])
        self.assertEqual([value['id'] for value in references], [value['id'] for value in source['matches']])
        return actor

    async def test_typed_show_image_reuses_presented_ids_without_generic_image_search_or_extra_models(self):
        session = self.session()
        initial = self.source(await self.turn(session, 'Tell me about Monet.'))
        self.assertNotIn('image_request', initial)
        self.assertEqual([value['source_entry_id'] for value in initial['matches']], ['1'])
        first_searches = deepcopy(self.snapshots[session].searches)
        shown = self.source(await self.turn(session, 'Show me the image.'))
        self.assertIs(shown['image_request'], True)
        self.assertEqual(shown['matches'], initial['matches'])
        self.assertEqual(self.snapshots[session].searches, first_searches)
        context = self.assert_same_grounding(shown, 1)
        self.assertIn('image', context[0]['content'].lower())
        self.assertEqual(len(context), len(self.providers.actor[0]['kwargs']['archive']) + 1)
        self.assertEqual(context[-1]['role'], 'system')
        self.assertIn('image', context[-1]['content'].lower())
        self.assertEqual((len(self.providers.actor), len(self.providers.director), len(self.providers.voices)), (2, 2, 2))

    async def test_named_artist_image_request_searches_new_topic_and_preserves_exact_image_evidence(self):
        session = self.session()
        await self.turn(session, 'Tell me about Monet.')
        shown = self.source(await self.turn(session, 'Show me an image of Renoir.'))
        self.assertIs(shown['image_request'], True)
        self.assertEqual([value['source_entry_id'] for value in shown['matches']], ['2'])
        self.assertEqual(len(self.snapshots[session].searches), 2)
        self.assertIn('renoir', self.snapshots[session].searches[-1][0].casefold())
        self.assertNotIn('monet', self.snapshots[session].searches[-1][0].casefold())
        self.assert_same_grounding(shown, 1)
        self.assertEqual(shown['matches'][0]['image_url'], 'https://api.nga.gov/iiif/art-2/full/!600,600/0/default.jpg')

    async def test_a_pure_acknowledgement_keeps_the_last_presented_image_without_an_image_search(self):
        session = self.session()
        initial = self.source(await self.turn(session, 'Tell me about Monet.'))
        acknowledgement = self.source(await self.turn(session, 'Thanks!'))
        self.assertEqual(acknowledgement['matches'], [])
        self.assertEqual(session.last_archive_matches, initial['matches'])
        before_image = deepcopy(self.snapshots[session].searches)
        shown = self.source(await self.turn(session, 'Show me the image.'))
        self.assertIs(shown['image_request'], True)
        self.assertEqual(shown['matches'], initial['matches'])
        self.assertEqual(self.snapshots[session].searches, before_image)
        self.assertEqual([query for query, _ in before_image], ['Tell me about Monet.', 'Thanks!'])
        self.assert_same_grounding(shown, 2)
        self.assertEqual((len(self.providers.actor), len(self.providers.director)), (3, 3))

    async def test_cancelled_or_failed_source_publication_never_changes_the_presented_image_cache(self):
        for failure in ('cancel', 'error'):
            with self.subTest(failure=failure):
                blocked = False
                sending = asyncio.Event()
                async def on_send(event):
                    if event['type'] == 'archive_sources' and blocked:
                        sending.set()
                        if failure == 'error': raise RuntimeError('Synthetic source publication failure.')
                        await asyncio.Event().wait()
                session = self.session(on_send=on_send)
                initial = self.source(await self.turn(session, 'Tell me about Monet.'))
                actor_count = len(self.providers.actor)
                blocked = True
                await session.begin(text='Show me an image of Renoir.')
                await asyncio.wait_for(sending.wait(), 2)
                if failure == 'cancel':
                    session.task.cancel()
                    await asyncio.gather(session.task, return_exceptions=True)
                else:
                    await asyncio.wait_for(session.task, 2)
                    self.assertEqual(self.events[session][-1]['type'], 'error')
                self.assertEqual(session.last_archive_matches, initial['matches'])
                self.assertEqual(len(self.providers.actor), actor_count)
                self.assertEqual(len([event for event in self.events[session] if event['type'] == 'archive_sources']), 1)
                blocked = False
                before_image = deepcopy(self.snapshots[session].searches)
                shown = self.source(await self.turn(session, 'Show me the image.'))
                self.assertEqual(shown['matches'], initial['matches'])
                self.assertEqual(self.snapshots[session].searches, before_image)

    async def test_microphone_image_request_takes_the_same_path_once_after_recognition(self):
        session = self.session()
        initial = self.source(await self.turn(session, 'Tell me about Monet.'))
        events = await self.turn(session, pcm=PCM)
        shown = self.source(events)
        self.assertIs(shown['image_request'], True)
        self.assertEqual(shown['matches'], initial['matches'])
        self.assertEqual(self.providers.stt.calls, [PCM])
        self.assertEqual([value['text'] for value in events if value['type'] == 'transcript'], [self.microphone_text])
        self.assertEqual(len(self.snapshots[session].searches), 1)
        self.assert_same_grounding(shown, 1)
        self.assertEqual((len(self.providers.actor), len(self.providers.director)), (2, 2))

    async def test_missing_image_keeps_the_record_and_an_empty_request_never_fabricates_a_match(self):
        session = self.session()
        initial = self.source(await self.turn(session, 'Tell me about Chardin.'))
        self.assertEqual([value['source_entry_id'] for value in initial['matches']], ['3'])
        shown = self.source(await self.turn(session, 'Show me the image.'))
        self.assertIs(shown['image_request'], True)
        self.assertEqual(shown['matches'], initial['matches'])
        self.assertIsNone(shown['matches'][0]['image_url'])
        self.assert_same_grounding(shown, 1)
        fresh = self.session()
        no_prior = self.source(await self.turn(fresh, 'Show me the image.'))
        self.assertIs(no_prior['image_request'], True)
        self.assertEqual(no_prior['matches'], [])
        self.assertEqual(self.snapshots[fresh].searches, [])
        self.assert_same_grounding(no_prior, 2)

    async def test_multiple_presented_artworks_are_reused_together_and_remain_bounded(self):
        self.store.import_records([
            artwork(4, 'Claude Monet', 'A Second Landscape'),
            artwork(5, 'Claude Monet', 'A Third Landscape'),
            artwork(6, 'Claude Monet', 'A Fourth Landscape')],
            collection_id=COLLECTION, label='National Gallery of Art', source_kind='nga')
        session = self.session()
        initial = self.source(await self.turn(session, 'Tell me about Monet.'))
        self.assertEqual(len(initial['matches']), 3)
        shown = self.source(await self.turn(session, 'Show me the images.'))
        self.assertIs(shown['image_request'], True)
        self.assertEqual(shown['matches'], initial['matches'])
        self.assertEqual(len(shown['matches']), 3)
        self.assert_same_grounding(shown, 1)

    async def test_an_unmatched_new_named_request_does_not_resurrect_previous_artwork(self):
        session = self.session()
        await self.turn(session, 'Tell me about Monet.')
        missing = self.source(await self.turn(session, 'Show me an image of Turner.'))
        self.assertIs(missing['image_request'], True)
        self.assertEqual(missing['matches'], [])
        followup = self.source(await self.turn(session, 'Show me the image.'))
        self.assertEqual(followup['matches'], [])
        self.assert_same_grounding(followup, 2)

    async def test_cached_artwork_is_call_local_and_close_disposes_snapshot_and_cache(self):
        first = self.session()
        await self.turn(first, 'Tell me about Monet.')
        self.assertTrue(first.last_archive_matches)
        await first.close()
        self.assertEqual(first.last_archive_matches, [])
        self.assertTrue(self.snapshots[first].closed)
        self.assertIsNone(first.archive_search)
        second = self.session()
        self.assertEqual(second.last_archive_matches, [])
        shown = self.source(await self.turn(second, 'Show me the image.'))
        self.assertEqual(shown['matches'], [])
        self.assert_same_grounding(shown, 1)

    async def test_stage_direction_and_disabled_archive_do_not_become_audience_image_intents(self):
        session = self.session()
        event = self.source(await self.turn(session, 'Show me the image.', action=True))
        self.assertNotIn('image_request', event)
        self.assertEqual(len(self.snapshots[session].searches), 1)
        disabled = self.session(enabled=False)
        events = await self.turn(disabled, 'Show me the image.')
        self.assertFalse(any(event['type'] == 'archive_sources' for event in events))
        self.assertNotIn('archive', self.providers.actor[-1]['kwargs'])


if __name__ == '__main__': unittest.main()
