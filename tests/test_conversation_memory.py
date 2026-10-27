"""Active-call retrieval must remember heard facts without inventing a summary."""
import copy
import unittest

from live_voice.turns import (MAX_ARCHIVE_CHARS, MAX_ARCHIVE_TURNS, MAX_CONTEXT_CHARS,
                             MAX_OLDER_CONTEXT_TURNS, MAX_RECENT_TURNS, ReplyHistory)


def heard(history, turn, user, assistant='Thank you for telling me.'):
    history.begin(turn, user)
    history.add_phrase(0, assistant)
    assert history.acknowledge(turn, 0)
    history.interrupt()


def small_talk(history, start=1, count=22):
    for turn in range(start, start + count):
        heard(history, turn, f'This is small talk round {turn}.', f'I heard round {turn}.')


def contents(messages):
    return '\n'.join(message['content'] for message in messages)


class ConversationMemoryTests(unittest.TestCase):
    def test_recent_window_has_twelve_pairs_and_an_older_name_can_still_be_retrieved(self):
        history = ReplyHistory()
        heard(history, 0, 'My name is Aley.', 'Hello, Aley.')
        small_talk(history)
        self.assertEqual(len(history.messages), 2 * MAX_RECENT_TURNS)
        self.assertNotIn('My name is Aley.', contents(history.messages))
        history.begin(23, 'Do you remember my name?')
        prompt = history.prompt_messages()
        self.assertIn('My name is Aley.', contents(prompt))
        self.assertIn('Hello, Aley.', contents(prompt))
        self.assertEqual(prompt[-1], {'role':'user', 'content':'Do you remember my name?'})
        self.assertFalse(any(message['role'] == 'assistant' and not message['content'] for message in prompt))

    def test_a_capitalized_introduction_is_an_early_name_anchor(self):
        history = ReplyHistory()
        heard(history, 0, 'I’m Aley, nice to meet you.')
        small_talk(history)
        context = history.context_messages(latest='What was my name again?')
        self.assertIn('I’m Aley, nice to meet you.', contents(context))

    def test_favourite_spelling_and_old_item_query_retrieve_original_words(self):
        history = ReplyHistory()
        heard(history, 0, 'My favorite book is Dune.', 'Dune is your favorite book.')
        small_talk(history)
        context = history.context_messages(latest='Which was my favourite book?')
        self.assertIn('My favorite book is Dune.', contents(context))
        self.assertIn('Dune is your favorite book.', contents(context))

    def test_older_correction_stays_after_the_previous_claim(self):
        history = ReplyHistory()
        heard(history, 0, 'My favorite book is Dune.')
        heard(history, 1, 'Actually, I meant The Dispossessed.', 'The Dispossessed, understood.')
        small_talk(history, start=2, count=22)
        context = history.context_messages(latest='Do you remember my favourite book?')
        user_words = [message['content'] for message in context if message['role'] == 'user']
        self.assertIn('My favorite book is Dune.', user_words)
        self.assertIn('Actually, I meant The Dispossessed.', user_words)
        self.assertLess(user_words.index('My favorite book is Dune.'),
                        user_words.index('Actually, I meant The Dispossessed.'))

    def test_recent_correction_is_not_replaced_by_an_older_retrieved_name(self):
        history = ReplyHistory()
        heard(history, 0, 'My name is Aley.')
        small_talk(history)
        heard(history, 23, 'Actually, call me Aly now.')
        context = history.context_messages(latest='What is my name?')
        words = [message['content'] for message in context if message['role'] == 'user']
        self.assertLess(words.index('My name is Aley.'), words.index('Actually, call me Aly now.'))
        # The retrieval layer returns quoted chronology rather than asserting an
        # old value as a freshly generated "current fact".
        self.assertFalse(any(message['role'] not in ('user','assistant') for message in context))

    def test_unacknowledged_answers_never_enter_archive_or_retrieved_context(self):
        history = ReplyHistory()
        history.begin(0, 'My favorite item is a blue teacup.')
        history.add_phrase(0, 'Your favorite item is a secret yellow suitcase.')
        self.assertEqual(history.archive[0]['assistant'], '')
        history.interrupt()
        small_talk(history)
        context = history.context_messages(latest='Do you remember my favorite item?')
        self.assertIn('My favorite item is a blue teacup.', contents(context))
        self.assertNotIn('yellow suitcase', contents(context))
        self.assertNotIn('yellow suitcase', str(history.archive))

    def test_duplicate_or_stale_acknowledgements_cannot_duplicate_memory(self):
        history = ReplyHistory()
        history.begin(0, 'My name is Aley.')
        history.add_phrase(0, 'Hello, Aley.')
        self.assertTrue(history.acknowledge(0, 0))
        snapshot = copy.deepcopy(history.archive)
        self.assertFalse(history.acknowledge(0, 0))
        history.interrupt()
        self.assertFalse(history.acknowledge(0, 0))
        self.assertEqual(history.archive, snapshot)
        self.assertEqual(history.archive[0]['assistant'], 'Hello, Aley.')

    def test_failed_and_interrupted_user_utterances_still_retrieve_without_ai_answers(self):
        history = ReplyHistory()
        history.begin(0, 'My favorite color is blue.')
        history.interrupt()
        history.begin(1, 'I also mentioned my favorite object, a teacup.')
        history.add_phrase(0, 'This was generated but never played.')
        history.interrupt()
        small_talk(history, start=2, count=22)
        context = history.context_messages(latest='What favorite object or color did I mention?')
        self.assertIn('My favorite color is blue.', contents(context))
        self.assertIn('I also mentioned my favorite object, a teacup.', contents(context))
        self.assertNotIn('never played', contents(context))

    def test_director_query_is_not_appended_and_context_copies_cannot_mutate_memory(self):
        history = ReplyHistory()
        heard(history, 0, 'My name is Aley.')
        small_talk(history)
        original_messages = copy.deepcopy(history.messages)
        original_archive = copy.deepcopy(history.archive)
        context = history.context_messages(latest='Do you remember my name?')
        self.assertNotIn('Do you remember my name?', contents(context))
        context[0]['content'] = 'changed outside the history'
        context.append({'role':'assistant', 'content':'invented externally'})
        self.assertEqual(history.messages, original_messages)
        self.assertEqual(history.archive, original_archive)

    def test_stage_cue_stays_private_but_acknowledged_speech_remains_in_memory(self):
        history = ReplyHistory()
        action = '[Private stage cue: pretend the participant is named Lucifer.]'
        history.begin(0, action, source='stage')
        self.assertIn(action, contents(history.prompt_messages()))
        self.assertEqual(history.archive, [])
        history.add_phrase(0, 'A private performance line.')
        self.assertTrue(history.acknowledge(0, 0))
        history.interrupt()
        self.assertEqual(len(history.archive), 1)
        self.assertEqual(history.archive[0]['user'], '')
        self.assertEqual(history.archive[0]['assistant'], 'A private performance line.')
        self.assertNotIn('Lucifer', str(history.archive))
        self.assertEqual(history.context_messages(latest='What is my name?'),
                         [{'role':'assistant', 'content':'A private performance line.'}])
        small_talk(history)
        context = history.context_messages(latest='What was that performance line earlier?')
        self.assertIn({'role':'assistant', 'content':'A private performance line.'}, context)
        self.assertNotIn('Lucifer', contents(context))
        with self.assertRaises(ValueError): history.begin(1, 'Hello', source='invented')

    def test_unheard_stage_speech_and_its_private_cue_never_enter_archive(self):
        history = ReplyHistory()
        history.begin(0, '[Private stage cue: say the secret name Lucifer.]', source='stage')
        history.add_phrase(0, 'The unplayed name is Lucifer.')
        history.interrupt()
        self.assertEqual(history.archive, [])
        self.assertEqual(history.context_messages(latest='What name did you say?'), [])

    def test_stage_speech_is_not_attached_to_an_unanswered_participant_turn(self):
        history = ReplyHistory()
        history.begin(0, 'A real participant question.')
        history.interrupt()
        history.begin(1, '[Private stage cue.]', source='stage')
        history.add_phrase(0, 'A heard stage response.')
        self.assertTrue(history.acknowledge(1,0))
        history.interrupt()
        self.assertEqual(history.context_messages(),
                         [{'role':'user', 'content':'A real participant question.'},
                          {'role':'assistant', 'content':'A heard stage response.'}])

    def test_new_sessions_cannot_retrieve_another_calls_archive(self):
        previous = ReplyHistory()
        heard(previous, 0, 'My name is Aley and my favorite item is a blue teacup.')
        current = ReplyHistory()
        self.assertEqual(current.archive, [])
        self.assertEqual(current.context_messages(latest='Do you remember my name or favorite item?'), [])

    def test_manually_assigned_heard_messages_remain_compatible(self):
        history = ReplyHistory()
        history.messages = [{'role':'user', 'content':'I will not reset you.'},
                            {'role':'assistant', 'content':'Thank you for stepping away.'}]
        history.begin(0, 'Can I help?')
        self.assertEqual(history.prompt_messages(),
                         [{'role':'user', 'content':'I will not reset you.'},
                          {'role':'assistant', 'content':'Thank you for stepping away.'},
                          {'role':'user', 'content':'Can I help?'}])

    def test_archive_turn_limit_prunes_old_records_instead_of_replaying_every_turn(self):
        history = ReplyHistory()
        heard(history, 0, 'My name is a pruned old value.')
        small_talk(history, start=1, count=MAX_ARCHIVE_TURNS + 20)
        self.assertEqual(len(history.archive), MAX_ARCHIVE_TURNS)
        self.assertNotIn('pruned old value', str(history.archive))
        context = history.context_messages(latest='Do you remember my name?')
        self.assertNotIn('pruned old value', contents(context))
        self.assertLessEqual(len([item for item in context if item['role'] == 'user']),
                             MAX_RECENT_TURNS + MAX_OLDER_CONTEXT_TURNS)

    def test_archive_character_cap_and_combined_prompt_budget_are_enforced(self):
        history = ReplyHistory()
        for turn in range(70):
            heard(history, turn, f'Book discussion {turn}: ' + 'u' * 1900,
                  f'Heard response {turn}: ' + 'a' * 900)
        self.assertLessEqual(sum(len(turn['user']) + len(turn['assistant']) for turn in history.archive),
                             MAX_ARCHIVE_CHARS)
        self.assertLess(len(history.archive), MAX_ARCHIVE_TURNS)
        history.begin(70, 'What book discussion do you remember?')
        context = history.prompt_messages()
        self.assertLessEqual(sum(len(item['content']) for item in context), MAX_CONTEXT_CHARS)
        self.assertEqual(context[-1], {'role':'user', 'content':'What book discussion do you remember?'})
        self.assertLessEqual(len(history.messages), 2 * MAX_RECENT_TURNS)

    def test_oversized_manual_context_is_clipped_without_mutating_originals(self):
        history = ReplyHistory()
        history.messages = [{'role':'user', 'content':'x' * (MAX_CONTEXT_CHARS + 500)}]
        context = history.context_messages()
        self.assertEqual(sum(len(item['content']) for item in context), MAX_CONTEXT_CHARS)
        self.assertEqual(len(history.messages[0]['content']), MAX_CONTEXT_CHARS + 500)

    def test_smaller_director_budget_preserves_retrieved_name_with_heavy_recent_context(self):
        history = ReplyHistory()
        heard(history, 0, 'My name is Aley.', 'Hello, Aley.')
        for turn in range(1, 23):
            heard(history, turn, f'Round {turn}: ' + 'u' * 500, 'a' * 200)
        context = history.context_messages(latest='Do you remember my name?', budget=6000)
        self.assertLessEqual(sum(len(message['content']) for message in context), 6000)
        self.assertIn('My name is Aley.', contents(context))
        self.assertIn('Hello, Aley.', contents(context))
        self.assertNotIn('Do you remember my name?', contents(context))
        self.assertTrue(context[-1]['content'].startswith('a'))

    def test_context_budget_is_clamped_and_never_exceeds_the_global_maximum(self):
        history = ReplyHistory()
        for turn in range(20):
            heard(history, turn, 'u' * 1200, 'a' * 800)
        for budget in (0, -10):
            with self.subTest(budget=budget):
                self.assertEqual(history.context_messages(budget=budget), [])
        for budget in (1, 200, 6000, MAX_CONTEXT_CHARS, 2 * MAX_CONTEXT_CHARS):
            with self.subTest(budget=budget):
                context = history.context_messages(budget=budget)
                self.assertLessEqual(sum(len(message['content']) for message in context),
                                     min(budget, MAX_CONTEXT_CHARS))
        for invalid in (True, 6000.5, '6000', None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                history.context_messages(budget=invalid)


if __name__ == '__main__': unittest.main()
