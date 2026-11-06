"""Natural image dialogue stays grounded in published, call-local artwork records."""
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
from test_artwork_session import COLLECTION, ObservedSnapshot, artwork


PENNELL_OFFER = ("Joseph Pennell's St Paul's Cathedral is in the supplied catalogue. "
                 "Would you like me to show you an image of St Paul's Cathedral?")
ACKNOWLEDGEMENT = 'The available artwork information is supplied beside this reply.'


class DialogueProviders(ArchiveProviders):
    def __init__(self):
        super().__init__()
        self.replies = []
        self.recognized_text = ''
        async def transcribe(pcm):
            self.stt.calls.append(pcm)
            return self.recognized_text
        self.stt.transcribe = transcribe

    async def text(self, character, history, **kwargs):
        self.actor.append({'character':character.model_dump(), 'history':deepcopy(history), 'kwargs':deepcopy(kwargs)})
        yield self.replies.pop(0) if self.replies else ACKNOWLEDGEMENT


class ArtworkDialogueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = CollectionDatabase(self.directory.name)
        self.store.import_records([
            artwork(1, 'Joseph Pennell', "St Paul's Cathedral"),
            artwork(2, 'Joseph Pennell', 'Waterloo Bridge'),
            artwork(3, 'Claude Monet', 'Water Lilies'),
            artwork(4, 'Restricted Artist', 'A Work Without an Image', image=False)],
            collection_id=COLLECTION, label='National Gallery of Art', source_kind='nga')
        self.providers = DialogueProviders()
        self.sessions, self.events, self.snapshots = [], {}, {}

    async def asyncTearDown(self):
        for session in self.sessions: await session.close()
        self.store.close()
        self.directory.cleanup()

    def session(self, on_send=None):
        events = []
        async def send_json(event):
            if on_send is not None: await on_send(event)
            events.append(deepcopy(event))
        async def send_bytes(packet): pass
        snapshot = ObservedSnapshot(self.store.open_snapshot([COLLECTION]))
        session = LiveSession(self.providers, example(), send_json, send_bytes,
            director=DirectorSettings(enabled=True), archive_search=snapshot,
            archive=ArchiveSettings(enabled=True, scope='library', collection_ids=[COLLECTION]))
        self.sessions.append(session); self.events[session] = events; self.snapshots[session] = snapshot
        return session

    async def turn(self, session, text, *, reply=None, audio=False, failure=False):
        if reply is not None: self.providers.replies.append(reply)
        offset = len(self.events[session])
        if audio:
            self.providers.recognized_text = text
            pcm = b'\x01\x00' * (160 + session.turn_id)
            await session.begin(pcm=pcm, endpoint_ms=400)
        else:
            await session.begin(text=text)
        await asyncio.wait_for(session.task, 2)
        events = self.events[session][offset:]
        self.assertEqual(events[-1]['type'], 'error' if failure else 'response_done')
        if not failure:
            self.assertTrue(session.history.acknowledge(session.turn_id, 0))
            self.assertTrue(session.playback_done(session.turn_id))
        return events

    def source(self, events):
        source = next(event for event in events if event['type'] == 'archive_sources')
        actor = self.providers.actor[-1]['kwargs']['archive']
        director = self.providers.director[-1]['kwargs']['archive']
        self.assertEqual(actor, director)
        self.assertEqual(json.loads(actor[1]['content'])['references'], source['matches'])
        return source

    async def offer_confirm_and_repair(self, *, audio):
        session = self.session()
        offered = self.source(await self.turn(session, 'Tell me about Joseph Pennell.', reply=PENNELL_OFFER, audio=audio))
        self.assertEqual({match['source_entry_id'] for match in offered['matches']}, {'1', '2'})
        before = deepcopy(self.snapshots[session].searches)
        confirmed = self.source(await self.turn(session, 'Yes, please.', audio=audio))
        self.assertIs(confirmed.get('image_request'), True)
        self.assertEqual([match['source_entry_id'] for match in confirmed['matches']], ['1'])
        self.assertTrue(confirmed['matches'][0]['image_url'])
        self.assertEqual(self.snapshots[session].searches, before)
        repaired = self.source(await self.turn(session, 'I can’t see it.', audio=audio))
        self.assertIs(repaired.get('image_request'), True)
        self.assertEqual(repaired['matches'], confirmed['matches'])
        self.assertEqual(self.snapshots[session].searches, before)
        self.assertNotIn('no available source image', self.providers.actor[-1]['kwargs']['archive'][-1]['content'])
        self.assertEqual((len(self.providers.actor), len(self.providers.director), len(self.providers.voices)), (3, 3, 3))
        self.assertEqual(len(self.providers.stt.calls), 3 if audio else 0)

    async def test_typed_yes_to_a_specific_offered_title_and_visibility_repair_show_the_real_image(self):
        await self.offer_confirm_and_repair(audio=False)

    async def test_microphone_yes_and_visibility_repair_follow_the_same_source_path_once(self):
        await self.offer_confirm_and_repair(audio=True)

    async def test_user_exact_stuttered_request_shows_current_images_without_searching_the_stutter(self):
        session = self.session()
        first = self.source(await self.turn(session, 'Tell me about Joseph Pennell.',
            reply='Two works by Joseph Pennell are present in the catalogue.'))
        before = deepcopy(self.snapshots[session].searches)
        shown = self.source(await self.turn(session, 'Uh can you show show them to me'))
        self.assertIs(shown.get('image_request'), True)
        self.assertEqual(shown['matches'], first['matches'])
        self.assertEqual(self.snapshots[session].searches, before)
        self.assertEqual((len(self.providers.actor), len(self.providers.director)), (2, 2))

    async def test_recognized_stuttered_request_uses_final_speech_and_does_not_add_a_model_call(self):
        session = self.session()
        first = self.source(await self.turn(session, 'Tell me about Joseph Pennell.'))
        events = await self.turn(session, 'Uh can you show show them to me', audio=True)
        shown = self.source(events)
        self.assertIs(shown.get('image_request'), True)
        self.assertEqual(shown['matches'], first['matches'])
        self.assertEqual(len(self.providers.stt.calls), 1)
        self.assertEqual(len(self.snapshots[session].searches), 1)
        self.assertEqual((len(self.providers.actor), len(self.providers.director)), (2, 2))

    async def test_yes_without_an_image_offer_does_not_become_a_display_command(self):
        session = self.session()
        await self.turn(session, 'Tell me about Joseph Pennell.', reply='The catalogue has two works by Joseph Pennell.')
        confirmed = self.source(await self.turn(session, 'Yes, please.'))
        self.assertNotIn('image_request', confirmed)

    async def test_new_named_topic_invalidates_an_old_offer_before_an_unrelated_yes(self):
        session = self.session()
        await self.turn(session, 'Tell me about Joseph Pennell.', reply=PENNELL_OFFER)
        monet = self.source(await self.turn(session, 'Tell me about Monet.', reply='The catalogue includes Water Lilies by Monet.'))
        self.assertEqual([match['source_entry_id'] for match in monet['matches']], ['3'])
        yes = self.source(await self.turn(session, 'Yes, please.'))
        self.assertNotIn('image_request', yes)
        self.assertFalse(any(match['source_entry_id'] == '1' for match in yes['matches']))

    async def test_a_visibility_complaint_with_no_eligible_image_is_honest_and_does_not_substitute(self):
        session = self.session()
        first = self.source(await self.turn(session, 'Tell me about Restricted Artist.',
            reply='The catalogue has A Work Without an Image.'))
        self.assertEqual([match['source_entry_id'] for match in first['matches']], ['4'])
        missing = self.source(await self.turn(session, 'I can’t see it.'))
        self.assertIs(missing.get('image_request'), True)
        self.assertEqual(missing['matches'], first['matches'])
        self.assertTrue(all(match['image_url'] is None for match in missing['matches']))
        context = self.providers.actor[-1]['kwargs']['archive'][-1]['content']
        self.assertIn('matched source records are present', context)
        self.assertIn('none includes an eligible image URL', context)
        self.assertIn('Do not say the artworks, names, or catalogue records are missing', context)
        self.assertEqual(len(self.snapshots[session].searches), 1)

    async def test_three_queer_art_catalogue_records_remain_text_only_through_filler_request_and_repair(self):
        self.store.import_records([
            artwork(5, 'Leon Bibel', 'Queer Birds', image=False),
            artwork(6, 'Norman Kent', 'Queer House', image=False),
            artwork(7, 'Mabel Dwight', 'Queer Fish', image=False)],
            collection_id=COLLECTION, label='National Gallery of Art', source_kind='nga')
        session = self.session()
        original = self.source(await self.turn(session, 'Tell me about queer art.',
            reply='The catalogue has Queer Birds, Queer House and Queer Fish, with metadata but no attached images.'))
        self.assertEqual(len(original['matches']), 3)
        self.assertEqual({match['title'] for match in original['matches']}, {'Queer Birds', 'Queer House', 'Queer Fish'})
        self.assertTrue(all(match['collection_id'] == COLLECTION and match['image_url'] is None for match in original['matches']))
        for utterance in ('Uh can you show show them to me', 'I can’t see them.'):
            with self.subTest(utterance=utterance):
                shown = self.source(await self.turn(session, utterance))
                self.assertIs(shown.get('image_request'), True)
                self.assertEqual(shown['matches'], original['matches'])
                self.assertTrue(all(match['image_url'] is None for match in shown['matches']))
                context = self.providers.actor[-1]['kwargs']['archive'][-1]['content']
                self.assertIn('matched source records are present', context)
                self.assertIn('none includes an eligible image URL', context)
                self.assertIn('Do not say the artworks, names, or catalogue records are missing', context)
        self.assertEqual(len(self.snapshots[session].searches), 1)
        self.assertEqual((len(self.providers.actor), len(self.providers.director)), (3, 3))

    async def test_a_visible_offer_survives_voice_failure_without_becoming_heard_memory(self):
        session = self.session()
        self.providers.voice_failures = 1
        events = await self.turn(session, 'Tell me about Joseph Pennell.', reply=PENNELL_OFFER, failure=True)
        self.assertEqual([event['text'] for event in events if event['type'] == 'text_done'], [PENNELL_OFFER])
        self.assertEqual(session.history.messages[-1]['content'], '')
        shown = self.source(await self.turn(session, 'Yes, please.'))
        self.assertIs(shown.get('image_request'), True)
        self.assertEqual([match['source_entry_id'] for match in shown['matches']], ['1'])
        self.assertEqual(session.history.archive[0]['assistant'], '')
        self.assertEqual(len(self.snapshots[session].searches), 1)

    async def test_new_call_does_not_inherit_a_previous_calls_pending_offer(self):
        first = self.session()
        await self.turn(first, 'Tell me about Joseph Pennell.', reply=PENNELL_OFFER)
        await first.close()
        second = self.session()
        yes = self.source(await self.turn(second, 'Yes, please.'))
        self.assertNotIn('image_request', yes)
        self.assertEqual(yes['matches'], [])

    async def test_an_invented_offered_title_or_actor_url_cannot_select_a_real_source_image(self):
        for reply in (
            'Would you like me to show you an image of Imaginary Tower by Joseph Pennell?',
            'Would you like me to show you an image at https://example.org/invented.jpg?',
        ):
            with self.subTest(reply=reply):
                session = self.session()
                await self.turn(session, 'Tell me about Joseph Pennell.', reply=reply)
                self.assertEqual(session.pending_image_offer, [])
                yes = self.source(await self.turn(session, 'Yes, please.'))
                self.assertNotIn('image_request', yes)
                self.assertTrue(all(match.get('image_url') != 'https://example.org/invented.jpg' for match in yes['matches']))

    async def test_an_offer_whose_completed_text_was_not_published_cannot_arm_confirmation(self):
        block = True
        async def on_send(event):
            if event['type'] == 'text_done' and block:
                raise RuntimeError('Synthetic completed-text publication failure.')
        session = self.session(on_send=on_send)
        events = await self.turn(session, 'Tell me about Joseph Pennell.', reply=PENNELL_OFFER, failure=True)
        self.assertFalse(any(event['type'] == 'text_done' for event in events))
        self.assertEqual(session.pending_image_offer, [])
        self.assertEqual(self.providers.voices, [])
        block = False
        yes = self.source(await self.turn(session, 'Yes, please.'))
        self.assertNotIn('image_request', yes)


if __name__ == '__main__': unittest.main()
