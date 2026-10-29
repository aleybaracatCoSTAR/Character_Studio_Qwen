"""Trim only an established strict-quiet tail; never infer spoken completion."""
from dataclasses import FrozenInstanceError
import json
import unittest

import numpy as np

from live_voice.voice_tail import (FRAME_SAMPLES, MAX_PCM_BYTES, PAD_SAMPLES, RATE,
                                  analyze_tail, trim_tail)


def tone(ms=1000,amplitude=3000):
    count = round(RATE*ms/1000)
    return np.rint(amplitude*np.sin(2*np.pi*220*np.arange(count)/RATE)).astype('<i2').tobytes()


def silence(ms=1000): return b'\x00\x00'*round(RATE*ms/1000)


class VoiceTailTests(unittest.TestCase):
    def test_established_voice_plus_long_silence_keeps_all_voice_and_four_hundred_ms_pad(self):
        voiced = tone()
        pcm = voiced+silence(3000)
        report = analyze_tail(pcm)
        self.assertTrue(report.speech_detected)
        self.assertTrue(report.clean_ending)
        self.assertEqual(report.trailing_silence_ms,3000)
        self.assertEqual(report.voiced_ms,1000)
        self.assertEqual(report.last_non_silent_sample,RATE)
        trimmed = trim_tail(pcm,report)
        self.assertEqual(trimmed,voiced+silence(400))
        self.assertEqual(trimmed,pcm[:(RATE+PAD_SAMPLES)*2])

    def test_active_voiced_ending_never_qualifies_or_changes_the_audio(self):
        pcm = tone(1400)
        report = analyze_tail(pcm)
        self.assertTrue(report.speech_detected)
        self.assertFalse(report.clean_ending)
        self.assertEqual(report.trailing_silence_ms,0)
        self.assertEqual(trim_tail(pcm,report),pcm)

    def test_tail_shorter_than_eight_hundred_ms_is_not_trimmed(self):
        for duration in (0,100,400,780,799):
            pcm = tone()+silence(duration)
            with self.subTest(duration=duration):
                report = analyze_tail(pcm)
                self.assertTrue(report.speech_detected)
                self.assertFalse(report.clean_ending)
                self.assertEqual(trim_tail(pcm,report),pcm)

    def test_exact_eight_hundred_ms_boundary_is_required(self):
        pcm = tone()+silence(800)
        report = analyze_tail(pcm)
        self.assertEqual(report.trailing_silence_ms,800)
        self.assertTrue(report.clean_ending)
        self.assertEqual(trim_tail(pcm),tone()+silence(400))

    def test_all_zero_and_constant_dc_audio_never_establish_speech(self):
        for value in (0,10,1000,-2000,32767,-32768):
            pcm = np.full(RATE,value,dtype='<i2').tobytes()+silence(3000)
            with self.subTest(value=value):
                report = analyze_tail(pcm)
                self.assertFalse(report.speech_detected)
                self.assertFalse(report.clean_ending)
                self.assertEqual(report.voiced_ms,0)
                self.assertEqual(trim_tail(pcm),pcm)

    def test_brief_click_or_voice_fragment_is_not_enough_speech_evidence(self):
        for duration in (20,100,180):
            pcm = tone(duration)+silence(1000)
            with self.subTest(duration=duration):
                report = analyze_tail(pcm)
                self.assertFalse(report.speech_detected)
                self.assertFalse(report.clean_ending)
                self.assertEqual(trim_tail(pcm),pcm)
        report = analyze_tail(tone(200)+silence(1000))
        self.assertTrue(report.speech_detected)
        self.assertTrue(report.clean_ending)

    def test_quiet_breath_or_whisper_like_energy_alone_is_ambiguous_and_not_trimmed(self):
        pcm = tone(1000,amplitude=80)+silence(3000)
        report = analyze_tail(pcm)
        self.assertFalse(report.speech_detected)
        self.assertFalse(report.clean_ending)
        self.assertEqual(trim_tail(pcm),pcm)

    def test_quiet_final_syllable_above_strict_quiet_threshold_is_preserved_with_its_own_pad(self):
        voiced = tone(600)
        quiet_syllable = tone(200,amplitude=80)
        pcm = voiced+silence(1000)+quiet_syllable+silence(3000)
        report = analyze_tail(pcm)
        self.assertTrue(report.clean_ending)
        trimmed = trim_tail(pcm,report)
        self.assertEqual(trimmed,voiced+silence(1000)+quiet_syllable+silence(400))
        self.assertIn(quiet_syllable,trimmed)

    def test_low_rms_but_larger_tail_peak_is_not_called_silence(self):
        pcm = bytearray(tone()+silence(2000))
        values = np.frombuffer(pcm,dtype='<i2')
        values[RATE+round(RATE*1.5)] = 121
        report = analyze_tail(pcm)
        self.assertLess(report.trailing_silence_ms,800)
        self.assertFalse(report.clean_ending)
        self.assertEqual(trim_tail(pcm),bytes(pcm))

    def test_last_partial_quiet_window_counts_only_its_actual_samples(self):
        extra = 17
        pcm = tone()+silence(800)+b'\x00\x00'*extra
        report = analyze_tail(pcm)
        self.assertEqual(report.total_samples,RATE+round(RATE*.8)+extra)
        self.assertEqual(report.trailing_silence_ms,round(800+extra*1000/RATE,3))
        self.assertTrue(report.clean_ending)
        self.assertEqual(trim_tail(pcm),tone()+silence(400))

    def test_last_partial_nonquiet_window_cannot_be_padded_into_a_false_clean_ending(self):
        pcm = tone()+silence(1000)+b'\x88\x13'*17
        report = analyze_tail(pcm)
        self.assertEqual(report.last_non_silent_sample,len(pcm)//2)
        self.assertEqual(report.trailing_silence_ms,0)
        self.assertFalse(report.clean_ending)
        self.assertEqual(trim_tail(pcm),pcm)

    def test_voice_ending_inside_a_window_retains_its_entire_block_before_the_pad(self):
        voiced = tone()+b'\x88\x13'*17
        pcm = voiced+silence(3000)
        report = analyze_tail(pcm)
        self.assertTrue(report.clean_ending)
        self.assertEqual(report.last_non_silent_sample,RATE+FRAME_SAMPLES)
        trimmed = trim_tail(pcm,report)
        self.assertTrue(trimmed.startswith(voiced))
        self.assertGreaterEqual(len(trimmed)//2-len(voiced)//2,PAD_SAMPLES)

    def test_stale_report_for_same_length_different_audio_cannot_clip_an_active_ending(self):
        clean = tone()+silence(3000)
        report = analyze_tail(clean)
        active = tone(4000)
        self.assertEqual(len(clean),len(active))
        self.assertEqual(trim_tail(active,report),active)

    def test_safe_report_has_only_immutable_primitive_metadata_and_no_audio(self):
        report = analyze_tail(tone()+silence(1000))
        metadata = report.safe_metadata()
        self.assertEqual(set(metadata),{'trailing_silence_ms','speech_detected','clean_ending',
                                        'total_samples','voiced_ms','last_non_silent_sample'})
        self.assertTrue(all(isinstance(value,(bool,int,float)) for value in metadata.values()))
        json.dumps(metadata)
        self.assertFalse(any(isinstance(value,(bytes,bytearray,np.ndarray)) for value in vars(report).values()))
        metadata['clean_ending'] = False
        self.assertTrue(report.clean_ending)
        with self.assertRaises(FrozenInstanceError): report.clean_ending=False

    def test_input_bounds_and_pcm_alignment_are_validated_without_mutating_buffers(self):
        for invalid in (None,7,'PRIVATE_SENTINEL',b'',b'\x00',b'\x00'*(MAX_PCM_BYTES+2)):
            with self.subTest(kind=type(invalid).__name__):
                with self.assertRaises(ValueError) as raised: analyze_tail(invalid)
                self.assertNotIn('PRIVATE_SENTINEL',str(raised.exception))
                with self.assertRaises(ValueError): trim_tail(invalid)
        pcm = bytearray(tone()+silence(1000))
        original = bytes(pcm)
        report = analyze_tail(memoryview(pcm))
        trim_tail(pcm,report)
        self.assertEqual(bytes(pcm),original)

    def test_exact_maximum_input_is_bounded_valid_audio_without_false_speech(self):
        report = analyze_tail(b'\x00'*MAX_PCM_BYTES)
        self.assertEqual(report.total_samples,MAX_PCM_BYTES//2)
        self.assertFalse(report.speech_detected)
        self.assertFalse(report.clean_ending)


if __name__ == '__main__': unittest.main()
