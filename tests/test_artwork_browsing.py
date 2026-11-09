"""Call-local browsing intent survives fillers and never invents visual facts."""
import copy
import unittest

from live_voice.artwork_browsing import ArtworkBrowsing, MAX_KEYS, MAX_QUERY


def record(number, **changes):
    return {'collection_id':'gallery', 'id':f'{number:032x}', 'title':f'Painting {number}',
            'metadata':{'medium':'oil on canvas'}, **changes}


def key(number): return ('gallery', f'{number:032x}')


class ArtworkBrowsingTests(unittest.TestCase):
    def colorful(self):
        state = ArtworkBrowsing()
        plan = state.plan('Do you have any colorful paintings?')
        self.assertEqual((plan.query, plan.medium), ('colorful', 'painting'))
        return state

    def test_original_topic_is_meaningful_query_with_separate_medium(self):
        state = self.colorful()
        self.assertTrue(state.active)
        self.assertFalse(state.require_images)
        self.assertEqual(state.query, 'colorful')
        self.assertEqual(state.medium, 'painting')
        self.assertEqual(state.failed_keys, ())

    def test_spoken_polite_framing_does_not_become_catalogue_search_words(self):
        state = ArtworkBrowsing()
        plan = state.plan("I'm not sure to be honest, but I'm interested in colorful paintings.")
        self.assertEqual((plan.query, plan.medium), ('colorful', 'painting'))
        self.assertEqual(state.plan('Tell me about Monet.').query, 'Monet')

    def test_misspelled_painting_medium_keeps_the_filter_and_leaves_only_the_subject(self):
        for medium in ('paintngs', 'paintigns', 'paintng', 'paitings'):
            with self.subTest(medium=medium):
                state = ArtworkBrowsing()
                plan = state.plan(f'Show me colorful {medium}.')
                self.assertEqual((plan.query, plan.medium, plan.require_images), ('colorful', 'painting', True))
        printing = ArtworkBrowsing().plan('Show me colorful printings.')
        self.assertEqual((printing.query, printing.medium), ('colorful', 'print'))
        for title in ('Pointing', 'Fainting'):
            plan = ArtworkBrowsing().plan(title)
            self.assertEqual((plan.query, plan.medium), (title, ''))

    def test_more_with_pictures_keeps_topic_and_medium_and_excludes_seen_page(self):
        state = self.colorful()
        state.observe([record(1), record(2), record(3)], key(1))
        plan = state.plan('Can you find more artworks with pictures?')
        self.assertEqual((plan.query, plan.medium), ('colorful', 'painting'))
        self.assertEqual(plan.reason, 'more')
        self.assertTrue(plan.require_images)
        self.assertTrue(plan.fresh_search)
        self.assertEqual(plan.exclude_keys, frozenset((key(1), key(2), key(3))))

    def test_status_followups_and_hesitations_keep_exhausted_search_without_resurrecting_page(self):
        state = self.colorful()
        state.observe([record(1), record(2), record(3)], key(1))
        issued = state.plan('Show me another colorful painting with an image.')
        state.observe([])
        for text in ('Any luck?', 'Any luck in that?', 'Did you find anything?', 'Have you found anything yet?',
                     'Um', 'Uh...', 'Um, uh…'):
            with self.subTest(text=text):
                plan = state.plan(text)
                self.assertEqual((plan.query, plan.medium, plan.require_images), ('colorful', 'painting', True))
                self.assertFalse(plan.fresh_search)
                self.assertEqual(plan.exclude_keys, issued.exclude_keys)
                self.assertEqual(plan.reason, 'hesitation' if text.startswith(('Um', 'Uh')) else 'status')

    def test_bare_filler_or_status_cannot_start_browsing(self):
        state = ArtworkBrowsing()
        for text in ('Um', 'Uh...', 'Any luck?', 'Did you find anything?'):
            self.assertIsNone(state.plan(text))
        self.assertFalse(state.active)
        self.assertEqual((state.query, state.medium, state.failed_keys), ('', '', ()))

    def test_only_selected_unavailable_image_is_excluded(self):
        for text in ('No, it is not available.', "I don't think it's available.", 'I do not think it is available.',
                     "It isn't available.", 'That one is not available', 'None are available.',
                     'There are no images available.'):
            with self.subTest(text=text):
                state = self.colorful()
                candidates = [record(1), record(2), record(3)]
                state.observe(candidates, key(2))
                plan = state.plan(text, candidates, selected_key=key(2))
                self.assertEqual(plan.reason, 'availability')
                self.assertTrue(plan.fresh_search)
                self.assertTrue(plan.require_images)
                self.assertEqual(plan.query, 'colorful')
                self.assertEqual(plan.medium, 'painting')
                self.assertEqual(plan.exclude_keys, frozenset((key(2),)))

    def test_unavailable_without_selection_never_marks_all_candidates_failed(self):
        state = self.colorful()
        candidates = [record(1), record(2), record(3)]
        state.observe(candidates)
        plan = state.plan('None are available.', candidates)
        self.assertEqual(plan.exclude_keys, frozenset())
        self.assertEqual(state.failed_keys, ())

    def test_positive_available_feedback_never_marks_an_image_failed(self):
        for text in ('It is available.', "It's available.", 'The image is available.', 'That one is available.'):
            with self.subTest(text=text):
                state = self.colorful()
                state.observe([record(1)], key(1))
                plan = state.plan(text, [record(1)], selected_key=key(1))
                self.assertTrue(plan is None or plan.reason != 'availability')
                self.assertEqual(state.failed_keys, ())

    def test_browser_failure_and_success_are_local_and_reversible(self):
        state = self.colorful()
        state.observe([record(1), record(2)], key(1))
        state.fail(key(1))
        plan = state.plan('Any luck?')
        self.assertEqual(plan.exclude_keys, frozenset((key(1),)))
        state.restore(key(1))
        self.assertEqual(state.plan('Any luck?').exclude_keys, frozenset())
        other = self.colorful()
        self.assertEqual(other.failed_keys, ())

    def test_restoring_failed_image_does_not_erase_more_page_exclusion(self):
        state = self.colorful()
        state.observe([record(1), record(2)], key(1))
        state.fail(key(1))
        issued = state.plan('Show me more paintings.')
        state.restore(key(1))
        self.assertEqual(state.failed_keys, ())
        self.assertEqual(state.plan('Any luck?').exclude_keys, issued.exclude_keys)

    def test_another_cycles_forward_across_multiple_returned_pages(self):
        state = self.colorful()
        state.observe([record(1), record(2), record(3)])
        state.plan('Show me more.')
        state.observe([record(4), record(5), record(6)])
        plan = state.plan('Another one, please.')
        self.assertEqual(plan.exclude_keys, frozenset(key(number) for number in range(1, 7)))
        self.assertEqual((plan.query, plan.medium), ('colorful', 'painting'))

    def test_image_constraint_and_medium_survive_generic_artwork_followups(self):
        state = self.colorful()
        first = state.plan('Show me more artworks with pictures.')
        second = state.plan('Find another artwork, please.')
        self.assertEqual((first.query, first.medium, first.require_images), ('colorful', 'painting', True))
        self.assertEqual((second.query, second.medium, second.require_images), ('colorful', 'painting', True))

    def test_new_subject_changes_query_and_explicit_new_medium_replaces_painting(self):
        state = self.colorful()
        plan = state.plan('Show me landscapes.')
        self.assertEqual((plan.query, plan.medium), ('landscapes', 'painting'))
        prints = state.plan('Prints instead.')
        self.assertEqual((prints.query, prints.medium), ('landscapes', 'print'))
        watercolors = state.plan('Another watercolour, please.')
        self.assertEqual((watercolors.query, watercolors.medium), ('landscapes', 'watercolor'))
        any_medium = state.plan('Any medium instead.')
        self.assertEqual((any_medium.query, any_medium.medium), ('landscapes', ''))

    def test_named_or_quoted_titles_are_not_hesitations_or_statuses(self):
        state = self.colorful()
        for text, expected in (
            ('Find the artwork called Um.', 'Um'),
            ('Find the artwork called Any Luck.', 'Any Luck'),
            ('Show me the artwork "Um".', 'Um'),
            ('Find the painting "More".', 'More'),
            ('Find the artwork called Not Available.', 'Not Available'),
            ('Find the artwork called "Colorful Paintings".', 'Colorful Paintings'),
        ):
            with self.subTest(text=text):
                plan = state.plan(text)
                self.assertEqual(plan.query, expected)
                self.assertEqual(plan.reason, 'topic')
                self.assertTrue(plan.fresh_search)

    def test_artist_lucky_is_a_query_not_an_any_luck_status(self):
        state = self.colorful()
        for text in ('Lucky', 'Show me paintings by Lucky.', 'Find an artwork by Lucky.'):
            with self.subTest(text=text):
                plan = state.plan(text)
                self.assertEqual(plan.query, 'Lucky')
                self.assertEqual(plan.reason, 'topic')

    def test_generic_image_referents_and_current_medium_selection_defer_existing_helpers(self):
        state = self.colorful()
        candidates = [record(1), record(2, metadata={'medium':'woodcut'})]
        for text in ('Show me it.', 'Can I see the image?', 'Can I see the woodcut?', 'Show me the picture again.',
                     'and I see the wood cut.'):
            with self.subTest(text=text):
                self.assertIsNone(state.plan(text, candidates))
                self.assertEqual((state.query, state.medium), ('colorful', 'painting'))

    def test_exact_known_title_with_no_image_defers_selection_without_image_only_filter(self):
        state = self.colorful()
        candidates = [record(1, title='Musicians (Musikanten)', image_url=None)]
        self.assertIsNone(state.plan('Show me the painting Musicians Musikanten.', candidates))
        self.assertFalse(state.require_images)
        self.assertEqual((state.query, state.medium), ('colorful', 'painting'))

    def test_rejected_display_title_correction_defers_to_existing_wanted_half_parser(self):
        state = self.colorful()
        text = "you're showing me the expulsion from Eden not the music one"
        self.assertIsNone(state.plan(text, [record(1)]))
        self.assertEqual((state.query, state.medium), ('colorful', 'painting'))

    def test_more_about_current_work_does_not_become_another_artwork_search(self):
        state = self.colorful()
        for text in ('Tell me more about that painting.', 'Can you give me more information about it?',
                     'Thanks.', 'Yes, please.', 'Okay.', 'What does it look like?', 'Describe that painting.'):
            self.assertIsNone(state.plan(text))
            self.assertEqual(state.query, 'colorful')

    def test_colour_words_remain_query_text_without_visual_claims_or_model_inference(self):
        for word in ('colourful', 'colorful', 'vibrant', 'multicolored', 'polychrome'):
            state = ArtworkBrowsing()
            plan = state.plan(f'Show me {word} paintings.')
            self.assertEqual(plan.query, word)
            self.assertEqual(plan.medium, 'painting')
            self.assertFalse(hasattr(plan, 'description'))

    def test_failed_key_memory_and_every_exclusion_plan_are_bounded(self):
        state = self.colorful()
        for number in range(MAX_KEYS + 20):
            state.observe([record(number)], key(number))
            state.fail(key(number))
        plan = state.plan('Show me another painting.')
        self.assertEqual(len(state.seen_keys), MAX_KEYS)
        self.assertEqual(len(state.failed_keys), MAX_KEYS)
        self.assertEqual(len(plan.exclude_keys), MAX_KEYS)
        self.assertNotIn(key(0), plan.exclude_keys)

    def test_invalid_untrusted_feedback_and_source_objects_do_not_mutate_inputs(self):
        state = self.colorful()
        candidates = [record(1), {'collection_id':[], 'id':{}}, None]
        original = copy.deepcopy(candidates)
        state.observe(candidates, key(1))
        state.fail(None)
        state.fail(('gallery', []))
        plan = state.plan('Another painting.', candidates, failed_keys=[None, ('a', []), key(2)])
        self.assertEqual(candidates, original)
        self.assertEqual(plan.exclude_keys, frozenset((key(1), key(2))))
        self.assertEqual(state.failed_keys, (key(2),))

    def test_invalid_or_empty_utterances_do_not_replace_the_subject(self):
        state = self.colorful()
        for value in (None, [], {}, 1, '', ' ', 'show me\x00 a painting', 'x' * 4001):
            with self.subTest(value=repr(value)[:30]):
                self.assertIsNone(state.plan(value))
                self.assertEqual(state.query, 'colorful')

    def test_negated_search_is_not_inverted_into_a_positive_browse(self):
        state = self.colorful()
        for text in ('Do not show me paintings.', "I don't want more pictures.",
                     'I am not interested in sculptures.'):
            self.assertIsNone(state.plan(text))
            self.assertEqual((state.query, state.medium), ('colorful', 'painting'))

    def test_explicit_without_pictures_does_not_turn_on_an_image_requirement(self):
        state = self.colorful()
        self.assertIsNone(state.plan('Show me paintings without pictures.'))
        self.assertFalse(state.require_images)
        self.assertEqual((state.query, state.medium), ('colorful', 'painting'))

    def test_long_named_query_is_bounded_without_storing_source_bodies(self):
        state = ArtworkBrowsing()
        plan = state.plan('Find the artwork called ' + ('Long title ' * 100))
        self.assertEqual(len(plan.query), MAX_QUERY)
        state.observe([record(1, text='PRIVATE SOURCE BODY')])
        self.assertNotIn('PRIVATE SOURCE BODY', repr(state))


if __name__ == '__main__': unittest.main()
