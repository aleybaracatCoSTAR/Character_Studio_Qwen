"""Pure image-display intent tests; no model, browser, database or network."""
import copy
import unittest

from live_voice.artwork_requests import image_context, image_reference_ack, image_request


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
        self.assertIn('no available source image', context)
        self.assertIn('Do not claim an image is being shown', context)
        self.assertIn('unrelated work', context)
        self.assertIn('not the image pixels', context)

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


if __name__ == '__main__':
    unittest.main()
