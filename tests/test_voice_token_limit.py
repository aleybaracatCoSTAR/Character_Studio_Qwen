"""A capped take is recoverable only with exact words and a quiet ending.

All audio is deterministic synthetic PCM and recognition is scripted. These
checks exercise the public audio iterator, not a real voice model or GPU.
"""
import base64
import json
import unittest

import httpx
import numpy as np

from live_voice.inspection import RequestInspection
from live_voice.providers import LocalProviders, VoiceFailure, VoiceTakeIncomplete, speech_token_budget
from test_voice_generation import REFERENCE, delta, done, event, response


RATE = 44100
TEXT = 'I want to feel safe here and keep these memories with me for now.'
STYLED = '<|speaker:0|> [calm, restrained voice] ' + TEXT
PREFIX = 'I want to feel safe here and keep'
SUCCESS_NOTICE = 'The full reply was verified; trailing silence was removed.'


def tone(ms=600):
    count = RATE * ms // 1000
    samples = np.rint(np.sin(np.arange(count) * (2 * np.pi * 220 / RATE)) * 8000).astype('<i2')
    return samples.tobytes()


def quiet(ms): return b'\x00\x00' * (RATE * ms // 1000)


ACTIVE = tone()
CLEAN = ACTIVE + quiet(1200)
TRIMMED = ACTIVE + quiet(400)
NORMAL = tone(800) + quiet(400)


class ScriptedSTT:
    def __init__(self, replies):
        self.replies = list(replies); self.calls = []
    async def transcribe(self, pcm):
        self.calls.append(pcm)
        if not self.replies: raise AssertionError('Unexpected ASR call.')
        result = self.replies.pop(0)
        if isinstance(result, BaseException): raise result
        return result
    def close(self): pass


def capped(pcm, budget, *, finish='length', suffix=b''):
    return delta(pcm) + event('speech.audio.done', usage={'output_tokens':budget}, finish_reason=finish) + suffix


def provider(replies, *, first=CLEAN, second=None, verify=True, first_suffix=b'', second_status=None):
    stt = ScriptedSTT(replies); requests = []
    def serve(request):
        payload = json.loads(request.content); requests.append(payload)
        attempt = len(requests)
        if attempt == 2 and second_status is not None:
            return httpx.Response(second_status, json={'error':'PRIVATE_SERVICE_RESPONSE'})
        if attempt == 1:
            body = capped(first, payload['max_new_tokens'], suffix=first_suffix)
        elif second is not None:
            body = delta(second) + done()
        else:
            body = capped(first, payload['max_new_tokens'])
        return response(body, widths=(1_048_576,))
    services = LocalProviders(stt, transport=httpx.MockTransport(serve), verify_voice=verify)
    return services, stt, requests


class CappedVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_full_capped_reply_with_quiet_tail_is_trimmed_verified_and_released_on_first_attempt(self):
        services, stt, requests = provider([TEXT]); notices = []
        async def notice(message): notices.append(message)
        try:
            output = b''.join([chunk async for chunk in services.audio(STYLED, REFERENCE, on_notice=notice)])
            self.assertEqual(speech_token_budget(STYLED), 488)
            self.assertEqual(output, TRIMMED)
            self.assertEqual(len(requests), 1); self.assertEqual(len(stt.calls), 1)
            # Verification must hear the actually retained 600 ms + 400 ms,
            # rather than the discarded long silent candidate.
            self.assertEqual(len(stt.calls[0]), 16000 * 2)
            self.assertEqual(requests[0]['input'], STYLED)
            self.assertEqual((requests[0]['ref_audio'],requests[0]['ref_text']), REFERENCE)
            self.assertEqual(notices, [SUCCESS_NOTICE])
            check = services.last_voice_check
            self.assertEqual(check['expected_words'], check['heard_words'])
            self.assertEqual(check['expected_words'], check['matched_words'])
            self.assertGreater(check['expected_words'], 0)
        finally: await services.close()

    async def test_exact_eight_hundred_ms_quiet_boundary_can_recover_without_removing_any_voiced_sample(self):
        services, stt, requests = provider([TEXT], first=ACTIVE+quiet(800))
        try:
            output = b''.join([chunk async for chunk in services.audio(STYLED)])
            self.assertEqual(output, TRIMMED); self.assertTrue(output.startswith(ACTIVE))
            self.assertEqual(len(requests), 1); self.assertEqual(len(stt.calls), 1)
        finally: await services.close()

    async def test_less_than_eight_hundred_ms_quiet_cannot_be_rescued_even_if_fake_asr_hears_full_words(self):
        services, stt, requests = provider([TEXT,TEXT], first=ACTIVE+quiet(780)); yielded=[]
        try:
            with self.assertRaises(VoiceTakeIncomplete):
                async for chunk in services.audio(STYLED): yielded.append(chunk)
            self.assertEqual(yielded, []); self.assertEqual(len(requests), 2)
            self.assertEqual(requests[0]['max_new_tokens'], requests[1]['max_new_tokens'])
        finally: await services.close()

    async def test_capped_quiet_candidate_without_verification_stays_rejected_and_never_calls_asr(self):
        services, stt, requests = provider([], verify=False); yielded=[]
        try:
            with self.assertRaises(VoiceTakeIncomplete):
                async for chunk in services.audio(STYLED): yielded.append(chunk)
            self.assertEqual(yielded, []); self.assertEqual(stt.calls, [])
            self.assertEqual(len(requests), 2); self.assertEqual(requests[0], requests[1])
        finally: await services.close()

    async def test_silence_or_dc_offset_does_not_become_verified_speech_from_a_hallucinated_transcript(self):
        for pcm in (quiet(1600), b'\xff\x1f' * (RATE*600//1000) + quiet(1200)):
            services, stt, requests = provider([TEXT,TEXT], first=pcm); yielded=[]
            try:
                with self.subTest(kind='silence' if pcm==quiet(1600) else 'dc'):
                    with self.assertRaises(VoiceTakeIncomplete):
                        async for chunk in services.audio(STYLED): yielded.append(chunk)
                    self.assertEqual(yielded, []); self.assertEqual(len(requests), 2)
                    self.assertEqual(requests[0]['max_new_tokens'],requests[1]['max_new_tokens'])
            finally: await services.close()

    async def test_one_missing_word_empty_or_repeated_recognition_cannot_rescue_a_quiet_capped_take(self):
        cases = (TEXT.rsplit(' ',1)[0], TEXT.split(' ',1)[1], '', TEXT+' now', PREFIX+' '+PREFIX)
        for heard in cases:
            services, stt, requests = provider([heard,heard]); yielded=[]
            try:
                with self.subTest(heard_words=len(heard.split())):
                    with self.assertRaises(VoiceTakeIncomplete):
                        async for chunk in services.audio(STYLED): yielded.append(chunk)
                    self.assertEqual(yielded, []); self.assertEqual(len(requests), 2)
                    self.assertEqual(requests[0],requests[1])
            finally: await services.close()

    async def test_active_ending_clean_ordered_prefix_gets_one_larger_budget_only_and_keeps_every_other_parameter(self):
        services, stt, requests = provider([PREFIX,TEXT], first=ACTIVE, second=NORMAL); notices=[]
        async def notice(message): notices.append(message)
        try:
            output = b''.join([chunk async for chunk in services.audio(STYLED,REFERENCE,on_notice=notice)])
            self.assertEqual(output,NORMAL); self.assertEqual(len(requests),2)
            first,second=requests
            self.assertEqual(second['max_new_tokens'],744)
            self.assertLessEqual(second['max_new_tokens'],1600)
            self.assertEqual({k:v for k,v in first.items() if k!='max_new_tokens'},
                             {k:v for k,v in second.items() if k!='max_new_tokens'})
            self.assertEqual(len(notices),1); self.assertEqual(len(stt.calls),2)
            self.assertNotIn(TEXT,notices[0]); self.assertNotIn(PREFIX,notices[0])
        finally: await services.close()

    async def test_active_ending_with_all_recognized_words_still_needs_a_second_take_with_a_larger_budget(self):
        services, stt, requests = provider([TEXT,TEXT], first=ACTIVE, second=NORMAL)
        try:
            output = b''.join([chunk async for chunk in services.audio(STYLED,REFERENCE)])
            self.assertEqual(output,NORMAL); self.assertEqual(len(requests),2)
            self.assertEqual(len(stt.calls),2)
            self.assertGreater(requests[1]['max_new_tokens'],requests[0]['max_new_tokens'])
            self.assertLessEqual(requests[1]['max_new_tokens'],1600)
            self.assertEqual({k:v for k,v in requests[0].items() if k!='max_new_tokens'},
                             {k:v for k,v in requests[1].items() if k!='max_new_tokens'})
        finally: await services.close()

    async def test_quiet_partial_noisy_repeated_or_too_short_candidates_never_receive_extra_budget(self):
        cases = ((CLEAN,[PREFIX,TEXT]),(ACTIVE,['unrelated noisy recognition words',TEXT]),
                 (ACTIVE,['feel safe here and keep these memories',TEXT]),
                 (ACTIVE,[PREFIX+' '+PREFIX,TEXT]),(ACTIVE,['I want',TEXT]),(tone(180),[TEXT]))
        for pcm,recognition in cases:
            services, stt, requests = provider(recognition,first=pcm,second=NORMAL)
            try:
                output=b''.join([chunk async for chunk in services.audio(STYLED,REFERENCE)])
                self.assertEqual(output,NORMAL);self.assertEqual(len(requests),2)
                self.assertEqual(requests[0],requests[1])
                self.assertEqual(len(stt.calls),len(recognition))
            finally: await services.close()

    async def test_invalid_events_after_capped_done_are_never_recovered_or_sent_to_recognition(self):
        suffixes=(done(), event('speech.audio.error',error='PRIVATE_LATER_ERROR'),
                  b'event: speech.audio.delta\ndata: {bad json}\n\n',
                  b'event: speech.audio.error\ndata: {"type":"speech.audio.error"}')
        for suffix in suffixes:
            services,stt,requests=provider([TEXT],second=NORMAL,first_suffix=suffix);yielded=[]
            try:
                async for chunk in services.audio(STYLED):yielded.append(chunk)
                self.assertEqual(b''.join(yielded),NORMAL);self.assertEqual(len(requests),2)
                self.assertEqual(len(stt.calls),1)
            finally:await services.close()

    async def test_unaligned_capped_pcm_is_never_rescued_even_if_a_second_regular_take_can_play(self):
        services,stt,requests=provider([TEXT],first=CLEAN+b'\x00',second=NORMAL)
        try:
            output=b''.join([chunk async for chunk in services.audio(STYLED)])
            self.assertEqual(output,NORMAL);self.assertEqual(len(requests),2);self.assertEqual(len(stt.calls),1)
        finally:await services.close()

    async def test_expanded_budget_then_transient_failure_cannot_start_a_third_attempt(self):
        services,stt,requests=provider([PREFIX],first=ACTIVE,second_status=503);yielded=[];notices=[]
        async def notice(message):notices.append(message)
        try:
            with self.assertRaises(VoiceFailure):
                async for chunk in services.audio(STYLED,on_notice=notice):yielded.append(chunk)
            self.assertEqual(yielded,[]);self.assertEqual(len(requests),2);self.assertEqual(len(stt.calls),1)
            self.assertGreater(requests[1]['max_new_tokens'],requests[0]['max_new_tokens']);self.assertEqual(len(notices),1)
        finally:await services.close()

    async def test_a_transient_first_attempt_then_capped_prefix_cannot_start_a_third_attempt(self):
        stt=ScriptedSTT([PREFIX]);requests=[];yielded=[]
        def serve(request):
            payload=json.loads(request.content);requests.append(payload)
            return httpx.Response(503) if len(requests)==1 else response(capped(ACTIVE,payload['max_new_tokens']),widths=(1_048_576,))
        services=LocalProviders(stt,transport=httpx.MockTransport(serve),verify_voice=True)
        try:
            with self.assertRaises(VoiceTakeIncomplete):
                async for chunk in services.audio(STYLED):yielded.append(chunk)
            self.assertEqual(yielded,[]);self.assertEqual(len(requests),2);self.assertEqual(len(stt.calls),1)
        finally:await services.close()

    async def test_candidate_audio_and_recognized_text_never_leak_to_diagnostics_logs_or_inspector(self):
        heard='PRIVATE_RECOGNIZED_CANDIDATE_WORDS';services,stt,requests=provider([heard,heard]);notices=[];yielded=[];events=[]
        async def notice(message):notices.append(message)
        async def send_json(value):events.append(value)
        inspection=RequestInspection(send_json)
        try:
            with self.assertLogs('live_voice.providers',level='WARNING') as logs:
                with self.assertRaises(VoiceTakeIncomplete) as raised:
                    with inspection.scope(1):
                        async for chunk in services.audio(STYLED,REFERENCE,on_notice=notice):yielded.append(chunk)
            diagnostic=raised.exception.diagnostic()
            visible=json.dumps(diagnostic)+'\n'.join(logs.output)+''.join(notices)+str(raised.exception)
            captured=json.dumps(inspection.snapshot())
            for secret in (heard,REFERENCE[0],base64.b64encode(CLEAN).decode()):
                self.assertNotIn(secret,visible);self.assertNotIn(secret,captured)
            self.assertNotIn(TEXT,visible);self.assertNotIn('calm, restrained voice',visible)
            # Submitted text is deliberately inspectable; a candidate transcript
            # and its PCM are not another submitted prompt or metadata field.
            self.assertIn(TEXT,captured);self.assertEqual(yielded,[])
            self.assertEqual(len(inspection.requests),2)
        finally:inspection.close();await services.close()


if __name__=='__main__':unittest.main()
