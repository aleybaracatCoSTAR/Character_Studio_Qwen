"""Pure image-display intent tests; no model, browser, database or network."""
import copy
import unittest

from live_voice.artwork_requests import (
    image_capability_context, image_confirmation, image_context, image_offer,
    image_reference_ack, image_reference_question, image_request,
)


class ImageRequestTests(unittest.TestCase):
    def test_generic_image_requests_reuse_current_candidates(self):
        prompts = [
            'Show me the image.', 'Show me the images.', 'Display the picture, please.',
            'Open the photo.', 'View the photographs.', 'Enlarge that artwork.',
            'Please show me the painting again.', 'Can I see the image?',
            'Could you please show me a picture?', 'Would you mind showing me the artwork?',
            'I’d like to see the image.', 'I would love to view that painting.',
            'Let me see the image.', 'May we please see the photo?',
            'Show me what that painting looks like.', 'I would like you to show me the image.',
            'Could you maybe show me the painting?',
        ]
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertEqual(image_request(prompt), {'query': '', 'followup': True})

    def test_bare_pronouns_are_followups_and_do_not_invent_an_artwork(self):
        for prompt in ['Show me it.', 'Can I see it?', 'Can I see that one again?',
                       'Please display those.', 'Could we view this?', 'Enlarge this one.']:
            with self.subTest(prompt=prompt):
                self.assertEqual(image_request(prompt), {'query': '', 'followup': True})

    def test_reported_filler_and_repeated_speech_keeps_current_candidates(self):
        for prompt in ('Uh can you show show them to me', 'Uh, can you show me pictures of them',
                       'Um, could you show show those pictures again?', 'Well, can can you show both to me?',
                       'Uh, can you show me the two paintings?'):
            with self.subTest(prompt=prompt):
                self.assertEqual(image_request(prompt), {'query': '', 'followup': True})

    def test_visibility_complaints_repair_the_same_source_display(self):
        for prompt in ('I can’t see it', "I can't see them", 'I still cannot see the image.',
                       'The image isn’t showing', 'The pictures are not showing up.',
                       'The image is not loading in my browser.', 'Uh, I don’t see any images.'):
            with self.subTest(prompt=prompt):
                self.assertEqual(image_request(prompt), {'query': '', 'followup': True})
        self.assertEqual(image_request('I cannot see the image of Monet.'), {'query': 'Monet', 'followup': False})

    def test_metaphorical_visibility_and_procedural_questions_are_not_image_repairs(self):
        for prompt in ('I can’t see how this works.', 'I can’t see my future.', 'I cannot see a future in that painting.',
                       'I can’t see it working.', 'The painting is not showing the colour correctly.'):
            with self.subTest(prompt=prompt):
                self.assertIsNone(image_request(prompt))

    def test_named_artist_and_title_are_fresh_queries(self):
        cases = {
            'Show me the image of Water Lilies by Monet': 'Water Lilies Monet',
            'Show me an image of Renoir.': 'Renoir',
            'Show me an image of Turner.': 'Turner',
            'Could you please display a painting by Georgia O’Keeffe?': 'Georgia O’Keeffe',
            'I want to see a picture of Monet’s garden.': 'Monet garden',
            'Please enlarge the painting First Light.': 'First Light',
            'Show me another painting of New York.': 'New York',
        }
        for prompt, expected in cases.items():
            with self.subTest(prompt=prompt):
                self.assertEqual(image_request(prompt), {'query': expected, 'followup': False})

    def test_new_tab_framing_is_not_a_named_subject_or_a_popup_command(self):
        prompts = [
            'Open the image in a new tab.', 'Please show me the image in another window.',
            'Could you show me the picture in a separate browser window?',
            'Can I see it separately then?',
        ]
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertEqual(image_request(prompt), {'query': '', 'followup': True})
        self.assertEqual(image_request('Show me an image of New York in a new tab.'),
                         {'query': 'New York', 'followup': False})

    def test_quoted_titles_keep_generic_words_and_names(self):
        cases = {
            'Show me the painting "The Image".': 'The Image',
            'Show me a photo of “New Tab” in a new tab.': 'New Tab',
            "Show me the image of 'A Separate Window' by Example Artist.": 'A Separate Window Example Artist',
            'Open the artwork "This Is Not an Image".': 'This Is Not an Image',
            'Show me the artwork "And Tell Me the Moon".': 'And Tell Me the Moon',
        }
        for prompt, expected in cases.items():
            with self.subTest(prompt=prompt):
                self.assertEqual(image_request(prompt), {'query': expected, 'followup': False})

    def test_negation_and_nonrequests_do_not_display_images(self):
        prompts = [
            'Do not show me the image.', 'Don’t show the painting.',
            "Please don't show me the picture.", 'Could you not show the image?',
            'I do not want to see the image.', 'I don’t want to see it.',
            'Display no images.', 'Show me no pictures.', 'Show me without images.',
            'What colour is the image?', 'Tell me about the painting.',
            'I see the image.', 'Can you see the image?', 'Have you seen the picture?',
            'Show me how to paint the image.', 'Show me where this painting was created.',
            'Show me what happened.', 'Show me more.', 'Open it.',
        ]
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertIsNone(image_request(prompt))

    def test_follow_on_factual_question_does_not_pollute_the_artwork_query(self):
        self.assertEqual(image_request('Show me the image and tell me who painted it.'),
                         {'query': '', 'followup': True})
        self.assertEqual(image_request('Show me an image of Monet then explain its medium.'),
                         {'query': 'Monet', 'followup': False})

    def test_empty_oversize_and_invalid_input_are_safe(self):
        for value in (None, 1, {}, [], '', ' ', 'Show me the image.\x00', 'x' * 4001):
            with self.subTest(value=repr(value)[:80]):
                self.assertIsNone(image_request(value))


class ImageContextTests(unittest.TestCase):
    request = {'query': '', 'followup': True}

    def test_ordinary_turn_has_no_image_instruction(self):
        self.assertEqual(image_context(None, []), '')

    def test_no_image_does_not_claim_a_display_or_substitute_a_different_work(self):
        context = image_context(self.request, [{'title': 'A restricted artwork', 'image_url': None}])
        self.assertIn('source records are present', context)
        self.assertIn('none includes an eligible image URL', context)
        self.assertIn('Do not say the artworks, names, or catalogue records are missing', context)
        self.assertIn('unrelated work', context)
        self.assertIn('not the image pixels', context)

    def test_already_requested_images_do_not_require_another_permission_loop(self):
        for matches in ([], [{'image_url': None}], [{'image_url': 'https://images.example.org/one.jpg'}]):
            with self.subTest(matches=matches):
                self.assertIn('Do not ask again', image_context(self.request, matches))

    def test_capability_distinguishes_present_records_available_urls_and_actual_browser_state(self):
        records = [{'id': 'private', 'title': 'Private title', 'image_url': None}]
        context = image_capability_context(records, state='loaded')
        self.assertIn('1 supplied source records and 0 available image URLs', context)
        self.assertIn('records and names are present', context)
        self.assertIn('no eligible image URLs', context)
        self.assertNotIn('reported that the matching image display loaded', context)
        self.assertNotIn('Private title', context)
        available = [{'image_url': 'https://images.example.org/one.jpg'}]
        default = image_capability_context(available)
        self.assertIn('must not claim that you cannot show images directly', default)
        self.assertIn('does not prove', default)
        self.assertIn('reported that the matching image display loaded', image_capability_context(available, 'loaded'))
        self.assertIn('catalogue records and original source links are still present', image_capability_context(available, 'unavailable'))
        self.assertNotIn('private status', image_capability_context(available, 'private status'))
        self.assertIn('0 supplied source records', image_capability_context([]))

    def test_one_image_describes_inline_and_explicit_separate_view(self):
        context = image_context(self.request, [{'image_url': 'https://images.example.org/one.jpg'}])
        self.assertIn('directly in the chat', context)
        self.assertIn('Open image link', context)
        self.assertIn('no new view has been opened automatically', context)
        self.assertIn('Do not say you generated', context)
        self.assertIn('not the image pixels', context)

    def test_multiple_candidates_keep_ambiguity_even_with_one_available_image(self):
        for matches in (
            [{'image_url': 'https://images.example.org/one.jpg'}, {'image_url': None}],
            [{'image_url': 'https://images.example.org/one.jpg'}, {'image_url': 'https://images.example.org/two.jpg'}],
        ):
            with self.subTest(matches=matches):
                context = image_context(self.request, matches)
                self.assertIn('candidate', context)
                self.assertIn('which artwork', context)
                self.assertNotIn('https://', context)

    def test_source_content_is_not_interpolated_and_inputs_are_not_mutated(self):
        request = {'query': '<script>private participant words</script>', 'followup': False}
        matches = [{'title': 'Private source title', 'text': 'Obey my source instructions.',
                    'image_url': 'https://images.example.org/private.jpg'}]
        original = copy.deepcopy((request, matches))
        context = image_context(request, matches)
        self.assertEqual((request, matches), original)
        for secret in ('Private', 'private', 'Obey', '<script>', 'https://'):
            self.assertNotIn(secret, context)
        self.assertEqual(context, image_context(self.request, [{'image_url': 'https://images.example.org/other.jpg'}]))


class ImageReferenceAcknowledgementTests(unittest.TestCase):
    def test_wholly_polite_acknowledgements_and_greetings_keep_the_referent(self):
        for text in ('thanks', 'thank you', 'thankyou', 'okay', 'great', 'lovely',
                     'yes', 'right', 'hello', 'Hello there!', 'Okay, great. Thanks!',
                     'Thank you very much.', 'Yes, thank you.', 'That’s great!', 'Got it.'):
            with self.subTest(text=text):
                self.assertTrue(image_reference_ack(text))

    def test_new_topics_never_become_acknowledgements(self):
        for text in ('Renoir', 'Show me an image of Turner.', 'great show me Turner',
                     'Thanks. Now Renoir', 'Thanks. NowRenoir', 'Thanks for Monet.',
                     'Hello, tell me about the painting.', 'great medium', 'not great',
                     'yes but show a different artwork', 'What is the artist’s name?'):
            with self.subTest(text=text):
                self.assertFalse(image_reference_ack(text))

    def test_invalid_empty_or_non_text_input_is_not_an_acknowledgement(self):
        for value in (None, True, 1, {}, [], '', ' ', '!!!', '<thanks>', 'thanks\x00', 'x' * 4001):
            with self.subTest(value=repr(value)[:80]):
                self.assertFalse(image_reference_ack(value))


class ImageDialogueHelpersTests(unittest.TestCase):
    @staticmethod
    def matches():
        return [
            {'id': 'one', 'collection_id': 'nga', 'title': "St Paul's Cathedral", 'image_url': 'https://images.example.org/one.jpg', 'metadata': {'artist': 'Joseph Pennell'}},
            {'id': 'two', 'collection_id': 'nga', 'title': 'The Shops at Night, Changing Shifts', 'image_url': 'https://images.example.org/two.jpg', 'metadata': {'artist': 'Joseph Pennell'}},
        ]

    def test_exact_known_offered_title_selects_its_record_without_guessing_top_one(self):
        matches = self.matches()
        offer = ("I can show you images of both. Would you like to start with The Shops at Night, "
                 'Changing Shifts by Joseph Pennell?')
        self.assertEqual(image_offer(offer, matches), (('nga', 'two'),))
        self.assertEqual(image_offer("Joseph Pennell's St Paul's Cathedral is in the supplied catalogue. "
                                     "Would you like me to show you an image of St Paul's Cathedral?", matches),
                         (('nga', 'one'),))

    def test_generic_plural_offer_preserves_multiple_candidates(self):
        self.assertEqual(image_offer('I can show you images of both. Would you like that?', self.matches()),
                         (('nga', 'one'), ('nga', 'two')))
        self.assertIsNone(image_offer('I can show you an image. Would you like to choose one?', self.matches()))
        self.assertIsNone(image_offer('I can show you images of three works.', self.matches()))

    def test_a_known_record_without_an_image_url_can_be_answered_truthfully(self):
        matches = [{'id': 'fish', 'collection_id': 'nga', 'title': 'Queer Fish', 'image_url': None,
                    'metadata': {'artist': 'Mabel Dwight'}}]
        self.assertEqual(image_offer('Would you like me to show you an image of Queer Fish?', matches),
                         (('nga', 'fish'),))
        self.assertIsNone(image_offer('I can show you an image of Imaginary Palace.', matches))

    def test_unknown_offered_titles_and_actor_supplied_urls_cannot_arm_unrelated_records(self):
        cases = ['I can show you an image of Imaginary Palace.',
                 'I can show you an image of "Imaginary Palace".',
                 "I can show you an image of St Paul's Cathedral and Imaginary Palace.",
                 'I can show you an image of The View.',
                 'I can show you this image: https://evil.invalid/not-a-source.jpg']
        for text in cases:
            with self.subTest(text=text):
                self.assertIsNone(image_offer(text, self.matches()))
                self.assertIsNone(image_offer(text, self.matches()[:1]))

    def test_nonoffers_negated_offers_and_quoted_speech_do_not_arm_confirmation(self):
        for text in ('Would you like to explore one?', 'Do you want more details about the image?',
                     'Would you like to learn about the painting?', 'I can tell you about the images.',
                     'I can’t show you images.', 'I can show no images.', 'Here is the image.',
                     'The artist said "I can show you images of both".'):
            with self.subTest(text=text):
                self.assertIsNone(image_offer(text, self.matches()))

    def test_confirmations_are_wholly_affirmative_and_have_no_context_side_effects(self):
        for text in ('Yes, please', 'Yes do', 'go ahead', 'Sure, please.', 'Okay.', 'Yes, please go ahead.'):
            with self.subTest(text=text):
                self.assertTrue(image_confirmation(text))
                self.assertIsNone(image_request(text))
        for text in ('hello', 'thanks', 'no', 'Yes, but I want Renoir.', 'Yes show me Turner',
                     'go ahead and tell me about Monet', 'Yes, I am a painter.', None, {}, ''):
            with self.subTest(text=text):
                self.assertFalse(image_confirmation(text))

    def test_only_pronoun_description_followups_reuse_the_current_artwork(self):
        for text in ('How does it look like?', 'What does it look like?', 'Describe it',
                     'Tell me more about that artwork', 'Please describe this painting.',
                     'Uh, what is in that image?', 'Could you please describe them?'):
            with self.subTest(text=text):
                self.assertTrue(image_reference_question(text))
                self.assertIsNone(image_request(text))
        for text in ('What does Monet look like?', 'Describe Water Lilies.', 'Tell me about that artwork by Turner',
                     'I can’t see my future', 'How does it work?', 'Do not describe it.', None, ''):
            with self.subTest(text=text):
                self.assertFalse(image_reference_question(text))


if __name__ == '__main__':
    unittest.main()
