"""Provisional recognition must never replace different finalized speech."""
import asyncio
import unittest

from live_voice.recognition import MAX_PCM_BYTES, PreparedRecognition


PCM = b'\x01\x00' * 100
MORE_PCM = PCM + b'\x02\x00' * 50


class PreparedRecognitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_matching_final_pcm_uses_work_started_before_the_endpoint(self):
        calls = []; started = asyncio.Event(); finish = asyncio.Event()
        async def transcribe(pcm):
            calls.append(pcm); started.set(); await finish.wait(); return 'The final words.'
        helper = PreparedRecognition(transcribe)
        try:
            self.assertTrue(helper.prepare(PCM))
            await started.wait()
            self.assertFalse(helper.prepare(PCM))
            final = asyncio.create_task(helper.transcribe(PCM))
            await asyncio.sleep(0)
            self.assertFalse(final.done())
            finish.set()
            self.assertEqual(await final, 'The final words.')
            self.assertTrue(helper.last_reused)
            self.assertEqual(calls, [PCM])
        finally: await helper.close()

    async def test_already_finished_preparation_is_reused_without_a_second_asr_call(self):
        calls = []; done = asyncio.Event()
        async def transcribe(pcm):
            calls.append(pcm); done.set(); return 'Remember the whole sentence.'
        helper = PreparedRecognition(transcribe)
        try:
            helper.prepare(bytearray(PCM)); await done.wait(); await asyncio.sleep(0)
            self.assertEqual(await helper.transcribe(memoryview(PCM)), 'Remember the whole sentence.')
            self.assertTrue(helper.last_reused); self.assertEqual(len(calls), 1)
        finally: await helper.close()

    async def test_resumed_speech_discards_draft_and_recognizes_the_actual_final_audio(self):
        calls = []; started = asyncio.Event(); cancelled = asyncio.Event()
        async def transcribe(pcm):
            calls.append(pcm)
            if pcm == PCM:
                started.set()
                try: await asyncio.Event().wait()
                finally: cancelled.set()
            return 'This includes the extra final words.'
        helper = PreparedRecognition(transcribe)
        try:
            helper.prepare(PCM); await started.wait()
            self.assertEqual(await helper.transcribe(MORE_PCM), 'This includes the extra final words.')
            self.assertTrue(cancelled.is_set()); self.assertFalse(helper.last_reused)
            self.assertEqual(calls, [PCM, MORE_PCM])
        finally: await helper.close()

    async def test_different_finished_draft_is_not_reused_even_if_its_text_looks_plausible(self):
        calls = []; done = asyncio.Event()
        async def transcribe(pcm):
            calls.append(pcm); done.set()
            return 'Old words.' if pcm == PCM else 'New words.'
        helper = PreparedRecognition(transcribe)
        try:
            helper.prepare(PCM); await done.wait(); await asyncio.sleep(0)
            self.assertEqual(await helper.transcribe(MORE_PCM), 'New words.')
            self.assertEqual(calls, [PCM, MORE_PCM]); self.assertFalse(helper.last_reused)
        finally: await helper.close()

    async def test_only_one_draft_runs_while_new_candidates_arrive(self):
        calls = []; started = asyncio.Event()
        async def transcribe(pcm):
            calls.append(pcm); started.set(); await asyncio.Event().wait()
        helper = PreparedRecognition(transcribe)
        try:
            self.assertTrue(helper.prepare(PCM)); await started.wait()
            for _ in range(20):
                self.assertFalse(helper.prepare(MORE_PCM))
                self.assertFalse(helper.prepare(PCM))
            self.assertEqual(calls, [PCM])
        finally: await helper.close()

    async def test_failed_matching_draft_gets_one_normal_final_recognition(self):
        calls = []; attempted = asyncio.Event()
        async def transcribe(pcm):
            calls.append(pcm)
            if len(calls) == 1:
                attempted.set(); raise RuntimeError('Provisional ASR failed')
            return 'Final recognition recovered.'
        helper = PreparedRecognition(transcribe)
        try:
            helper.prepare(PCM); await attempted.wait(); await asyncio.sleep(0)
            self.assertEqual(await helper.transcribe(PCM), 'Final recognition recovered.')
            self.assertEqual(calls, [PCM, PCM]); self.assertFalse(helper.last_reused)
        finally: await helper.close()

    async def test_failed_final_recognition_is_not_retried_indefinitely(self):
        calls = []
        async def transcribe(pcm):
            calls.append(pcm); raise RuntimeError('ASR unavailable')
        helper = PreparedRecognition(transcribe)
        try:
            with self.assertRaises(RuntimeError): await helper.transcribe(PCM)
            self.assertEqual(calls, [PCM])
            helper.prepare(PCM); await asyncio.sleep(0)
            with self.assertRaises(RuntimeError): await helper.transcribe(PCM)
            self.assertEqual(calls, [PCM, PCM, PCM])
        finally: await helper.close()

    async def test_cancelling_final_wait_cancels_prepared_work_without_retry(self):
        calls = []; started = asyncio.Event(); cancelled = asyncio.Event()
        async def transcribe(pcm):
            calls.append(pcm); started.set()
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        helper = PreparedRecognition(transcribe)
        try:
            helper.prepare(PCM); await started.wait()
            final = asyncio.create_task(helper.transcribe(PCM)); await asyncio.sleep(0)
            final.cancel()
            with self.assertRaises(asyncio.CancelledError): await final
            self.assertTrue(cancelled.is_set()); self.assertEqual(calls, [PCM])
            self.assertFalse(helper.last_reused)
        finally: await helper.close()

    async def test_clear_cancels_work_and_allows_a_fresh_candidate(self):
        calls = []; started = asyncio.Event(); cancelled = asyncio.Event()
        async def transcribe(pcm):
            calls.append(pcm)
            if pcm == PCM:
                started.set()
                try: await asyncio.Event().wait()
                finally: cancelled.set()
            return 'Fresh candidate.'
        helper = PreparedRecognition(transcribe)
        try:
            helper.prepare(PCM); await started.wait(); await helper.clear()
            self.assertTrue(cancelled.is_set()); self.assertTrue(helper.prepare(MORE_PCM))
            self.assertEqual(await helper.transcribe(MORE_PCM), 'Fresh candidate.')
            self.assertTrue(helper.last_reused); self.assertEqual(calls, [PCM, MORE_PCM])
        finally: await helper.close()

    async def test_close_cancels_an_active_final_and_prevents_future_preparation(self):
        started = asyncio.Event(); cancelled = asyncio.Event()
        async def transcribe(pcm):
            started.set()
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        helper = PreparedRecognition(transcribe)
        final = asyncio.create_task(helper.transcribe(PCM)); await started.wait()
        await helper.close()
        with self.assertRaises(asyncio.CancelledError): await final
        self.assertTrue(cancelled.is_set()); self.assertFalse(helper.prepare(PCM))
        with self.assertRaises(RuntimeError): await helper.transcribe(PCM)

    async def test_two_calls_never_share_prepared_audio_or_text(self):
        calls = []
        async def transcribe(pcm): calls.append(pcm); return 'Same input, independent call.'
        first = PreparedRecognition(transcribe); second = PreparedRecognition(transcribe)
        try:
            first.prepare(PCM); await asyncio.sleep(0)
            await second.transcribe(PCM)
            self.assertFalse(second.last_reused); self.assertEqual(calls, [PCM, PCM])
            self.assertEqual(await first.transcribe(PCM), 'Same input, independent call.')
            self.assertTrue(first.last_reused)
        finally: await first.close(); await second.close()

    async def test_pcm_is_bounded_and_mutable_input_is_snapshotted(self):
        calls = []
        async def transcribe(pcm): calls.append(pcm); return 'Snapshotted input.'
        helper = PreparedRecognition(transcribe)
        try:
            for invalid in (b'', b'\x00', b'\x00' * (MAX_PCM_BYTES + 2), 'not audio'):
                with self.subTest(size=len(invalid)), self.assertRaises(ValueError): helper.prepare(invalid)
            mutable = bytearray(PCM); helper.prepare(mutable); mutable[0] = 9
            await helper.transcribe(PCM)
            self.assertTrue(helper.last_reused); self.assertEqual(calls, [PCM])
        finally: await helper.close()

    async def test_prepare_during_final_recognition_does_not_start_another_job(self):
        calls = []; started = asyncio.Event(); finish = asyncio.Event()
        async def transcribe(pcm): calls.append(pcm); started.set(); await finish.wait(); return 'Done.'
        helper = PreparedRecognition(transcribe)
        try:
            final = asyncio.create_task(helper.transcribe(PCM)); await started.wait()
            self.assertFalse(helper.prepare(MORE_PCM)); self.assertEqual(calls, [PCM])
            finish.set(); self.assertEqual(await final, 'Done.')
        finally: await helper.close()
