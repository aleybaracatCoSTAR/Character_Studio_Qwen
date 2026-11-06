"""App configuration distinguishes early playback from pre-playback checking."""
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from live_voice.app import create_app


class VoicePlaybackModeTests(unittest.TestCase):
    def status(self, mode=None, verify='1'):
        with tempfile.TemporaryDirectory() as folder:
            environment = {**os.environ, 'LIVE_ARCHIVE_DIR':folder,
                           'LIVE_DEFAULT_ENGINE':'baseline', 'LIVE_VERIFY_VOICE':verify,
                           'LIVE_LISTENING_MODE':'baseline'}
            environment.pop('LIVE_OMNI_URL', None)
            environment.pop('LIVE_VOICE_PLAYBACK', None)
            if mode is not None:
                environment['LIVE_VOICE_PLAYBACK'] = mode
            with patch.dict(os.environ, environment, clear=True):
                with TestClient(create_app(preview=True)) as client:
                    return client.get('/api/status').json()

    def test_default_app_reports_immediate_playback_without_claiming_prechecked_words(self):
        result = self.status()
        self.assertEqual(result['voice_pipeline'], 'drama-stream-v1')
        self.assertFalse(result['voice_verification'])
        engine = result['comparison']['engines']['baseline']
        self.assertEqual(engine['voice_playback'], 'streaming')
        self.assertFalse(engine['whole_reply_verification'])
        # Drama remains the separate text-to-speech actor pipeline.
        self.assertFalse(engine['streaming_speech'])
        self.assertFalse(result['ready'])

    def test_checked_opt_in_retains_preplayback_verification(self):
        result = self.status('checked')
        self.assertEqual(result['voice_pipeline'], 'voice-checked-v5')
        self.assertTrue(result['voice_verification'])
        self.assertEqual(result['comparison']['engines']['baseline']['voice_playback'], 'checked')

    def test_disabling_word_checks_does_not_turn_checked_mode_into_streaming(self):
        result = self.status('checked', verify='0')
        self.assertEqual(result['voice_pipeline'], 'voice-checked-v5')
        self.assertFalse(result['voice_verification'])

    def test_invalid_mode_fails_before_model_loading(self):
        with self.assertRaisesRegex(ValueError, 'checked or streaming'):
            self.status('turbo')
