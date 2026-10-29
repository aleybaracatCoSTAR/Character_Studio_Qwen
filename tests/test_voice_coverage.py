"""Word coverage catches obvious omissions without asserting semantic equality."""
from dataclasses import FrozenInstanceError
import json
import unittest

import numpy as np

from live_voice.voice_coverage import (MAX_EXPECTED_WORDS, MAX_HEARD_WORDS, MAX_PCM_BYTES,
                                      compare_speech, resample_for_recognition, spoken_text)


REPLY = ('That’s what I keep telling myself. But it still hurts—like I’m breaking a part of me '
         'every time I hang up. Do you think it’s okay to want both? To love them and still be me?')
FIRST = 'That’s what I keep telling myself.'


class SpeechCoverageTests(unittest.TestCase):
    def test_exact_reported_reply_passes_but_first_sentence_only_does_not(self):
        complete = compare_speech(REPLY,REPLY)
        fragment = compare_speech(REPLY,FIRST)
        self.assertTrue(complete.acceptable)
        self.assertEqual(complete.coverage,1.0)
        self.assertEqual(complete.matched_words,complete.expected_words)
        self.assertGreater(complete.expected_words,30)
        self.assertFalse(fragment.acceptable)
        self.assertLess(fragment.coverage,.30)
        self.assertEqual(fragment.reason,'insufficient_word_coverage')

    def test_unicode_apostrophes_contractions_case_and_accents_match_recognition_variants(self):
        expected = 'That’s why I’m here. I can’t leave, and we’ll stay until they’re ready. I’m Anaïs from São Paulo.'
        heard = 'THAT IS WHY I AM HERE I cannot leave and we will stay until they are ready I am Anais from Sao Paulo'
        report = compare_speech(expected,heard)
        self.assertTrue(report.acceptable)
        self.assertEqual(report.coverage,1.0)

    def test_small_numeric_formats_decimal_punctuation_and_percent_are_normalized(self):
        expected = 'At move 31, we had 14 months, 3 weeks and 3.5 hours. The display read 50%.'
        heard = 'At move thirty-one we had fourteen months three weeks and three point five hours the display read fifty percent'
        report = compare_speech(expected,heard)
        self.assertTrue(report.acceptable)
        self.assertEqual(report.coverage,1.0)

    def test_speaker_controls_and_delivery_tags_never_count_as_spoken_words(self):
        styled = '<|speaker:0|> [sad, fragile voice; breathe slowly] '+REPLY
        self.assertEqual(spoken_text(styled),REPLY)
        styled_report = compare_speech(styled,REPLY)
        plain_report = compare_speech(REPLY,REPLY)
        self.assertEqual(styled_report,plain_report)
        self.assertNotIn('speaker',spoken_text(styled))

    def test_minor_recognition_errors_and_fillers_do_not_reject_a_complete_reply(self):
        heard = REPLY.replace('hurts','aches').replace('telling myself','telling me')
        heard = 'Um, '+heard+' Uh.'
        report = compare_speech(REPLY,heard)
        self.assertTrue(report.acceptable)
        self.assertGreater(report.coverage,.85)
        self.assertLess(report.coverage,1.0)

    def test_repeating_the_opening_does_not_fake_coverage_of_later_sentences(self):
        report = compare_speech(REPLY,' '.join([FIRST]*10))
        self.assertFalse(report.acceptable)
        self.assertLess(report.coverage,.70)
        self.assertLess(report.matched_words,report.expected_words)

    def test_substantial_middle_omissions_fail_even_if_the_last_sentence_is_present(self):
        heard = FIRST+' To love them and still be me?'
        report = compare_speech(REPLY,heard)
        self.assertFalse(report.acceptable)
        self.assertLess(report.coverage,.70)

    def test_clearly_missing_final_sentence_fails_despite_high_total_coverage(self):
        heard = REPLY.split(' To love them',1)[0]
        report = compare_speech(REPLY,heard)
        self.assertGreaterEqual(report.coverage,.70)
        self.assertFalse(report.acceptable)
        self.assertEqual(report.reason,'missing_reply_end')

    def test_ordered_coverage_cannot_be_faked_with_reversed_words(self):
        expected = 'alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima'
        heard = ' '.join(reversed(expected.split()))
        report = compare_speech(expected,heard)
        self.assertFalse(report.acceptable)
        self.assertEqual(report.matched_words,1)

    def test_empty_and_unrelated_transcripts_fail_but_short_minor_variations_can_pass(self):
        self.assertFalse(compare_speech('Hello there Anna.','').acceptable)
        self.assertFalse(compare_speech('Hello Anna.','Goodbye Peter.').acceptable)
        self.assertTrue(compare_speech('Hello there Anna.','Hello their Anna.').acceptable)
        self.assertFalse(compare_speech('','Any unexpected recognition.').acceptable)
        self.assertFalse(compare_speech('[calm] <|speaker:0|>','').acceptable)

    def test_prefix_coverage_is_monotonic_as_more_expected_speech_is_recognized(self):
        expected = 'alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima'
        reports = [compare_speech(expected,' '.join(expected.split()[:count])) for count in (0,2,4,8,12)]
        values = [report.coverage for report in reports]
        self.assertEqual(values,sorted(values))
        self.assertEqual(values[-1],1.0)
        self.assertTrue(reports[-1].acceptable)

    def test_alignment_limits_are_enforced_without_retaining_text_in_reports(self):
        expected = ' '.join(f'word{index}' for index in range(MAX_EXPECTED_WORDS))
        heard = expected+' '+' '.join(f'noise{index}' for index in range(MAX_HEARD_WORDS-MAX_EXPECTED_WORDS))
        report = compare_speech(expected,heard)
        self.assertEqual(report.expected_words,MAX_EXPECTED_WORDS)
        self.assertEqual(report.heard_words,MAX_HEARD_WORDS)
        self.assertEqual(report.matched_words,MAX_EXPECTED_WORDS)
        self.assertEqual(report.coverage,1.0)
        with self.assertRaises(ValueError): compare_speech(expected+' extra',heard)
        with self.assertRaises(ValueError): compare_speech(expected,heard+' extra')

    def test_safe_metadata_is_a_detached_private_text_free_report(self):
        report = compare_speech('PRIVATE_SENTINEL contains a complete expected sentence here.','PRIVATE_SENTINEL contains a complete expected sentence here.')
        metadata = report.safe_metadata()
        self.assertEqual(set(metadata),{'acceptable','expected_words','heard_words','matched_words','coverage','reason'})
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(metadata))
        self.assertNotIn('PRIVATE_SENTINEL',repr(report))
        metadata['acceptable'] = False
        self.assertTrue(report.acceptable)
        with self.assertRaises(FrozenInstanceError): report.reason='changed'

    def test_invalid_or_oversized_text_raises_fixed_non_sensitive_errors(self):
        for invalid in (None,7,[],b'PRIVATE_SENTINEL','PRIVATE_SENTINEL'*10000):
            with self.subTest(kind=type(invalid).__name__):
                with self.assertRaises(ValueError) as raised: compare_speech(invalid,'hello')
                self.assertNotIn('PRIVATE_SENTINEL',str(raised.exception))
                with self.assertRaises(ValueError): compare_speech('hello',invalid)


class RecognitionResampleTests(unittest.TestCase):
    def test_one_second_native_pcm_becomes_one_second_of_recognizer_audio(self):
        time = np.arange(44100)/44100
        original = np.rint(12000*np.sin(2*np.pi*440*time)).astype('<i2')
        output = np.frombuffer(resample_for_recognition(original.tobytes()),dtype='<i2')
        self.assertEqual(len(output),16000)
        ideal = 12000*np.sin(2*np.pi*440*np.arange(16000)/16000)
        self.assertGreater(np.corrcoef(output,ideal)[0,1],.999)

    def test_constant_full_scale_signals_retain_signed_little_endian_samples_and_the_tail(self):
        for value in (-32768,32767):
            original = np.full(4410,value,dtype='<i2')
            output = np.frombuffer(resample_for_recognition(original.tobytes()),dtype='<i2')
            self.assertEqual(len(output),1600)
            self.assertTrue(np.all(output==value))

    def test_small_inputs_and_mutable_buffers_remain_bounded_and_are_not_mutated(self):
        self.assertEqual(resample_for_recognition(b'\x34\x12'),b'\x34\x12')
        original = bytearray(np.arange(441,dtype='<i2').tobytes())
        before = bytes(original)
        output = np.frombuffer(resample_for_recognition(memoryview(original)),dtype='<i2')
        self.assertEqual(bytes(original),before)
        self.assertEqual(len(output),160)
        self.assertTrue(np.all(np.diff(output.astype(np.int32))>=0))

    def test_downsampling_reduces_high_frequency_aliasing(self):
        time = np.arange(4410)/44100
        low = np.rint(12000*np.sin(2*np.pi*2000*time)).astype('<i2').tobytes()
        high = np.rint(12000*np.sin(2*np.pi*14000*time)).astype('<i2').tobytes()
        low_output = np.frombuffer(resample_for_recognition(low),dtype='<i2')[30:-30]
        high_output = np.frombuffer(resample_for_recognition(high),dtype='<i2')[30:-30]
        self.assertLess(np.std(high_output),np.std(low_output)*.1)

    def test_empty_unaligned_non_pcm_and_oversized_inputs_are_rejected(self):
        for invalid in (b'',b'\x01',None,'PRIVATE_SENTINEL',7,b'\x00'*(MAX_PCM_BYTES+2)):
            with self.subTest(kind=type(invalid).__name__):
                with self.assertRaises(ValueError) as raised: resample_for_recognition(invalid)
                self.assertNotIn('PRIVATE_SENTINEL',str(raised.exception))


if __name__ == '__main__': unittest.main()
