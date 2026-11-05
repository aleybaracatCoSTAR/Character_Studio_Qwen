"""Speech rendering removes presentation syntax, never archive story words."""
import unittest

from live_voice.voice import spoken_text_clean
from live_voice.voice_coverage import compare_speech


PASSAGE = (
    'They wrote: *"I didn’t order anything. Just sat by the window, watching rain blur the street. '
    'The coffee stayed hot, like someone knew I’d need it."* '
    'The barista later left a small note on the table: *"You’re welcome to stay as long as you like."* '
    'That’s all it said. Did something like that ever happen to you?'
)
PLAIN_PASSAGE = (
    'They wrote: "I didn’t order anything. Just sat by the window, watching rain blur the street. '
    'The coffee stayed hot, like someone knew I’d need it." '
    'The barista later left a small note on the table: "You’re welcome to stay as long as you like." '
    'That’s all it said. Did something like that ever happen to you?'
)


class SpokenTextTests(unittest.TestCase):
    def test_reported_long_quote_keeps_all_61_normalized_words_and_its_quotations(self):
        cleaned = spoken_text_clean(PASSAGE)
        self.assertEqual(cleaned, PLAIN_PASSAGE)
        report = compare_speech(PASSAGE, cleaned)
        self.assertEqual((report.expected_words, report.heard_words, report.matched_words), (61,61,61))
        self.assertEqual(cleaned.count('"'), PASSAGE.count('"'))
        self.assertIn('I didn’t order anything.', cleaned)
        self.assertIn('You’re welcome to stay as long as you like.', cleaned)
        self.assertTrue(cleaned.endswith('Did something like that ever happen to you?'))

    def test_single_double_and_triple_emphasis_wrappers_only_remove_the_markers(self):
        for marker in ('*','**','***','_','__','___'):
            original = f'One {marker}very steady voice{marker}, then listen.'
            with self.subTest(marker=marker):
                self.assertEqual(spoken_text_clean(original), 'One very steady voice, then listen.')

    def test_paired_presentation_around_quoted_words_preserves_quotes_and_apostrophes(self):
        original = 'She said **“I’m here.”** Then _"Don’t go."_ He replied, *\'I won’t.\'*'
        self.assertEqual(spoken_text_clean(original), 'She said “I’m here.” Then "Don’t go." He replied, \'I won’t.\'')

    def test_nested_spaced_emphasis_is_cleaned_without_reordering_words(self):
        self.assertEqual(spoken_text_clean('A *quiet **very steady** voice*.'), 'A quiet very steady voice.')

    def test_inline_code_preserves_its_literal_contents_while_removing_backticks(self):
        for marker in ('`','``','```'):
            with self.subTest(marker=marker):
                self.assertEqual(spoken_text_clean(f'The value is {marker}2*3 + snake_case{marker}.'),
                                 'The value is 2*3 + snake_case.')
        self.assertEqual(spoken_text_clean('Read `*literal*` and `__name__` exactly.'),
                         'Read *literal* and __name__ exactly.')
        self.assertEqual(spoken_text_clean('Read **`*literal*`** exactly.'), 'Read *literal* exactly.')

    def test_literal_math_identifiers_footnotes_and_unmatched_markers_are_unchanged(self):
        cases = [
            'The result is 2*3, x*y, 2 * 3, and 3**2.',
            'Keep snake_case and file_name.txt unchanged.',
            'A note[^1], another[^long_note], and [^1]: the footnote text.',
            'A grade* and * a footnote marker, without presentation pairs.',
            '* unfinished emphasis and ** still unfinished',
            'This has an unmatched ` and another unmatched ``.',
            r'Escaped \*literal\*, \_literal\_, and \`literal\` markers.',
            '* List item\n* Next item',
            'https://example.invalid/some_path and [a link](https://example.invalid).',
            '```python\nprint(2*3)\n```',
        ]
        for original in cases:
            with self.subTest(original=original):
                self.assertEqual(spoken_text_clean(original), original)

    def test_hebrew_arabic_emoji_and_unicode_punctuation_survive_cleaning(self):
        original = 'היא אמרה: *“שלום עולם”* — **مرحبا بالعالم**. Then _🌈✨_ and café.'
        expected = 'היא אמרה: “שלום עולם” — مرحبا بالعالم. Then 🌈✨ and café.'
        self.assertEqual(spoken_text_clean(original), expected)
        self.assertEqual(spoken_text_clean('עברית_כאן 2*3 العربية_هنا 🌈*✨'), 'עברית_כאן 2*3 العربية_هنا 🌈*✨')

    def test_plain_text_whitespace_and_literal_sentinel_characters_are_preserved(self):
        original = '  Plain\twords, “quotes,”\nnew lines and café.  '
        self.assertEqual(spoken_text_clean(original), original)
        self.assertEqual(spoken_text_clean('\ue000 Keep `*literal*` and **bold**. \ue000\ue000'),
                         '\ue000 Keep *literal* and bold. \ue000\ue000')

    def test_missing_or_ambiguous_pairs_are_not_guessed(self):
        for original in ('word*emphasis*word', 'word_emphasis_word', '* padded *',
                         '****ambiguous****', '**mismatched*', '` padded `'):
            with self.subTest(original=original):
                self.assertEqual(spoken_text_clean(original), original)

    def test_cleaning_does_not_add_spoken_words_and_rejects_nontext(self):
        original = 'A **specific** name, _Ali_, and `song_title` are still the same words.'
        report = compare_speech(original, spoken_text_clean(original))
        self.assertEqual(report.expected_words, report.heard_words)
        self.assertEqual(report.matched_words, report.expected_words)
        self.assertEqual(spoken_text_clean(''), '')
        for invalid in (None, b'text', 123):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError): spoken_text_clean(invalid)


if __name__ == '__main__': unittest.main()
