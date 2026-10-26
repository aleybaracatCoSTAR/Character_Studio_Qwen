"""Realistic streamed dialogue must keep every word through sentence boundaries."""
import random
import re
import unittest

from live_voice.turns import PhraseChunker


REPORTED_REPLY = (
    'I’m okay, just… tired. I called my mom today, like I always do, '
    'but I couldn’t say anything about him. It’s hard, you know? '
    'I love them so much, but I feel so alone.'
)
REPORTED_SENTENCES = [
    'I’m okay, just… tired.',
    'I called my mom today, like I always do, but I couldn’t say anything about him.',
    'It’s hard, you know?',
    'I love them so much, but I feel so alone.',
]


def fragments(text):
    """Whole text, character deltas, every single cut and varied token sizes."""
    yield [text]
    yield list(text)
    for cut in range(len(text) + 1):
        yield [text[:cut], text[cut:]]
    randomizer = random.Random(23)
    for _ in range(120):
        cuts = sorted(randomizer.sample(range(1, len(text)),
                                       randomizer.randint(1, min(30, len(text) - 1))))
        edges = [0, *cuts, len(text)]
        yield [text[start:end] for start, end in zip(edges, edges[1:])]


def chunk(pieces, **options):
    chunker = PhraseChunker(**options)
    phrases = []
    for piece in pieces:
        phrases.extend(chunker.feed(piece))
    phrases.extend(chunker.feed('', final=True))
    return phrases


class PhraseChunkerTests(unittest.TestCase):
    def test_reported_short_sentences_keep_their_opening_words(self):
        sentences=['Just… sitting here.', 'Called my mom.', 'She asked if I’d met anyone.',
                   'I said no.', 'I couldn’t say the truth.', 'Not again.']
        for pieces in fragments(' '.join(sentences)):
            with self.subTest(pieces=pieces):
                self.assertEqual(chunk(pieces,target_chars=200,sentence_only=True),sentences)

    def test_reported_reply_keeps_all_four_complete_sentences_for_any_delta_boundary(self):
        for pieces in fragments(REPORTED_REPLY):
            with self.subTest(pieces=pieces):
                self.assertEqual(chunk(pieces, target_chars=200, sentence_only=True),
                                 REPORTED_SENTENCES)

    def test_ascii_ellipsis_is_a_pause_even_when_its_dots_arrive_separately(self):
        text = 'I know five words so far... This is the final line.'
        for sentence_only in (False, True):
            for pieces in fragments(text):
                with self.subTest(sentence_only=sentence_only, pieces=pieces):
                    self.assertEqual(chunk(pieces, target_chars=200, sentence_only=sentence_only),
                                     [text])

    def test_ascii_and_unicode_hesitation_do_not_split_short_sentences(self):
        for pause in ('...', '…'):
            text = f'I’m okay, just{pause} tired. It’s hard, you know?'
            expected = [f'I’m okay, just{pause} tired.', 'It’s hard, you know?']
            for pieces in fragments(text):
                with self.subTest(pause=pause, pieces=pieces):
                    self.assertEqual(chunk(pieces, target_chars=200, sentence_only=True), expected)

    def test_punctuation_needs_lookahead_but_short_final_sentence_never_hangs(self):
        chunker = PhraseChunker(target_chars=200, sentence_only=True)
        self.assertEqual(chunker.feed('No.'), [])
        self.assertEqual(chunker.feed(' Yes?'), ['No.'])
        self.assertEqual(chunker.feed('', final=True), ['Yes?'])
        self.assertEqual(chunker.feed('', final=True), [])

    def test_closing_quotes_stay_with_the_sentence_as_they_arrive(self):
        text = 'She said “I’m tired.” Then she listened.'
        expected = ['She said “I’m tired.”', 'Then she listened.']
        for pieces in fragments(text):
            with self.subTest(pieces=pieces):
                self.assertEqual(chunk(pieces, target_chars=200, sentence_only=True), expected)

    def test_decimal_and_title_are_preserved_but_year_can_end_a_sentence(self):
        text = 'I paid Dr. Reed 3.14 pounds in 2024. Now I can rest.'
        expected = ['I paid Dr. Reed 3.14 pounds in 2024.', 'Now I can rest.']
        for pieces in fragments(text):
            with self.subTest(pieces=pieces):
                self.assertEqual(chunk(pieces, target_chars=200, sentence_only=True), expected)

    def test_neutral_mode_still_emits_a_complete_clause_early(self):
        chunker = PhraseChunker()
        self.assertEqual(chunker.feed('I am Anna, the washing machine. '),
                         ['I am Anna, the washing machine.'])
        self.assertEqual(chunker.feed('I could help you with that, '),
                         ['I could help you with that,'])

    def test_long_unpunctuated_sentence_is_bounded_without_losing_or_splitting_words(self):
        text = 'these are unusually specific words ' * 20
        for sentence_only in (False, True):
            for pieces in fragments(text):
                with self.subTest(sentence_only=sentence_only, pieces=pieces):
                    phrases = chunk(pieces, target_chars=60, sentence_only=sentence_only)
                    self.assertTrue(all(len(phrase) <= 60 for phrase in phrases))
                    self.assertEqual(' '.join(phrases).split(), text.split())

    def test_long_expressive_sentence_prefers_a_natural_clause_before_word_fallback(self):
        text = ('I have waited here through a very long morning, '
                'and nobody has asked me what I actually need from them.')
        chunker = PhraseChunker(target_chars=60, sentence_only=True)
        phrases = chunker.feed(text) + chunker.feed('', final=True)
        self.assertEqual(phrases[0], 'I have waited here through a very long morning,')
        self.assertEqual(' '.join(phrases).split(), text.split())

    def test_single_oversized_word_and_final_unpunctuated_tail_are_not_truncated(self):
        text = 'Pneumonoultramicroscopicsilicovolcanoconiosis is a long word and I feel alone'
        for pieces in fragments(text):
            with self.subTest(pieces=pieces):
                phrases = chunk(pieces, target_chars=30, sentence_only=True)
                self.assertEqual(' '.join(phrases).split(), text.split())
                self.assertIn('Pneumonoultramicroscopicsilicovolcanoconiosis', phrases)

    def test_all_spoken_characters_survive_varied_splits_and_bounded_fallback(self):
        texts = [REPORTED_REPLY, REPORTED_REPLY.replace('…', '...'),
                 'She asked, “Really?” I said yes! I paid 3.14 pounds. That was in 2024.',
                 'I know five words so far... But this last phrase has no full stop']
        for text in texts:
            expected = re.sub(r'\s+', '', text)
            for sentence_only in (False, True):
                for pieces in fragments(text):
                    with self.subTest(text=text, sentence_only=sentence_only, pieces=pieces):
                        phrases = chunk(pieces, target_chars=85, sentence_only=sentence_only)
                        self.assertEqual(re.sub(r'\s+', '', ''.join(phrases)), expected)

    def test_sentence_at_target_boundary_waits_for_lookahead_instead_of_word_split(self):
        text = 'There are five words here.'
        for pieces in fragments(text + ' Now rest.'):
            with self.subTest(pieces=pieces):
                self.assertEqual(chunk(pieces, target_chars=len(text), sentence_only=True),
                                 [text, 'Now rest.'])


if __name__ == '__main__':
    unittest.main()
