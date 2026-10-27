import unittest

from live_voice.turns import FRAME_BYTES, TurnDetector


def frames(speech, count):
    return (b'\x01\x00' if speech else b'\x00\x00') * (FRAME_BYTES // 2) * count


class TurnHeadstartTests(unittest.TestCase):
    def detector(self):
        return TurnDetector(lambda frame:frame[:2] == b'\x01\x00')

    def test_preparation_does_not_finalize_a_turn_during_a_short_pause(self):
        detector = self.detector()
        events = detector.feed(frames(True, 20) + frames(False, 10))
        self.assertEqual([event[0] for event in events], ['start', 'prepare'])
        self.assertEqual(events[-1][2], 200)
        self.assertTrue(detector.active)
        self.assertEqual(detector.feed(frames(False, 9)), [])
        final = detector.feed(frames(False, 1))
        self.assertEqual(final[0][0], 'utterance')
        self.assertEqual(final[0][2], 400)
        self.assertEqual(events[-1][1], final[0][1])

    def test_resumed_speech_cannot_reuse_the_partial_preparation(self):
        detector = self.detector()
        first = detector.feed(frames(True, 20) + frames(False, 10))
        final = detector.feed(frames(True, 15) + frames(False, 20))
        self.assertEqual([event[0] for event in final], ['utterance'])
        self.assertNotEqual(first[-1][1], final[-1][1])
        self.assertIn(frames(False, 10) + frames(True, 15), final[-1][1])

    def test_preparation_has_the_final_tail_without_adding_wait_silence(self):
        detector = self.detector()
        events = detector.feed(frames(False, 12) + frames(True, 20) + frames(False, 20))
        prepared, final = events[-2:]
        self.assertEqual(prepared[1], final[1])
        self.assertTrue(final[1].endswith(frames(True, 1) + frames(False, 4)))
        self.assertFalse(final[1].endswith(frames(False, 5)))

    def test_new_utterance_can_prepare_after_the_previous_one(self):
        detector = self.detector()
        audio = frames(True, 20) + frames(False, 20)
        self.assertEqual([event[0] for event in detector.feed(audio)], ['start','prepare','utterance'])
        self.assertEqual([event[0] for event in detector.feed(audio)], ['start','prepare','utterance'])

    def test_short_noise_never_starts_speculative_recognition(self):
        events = self.detector().feed(frames(True, 3) + frames(False, 25))
        self.assertEqual([event[0] for event in events], ['start','noise'])


if __name__ == '__main__':
    unittest.main()
