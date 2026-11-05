"""Artwork follow-ups preserve source evidence without borrowing a new topic."""
import json
import tempfile
import unittest

from live_voice.collections import CollectionDatabase, collection_grounding_messages, _query_terms


def artwork(number, artist, date, medium):
    return {'id':f'{number:032x}', 'title':f'Catalogue work {number}',
            'text':f'An imported catalogue description for work {number}.',
            'source_url':f'https://www.nga.gov/artworks/{number}', 'source_entry_id':str(number),
            'image_url':f'https://api.nga.gov/iiif/art-{number}/full/!200,200/0/default.jpg',
            'image_alt':f'Catalogue artwork {number}',
            'metadata':{'artist':artist, 'date':date, 'medium':medium, 'rights':'CC0'}}


class ArtworkFollowupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = CollectionDatabase(self.directory.name)
        self.store.import_records([
            artwork(1, 'Claude Monet', '1867', 'Oil on canvas'),
            artwork(2, 'Pierre-Auguste Renoir', '1870', 'Oil on canvas')],
            collection_id='national-gallery-of-art', label='National Gallery of Art', source_kind='nga')
        self.snapshot = self.store.open_snapshot(['national-gallery-of-art'])
        self.history = [{'role':'user', 'content':'Show me a Monet.'},
                        {'role':'assistant', 'content':'The catalogue has a matching work by Claude Monet.'}]

    def tearDown(self):
        self.snapshot.close()
        self.store.close()
        self.directory.cleanup()

    def test_date_medium_and_artist_followups_retain_the_complete_source_and_image_record(self):
        initial = self.snapshot.search('Show me a Monet.')
        self.assertEqual([hit['source_entry_id'] for hit in initial], ['1'])
        for question in ('When was it painted?', 'What medium is it?', 'Who is the artist?',
                         'What year was this created?', 'Who painted that?', 'What year?', "What's the medium?"):
            with self.subTest(question=question):
                hits = self.snapshot.search(question, self.history)
                self.assertEqual(hits, initial)
                references = json.loads(collection_grounding_messages(hits)[1]['content'])['references']
                self.assertEqual(references[0]['metadata'], initial[0]['metadata'])
                self.assertEqual(references[0]['image_url'], initial[0]['image_url'])
                self.assertEqual(references[0]['source_url'], initial[0]['source_url'])

    def test_a_series_of_facet_questions_does_not_replace_the_meaningful_artist_query(self):
        history = list(self.history)
        for question in ('When was it painted?', 'What medium is it?', 'Who is the artist?',
                         'What year?', 'Tell me more.'):
            with self.subTest(question=question):
                hits = self.snapshot.search(question, history)
                self.assertEqual([hit['source_entry_id'] for hit in hits], ['1'])
                history += [{'role':'user', 'content':question}, {'role':'assistant', 'content':'A catalogue detail.'}]

    def test_named_new_artist_or_named_facet_remains_an_independent_search(self):
        for question in ('Now show me Renoir.', 'Actually, show me Renoir.', 'Who is the artist Renoir?',
                         'What medium does Renoir use?'):
            with self.subTest(question=question):
                hits = self.snapshot.search(question, self.history)
                self.assertEqual([hit['source_entry_id'] for hit in hits], ['2'])
        updated = self.history + [{'role':'user', 'content':'Now show me Renoir.'},
                                  {'role':'assistant', 'content':'Here is another catalogue record.'}]
        self.assertEqual([hit['source_entry_id'] for hit in self.snapshot.search('When was it painted?', updated)], ['2'])

    def test_unrelated_topics_and_pixel_questions_do_not_retrieve_old_artwork_by_pronoun_alone(self):
        for question in ('What about architecture?', 'Who is my mother?', 'When was it reset?',
                         'What colour is it?', 'Now show me Turner.'):
            with self.subTest(question=question): self.assertEqual(self.snapshot.search(question, self.history), [])
        # "Work" itself occurs in both records. It remains a fresh lexical
        # search rather than becoming an implicit Monet reference.
        self.assertEqual(_query_terms('How does it work?', self.history), {'work'})

    def test_no_previous_artwork_gives_no_invented_reference_and_history_is_bounded(self):
        for question in ('When was it painted?', 'What medium is it?', 'Who is the artist?'):
            with self.subTest(question=question): self.assertEqual(self.snapshot.search(question), [])
        outside_window = self.history + [{'role':'assistant', 'content':'A later unrelated reply.'} for _ in range(24)]
        self.assertEqual(self.snapshot.search('When was it painted?', outside_window), [])

    def test_multiple_matching_works_are_retained_as_ambiguous_evidence(self):
        self.store.import_records([artwork(3, 'Claude Monet', '1880', 'Pastel on paper')],
            collection_id='national-gallery-of-art', label='National Gallery of Art', source_kind='nga')
        fresh = self.store.open_snapshot(['national-gallery-of-art'])
        try:
            hits = fresh.search('When was it painted?', self.history)
            self.assertEqual({hit['source_entry_id'] for hit in hits}, {'1', '3'})
            self.assertEqual({hit['metadata']['date'] for hit in hits}, {'1867', '1880'})
            self.assertEqual(len(json.loads(collection_grounding_messages(hits)[1]['content'])['references']), 2)
        finally: fresh.close()


if __name__ == '__main__': unittest.main()
