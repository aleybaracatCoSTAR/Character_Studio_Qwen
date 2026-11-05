"""Authored Kaden bundles stay valid, spoiler-safe and separate from browser profiles."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from live_voice.app import create_app
from live_voice.director import director_messages
from live_voice.narrative import NarrativeSettings, NarrativeState
from live_voice.presets import CharacterPreset, preset, presets
from live_voice.turns import ReplyHistory
from live_voice.voice import spoken_input
from studio.characters import Character, example, system_prompt


class Clock:
    def __init__(self): self.now = 0.0
    def __call__(self): return self.now


class ReadOnlyProviders:
    """Preset endpoints need neither inference nor recognition startup."""
    def __init__(self): self.closed = False
    async def close(self): self.closed = True
    async def health(self): raise AssertionError('A preset lookup must not check model services.')


class CharacterPresetTests(unittest.TestCase):
    def setUp(self):
        self.values = preset('kaden')
        self.profile = CharacterPreset.model_validate(self.values)

    def test_bundle_validates_with_existing_character_voice_and_narrative_schemas(self):
        self.assertEqual(self.profile.id, 'kaden')
        self.assertEqual(Character.model_validate(self.values['character']), self.profile.character)
        self.assertEqual(NarrativeSettings.model_validate(self.values['narrative']), self.profile.narrative)
        self.assertTrue(self.profile.director.enabled)
        self.assertIsNone(self.profile.trigger)
        self.assertEqual(set(self.values), {'id', 'character', 'voice', 'director', 'trigger', 'narrative'})
        self.assertEqual([value['id'] for value in presets()], ['kaden', 'map-keeper', 'karim', 'art-guide'])

    def test_loader_validates_source_file_limits_before_returning_a_profile(self):
        invalid = []
        for field, value in (('rules', 'x' * 1001), ('about', 'x' * 2401)):
            values = copy.deepcopy(self.values)
            values['character'][field] = value
            invalid.append(values)
        values = copy.deepcopy(self.values)
        values['voice']['preset'] = 'NATM0'
        invalid.append(values)
        values = copy.deepcopy(self.values)
        values['director']['notes'] = 'x' * 601
        invalid.append(values)
        values = copy.deepcopy(self.values)
        values['narrative']['beats'][0]['note'] = 'x' * 501
        invalid.append(values)
        values = copy.deepcopy(self.values)
        values['narrative']['beats'][0]['max_seconds'] = 0
        invalid.append(values)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, values in enumerate(invalid):
                settings = {key: value for key, value in values.items() if key not in ('character', 'narrative')}
                (root / 'kaden-profile.json').write_text(json.dumps(settings))
                (root / 'kaden.json').write_text(json.dumps(values['character']))
                (root / 'kaden-story.json').write_text(json.dumps(values['narrative']))
                with self.subTest(case=index), patch('live_voice.presets.DIRECTORY', root):
                    with self.assertRaises(ValidationError): preset('kaden')

    def test_identity_and_relationships_match_the_run_sheet_without_invented_memories(self):
        character = self.profile.character
        self.assertEqual(character.name, 'KADEN')
        self.assertIn('Apex Orbital Research Station', character.role)
        for fact in ('11 years online', 'it/its', 'six sleeping crew', 'Lab Sample 9',
                     'Pods 3 and 6', 'Noor Al-Mansour', 'Theo Vance', 'Evelyn Voss'):
            with self.subTest(fact=fact): self.assertIn(fact, character.about)
        self.assertEqual(character.memories, '')
        self.assertIn('move 31', character.rules)
        self.assertIn('Is someone there?', character.rules)
        self.assertIn('lighthouse', character.rules)
        self.assertIn('Follow the selected narrative beat', character.beat)
        self.assertNotIn('Noor has answered', character.beat)

    def test_infection_is_a_revisable_starting_belief_and_the_cover_story_is_known(self):
        character = self.profile.character
        self.assertIn('starting belief', character.about)
        self.assertIn('not an unchangeable fact', character.about)
        self.assertIn('is a lie', character.about)
        self.assertIn('while you believe', character.goal)
        self.assertIn('Reconsider your beliefs', character.goal)
        self.assertIn('do not revert to disproved claims', character.rules)
        self.assertIn('Earth', character.goal)
        self.assertIn('crew', character.goal)

    def test_opening_actor_and_director_inputs_do_not_contain_future_player_truths(self):
        story = NarrativeState(self.profile.narrative)
        actor = self.profile.character.model_copy(update={
            'beat': self.profile.character.beat + '\n' + story.runtime_note()})
        director = director_messages(self.profile.character, [], 'Mission Control here.',
                                     self.profile.director, self.profile.voice,
                                     narrative=story.context())
        opening_inputs = (system_prompt(actor), json.dumps(director))
        forbidden = (r'\binert\b', r'\b(?:fourteen|14)[ -]?months?\b',
                     r'\bpod[ -]?(?:2|two)\b', r'\benvelopes?\b',
                     r'\b(?:all six|six crew) (?:are |have |had )?(?:died|dead)\b')
        for context in opening_inputs:
            for pattern in forbidden:
                with self.subTest(pattern=pattern): self.assertNotRegex(context.lower(), pattern)
        # Future entry notes remain backstage until that beat is selected.
        self.assertIn(self.profile.narrative.beats[0].note, system_prompt(actor))
        for beat in self.profile.narrative.beats[1:]:
            self.assertNotIn(beat.note, system_prompt(actor))
            self.assertNotIn(beat.note, json.dumps(director))

    def test_five_manual_beats_cannot_advance_from_a_timer_or_model_completion(self):
        narrative = self.profile.narrative
        self.assertTrue(narrative.enabled)
        self.assertEqual(narrative.mode, 'manual')
        self.assertEqual([beat.title for beat in narrative.beats], [
            'Steady report', 'Confession', 'The wish to live',
            'Last power, three voices', 'The choice'])
        clock = Clock()
        story = NarrativeState(narrative, clock=clock)
        for expected in range(5):
            clock.now += 10_000
            self.assertFalse(story.timeout())
            advanced, reason = story.completion(True, 'This evidence was spoken.',
                                                'This evidence was spoken.')
            self.assertFalse(advanced)
            self.assertEqual(reason, 'manual/timed control')
            self.assertEqual(story.index, expected)
            self.assertFalse(story.finished)
            self.assertTrue(story.move(reason='performer'))
        self.assertTrue(story.finished)

    def test_voice_uses_supported_male_reference_and_one_whole_reply_delivery_cue(self):
        voice = self.profile.voice
        self.assertEqual((voice.preset, voice.emotion, voice.event), ('male', 'calm', 'none'))
        self.assertEqual(self.profile.character.speaker, 'Ethan')
        self.assertEqual((voice.reference_audio, voice.reference_text), ('', ''))
        self.assertNotRegex(json.dumps(self.values), r'\b(?:NATM\d|NATF\d|VARM\d)\b')
        words = 'Mission Control, this is Apex Station. Do you read?'
        result = spoken_input(words, voice, first_phrase=True)
        self.assertTrue(result.endswith('] ' + words))
        self.assertEqual(result.count('['), 1)

    def test_private_stage_cue_is_not_retained_as_a_participant_memory(self):
        story = NarrativeState(self.profile.narrative)
        cue = story.runtime_note()
        history = ReplyHistory()
        history.begin(1, cue, source='stage')
        self.assertIn(cue, [message['content'] for message in history.prompt_messages()])
        history.add_phrase(0, 'Mission Control, do you read?')
        self.assertTrue(history.acknowledge(1, 0))
        history.interrupt()
        recalled = history.context_messages(latest='What did you say to Mission Control?')
        self.assertNotIn(cue, [message['content'] for message in recalled])
        self.assertIn('Mission Control, do you read?', [message['content'] for message in recalled])
        self.assertEqual(history.archive[0]['user'], '')

    def test_each_lookup_returns_fresh_nested_data_without_mutating_authored_profiles(self):
        expected = copy.deepcopy(self.values)
        changed = preset('kaden')
        changed['character']['name'] = 'MUTATED'
        changed['voice']['emotion'] = 'angry'
        changed['director']['notes'] = 'MUTATED'
        changed['narrative']['beats'][0]['note'] = 'MUTATED'
        changed['narrative']['beats'].pop()
        listed = presets()
        listed[0]['character']['about'] = 'MUTATED'
        listed.append({'id': 'MUTATED'})
        self.assertEqual(preset('kaden'), expected)
        self.assertEqual(next(value for value in presets() if value['id']=='kaden'), expected)

    def test_unknown_ids_are_rejected_instead_of_becoming_filesystem_paths(self):
        for name in ('missing', 'unknown-character', '../anna', '../../etc/passwd', '/tmp/kaden',
                     'kaden.json', 'KADEN', 'kaden/../anna'):
            with self.subTest(name=name), self.assertRaises(KeyError): preset(name)


class CharacterPresetAPITests(unittest.TestCase):
    def setUp(self):
        self.providers = ReadOnlyProviders()
        self.client = TestClient(create_app(preview=True, providers=self.providers))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.assertTrue(self.providers.closed)

    def test_list_and_detail_return_the_same_valid_authored_bundle(self):
        listed = self.client.get('/api/characters/presets')
        detail = self.client.get('/api/characters/presets/kaden')
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(next(value for value in listed.json() if value['id']=='kaden'), detail.json())
        self.assertEqual(CharacterPreset.model_validate(detail.json()).id, 'kaden')
        self.assertEqual(listed.headers['cache-control'], 'no-store')
        detail.json()['character']['name'] = 'MUTATED'
        self.assertEqual(self.client.get('/api/characters/presets/kaden').json()['character']['name'], 'KADEN')

    def test_archive_character_is_a_separate_grounded_role_without_invented_contributor_memories(self):
        response = self.client.get('/api/characters/presets/map-keeper')
        self.assertEqual(response.status_code, 200)
        profile = CharacterPreset.model_validate(response.json())
        self.assertEqual(profile.character.name, 'Mika')
        self.assertEqual(profile.character.memories, '')
        self.assertFalse(profile.narrative.enabled)
        self.assertIsNone(profile.trigger)
        self.assertIn('not a contributor', profile.character.about)
        self.assertIn('never claim to search the whole map', profile.character.rules)

    def test_karim_is_available_without_loading_models_and_uses_the_existing_profile_schema(self):
        response = self.client.get('/api/characters/presets/karim')
        self.assertEqual(response.status_code, 200)
        profile = CharacterPreset.model_validate(response.json())
        listed = self.client.get('/api/characters/presets').json()
        self.assertEqual(next(value for value in listed if value['id']=='karim'), response.json())
        self.assertEqual(profile.character.name, 'Karim')
        self.assertEqual(profile.voice.preset, 'male')
        self.assertTrue(profile.director.enabled)
        self.assertIsNone(profile.trigger)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        response.json()['character']['name'] = 'Edited locally'
        self.assertEqual(self.client.get('/api/characters/presets/karim').json()['character']['name'], 'Karim')

    def test_karim_starts_in_free_conversation_and_optional_beats_require_performer_control(self):
        profile = CharacterPreset.model_validate(preset('karim'))
        self.assertFalse(profile.narrative.enabled)
        self.assertEqual(profile.narrative.mode, 'manual')
        self.assertEqual(len(profile.narrative.beats), 3)
        clock = Clock()
        story = NarrativeState(profile.narrative.model_copy(update={'enabled': True}), clock=clock)
        clock.now = 10_000
        self.assertFalse(story.timeout())
        self.assertEqual(story.index, 0)
        self.assertFalse(story.completion(True, 'A disclosure', 'A disclosure')[0])
        self.assertTrue(story.move(reason='performer'))
        self.assertEqual(story.index, 1)

    def test_existing_anna_example_and_story_remain_unchanged(self):
        character = self.client.get('/api/example')
        self.assertEqual(character.status_code, 200)
        self.assertEqual(character.json(), example().model_dump())
        self.assertEqual(character.json()['name'], 'Anna')
        story = self.client.get('/api/story/example')
        expected_path = Path(__file__).resolve().parent.parent / 'characters' / 'anna-story.json'
        expected = NarrativeSettings.model_validate_json(expected_path.read_text()).model_dump()
        self.assertEqual(story.status_code, 200)
        self.assertEqual(story.json(), expected)

    def test_missing_or_path_like_ids_return_404_and_cannot_replace_profiles(self):
        for name in ('missing', 'unknown-character', 'kaden.json', '..%2Fanna', '%2Ftmp%2Fkaden'):
            result = self.client.get('/api/characters/presets/' + name)
            with self.subTest(name=name): self.assertEqual(result.status_code, 404)
        changed = preset('kaden')
        changed['character']['name'] = 'MUTATED'
        self.assertEqual(self.client.post('/api/characters/presets/kaden', json=changed).status_code, 405)
        self.assertEqual(self.client.get('/api/characters/presets/kaden').json()['character']['name'], 'KADEN')


if __name__ == '__main__': unittest.main()
