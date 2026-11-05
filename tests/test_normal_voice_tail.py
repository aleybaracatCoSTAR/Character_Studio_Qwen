"""Normal EOS takes may shed strict quiet tails only before a successful word check."""
import json
import unittest

import httpx
import numpy as np

from live_voice.providers import LocalProviders, VoiceTakeIncomplete
from live_voice.voice import Delivery, spoken_input
from live_voice.voice_coverage import resample_for_recognition
from live_voice.voice_tail import RATE, analyze_tail
from test_spoken_text import PLAIN_PASSAGE
from test_voice_generation import REFERENCE, delta, done, response
from test_voice_tail import silence, tone


REPLY = 'I can hear the complete reply and every final word clearly.'


class RecordingSTT:
    def __init__(self, transcript):
        self.transcript = transcript
        self.inputs = []

    async def transcribe(self, pcm):
        self.inputs.append(bytes(pcm))
        return self.transcript

    def close(self): pass


class NormalVoiceTailTests(unittest.IsolatedAsyncioTestCase):
    def providers(self, pcm, transcript=REPLY, verify=True):
        requests = []
        stt = RecordingSTT(transcript)
        def serve(request):
            requests.append(json.loads(request.content))
            # Ordinary EOS, comfortably below the requested generation cap.
            return response(delta(pcm) + done(output_tokens=50), widths=(65536,))
        providers = LocalProviders(stt, verify_voice=verify, transport=httpx.MockTransport(serve))
        return providers, stt, requests

    async def test_normal_eos_checks_shortened_candidate_and_releases_exact_voice_plus_pad(self):
        voiced = tone(1000)
        pcm = voiced + silence(3000)
        retained = voiced + silence(400)
        self.assertTrue(analyze_tail(pcm).clean_ending)
        providers, stt, requests = self.providers(pcm)
        notices = []
        async def notice(message): notices.append(message)
        try:
            result = b''.join([part async for part in providers.audio(REPLY, REFERENCE, on_notice=notice)])
            self.assertEqual(result, retained)
            self.assertEqual(stt.inputs, [resample_for_recognition(retained)])
            self.assertEqual(len(stt.inputs[0]), round(1.4 * 16000) * 2)
            self.assertEqual(len(requests), 1)
            metadata = providers.last_voice_check
            self.assertEqual(metadata['audio_seconds'], 4.0)
            self.assertEqual(metadata['trimmed_audio_seconds'], 1.4)
            self.assertEqual(metadata['removed_silence_ms'], 2600.0)
            self.assertEqual(metadata['matched_words'], metadata['expected_words'])
        finally: await providers.close()

    async def test_missing_59_of_61_words_is_still_rejected_after_silence_removal_on_both_attempts(self):
        # Mirrors the reported 34 s waveform / 2 of 61 words case. Shortening
        # the ASR input must never convert its first phrase into a complete take.
        pcm = tone(1000) + silence(33000)
        retained = tone(1000) + silence(400)
        providers, stt, requests = self.providers(pcm, transcript='They wrote')
        notices = []; released = []
        speech = spoken_input(PLAIN_PASSAGE, Delivery(emotion='sad'), first_phrase=True)
        async def notice(message): notices.append(message)
        try:
            with self.assertLogs('live_voice.providers', level='WARNING') as logged:
                with self.assertRaises(VoiceTakeIncomplete) as failure:
                    async for part in providers.audio(speech, REFERENCE, on_notice=notice): released.append(part)
            self.assertEqual(released, [])
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[0], requests[1])
            self.assertEqual(requests[0]['input'], speech)
            self.assertEqual((requests[1]['ref_audio'], requests[1]['ref_text']), REFERENCE)
            self.assertEqual(stt.inputs, [resample_for_recognition(retained)] * 2)
            self.assertEqual(len(notices), 1)
            self.assertTrue(notices[0].endswith('Retrying the whole reply with the same emotion and voice.'))
            self.assertEqual(failure.exception.reason, 'speech_mismatch')
            metadata = failure.exception.metadata
            self.assertEqual((metadata['matched_words'], metadata['expected_words']), (2, 61))
            self.assertEqual(metadata['audio_seconds'], 34.0)
            self.assertEqual(metadata['trimmed_audio_seconds'], 1.4)
            self.assertEqual(metadata['removed_silence_ms'], 32600.0)
            self.assertEqual(metadata['attempts'], 2)
            self.assertNotIn(PLAIN_PASSAGE, '\n'.join(logged.output))
        finally: await providers.close()

    async def test_dc_and_ambiguous_noisy_ending_are_not_trimmed_or_shortened_for_asr(self):
        dc = np.full(RATE, 1000, dtype='<i2').tobytes() + silence(3000)
        noisy = bytearray(tone(1000) + silence(3000))
        np.frombuffer(noisy, dtype='<i2')[round(RATE * 3.5)] = 121
        for label, pcm in (('dc', dc), ('uncertain tail peak', bytes(noisy))):
            with self.subTest(label=label):
                self.assertFalse(analyze_tail(pcm).clean_ending)
                providers, stt, requests = self.providers(pcm)
                try:
                    # The recognizer is stubbed to establish full coverage; this
                    # assertion tests that uncertain waveform energy is retained.
                    result = b''.join([part async for part in providers.audio(REPLY)])
                    self.assertEqual(result, pcm)
                    self.assertEqual(stt.inputs, [resample_for_recognition(pcm)])
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(providers.last_voice_check.get('removed_silence_ms', 0), 0)
                finally: await providers.close()

    async def test_quiet_final_syllable_and_its_own_pad_are_preserved(self):
        voiced = tone(600)
        quiet_syllable = tone(200, amplitude=80)
        pcm = voiced + silence(1000) + quiet_syllable + silence(3000)
        retained = voiced + silence(1000) + quiet_syllable + silence(400)
        providers, stt, requests = self.providers(pcm)
        try:
            result = b''.join([part async for part in providers.audio(REPLY)])
            self.assertEqual(result, retained)
            self.assertEqual(stt.inputs, [resample_for_recognition(retained)])
            self.assertEqual(len(requests), 1)
        finally: await providers.close()

    async def test_explicit_verification_optout_keeps_raw_pcm_and_does_not_call_asr(self):
        pcm = tone(1000) + silence(3000)
        self.assertTrue(analyze_tail(pcm).clean_ending)
        providers, stt, requests = self.providers(pcm, verify=False)
        try:
            result = b''.join([part async for part in providers.audio(REPLY)])
            self.assertEqual(result, pcm)
            self.assertEqual(stt.inputs, [])
            self.assertEqual(len(requests), 1)
            self.assertIsNone(providers.last_voice_check)
        finally: await providers.close()


if __name__ == '__main__': unittest.main()
