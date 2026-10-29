"""Exercise the Drama 3 sampler contract without loading a model or a GPU."""
import builtins
import copy
import importlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import httpx
import numpy as np

from live_voice.deployment import profile
from live_voice.director import DirectorSettings
from live_voice.drama_sampling import REQUEST_SAMPLING
from live_voice.inspection import RequestInspection
from live_voice.providers import LocalProviders
from live_voice.voice import VoiceSettings
from studio.characters import example
from test_voice_generation import DummySTT, PLAIN, REFERENCE, STYLED, delta, done, response


EXPECTED = {'temperature':1.0, 'top_p':0.9, 'top_k':0}
class ScriptedSTT(DummySTT):
    def __init__(self, replies): self.replies = iter(replies)
    async def transcribe(self, _pcm): return next(self.replies)


class DramaRequestSamplingTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_and_styled_requests_explicitly_select_the_supported_sampler(self):
        for text in (PLAIN,STYLED):
            sent=[]
            def serve(request):
                sent.append(json.loads(request.content)); return response(delta()+done())
            providers=LocalProviders(DummySTT(),transport=httpx.MockTransport(serve))
            try:
                with self.subTest(styled=text==STYLED):
                    _=[chunk async for chunk in providers.audio(text,REFERENCE)]
                    self.assertEqual(len(sent),1)
                    payload=sent[0]
                    self.assertEqual(payload['extra_params'],EXPECTED)
                    self.assertEqual(payload['input'],text)
                    self.assertEqual((payload['ref_audio'],payload['ref_text']),REFERENCE)
                    self.assertFalse(any(name in payload for name in EXPECTED))
            finally: await providers.close()

    async def test_incomplete_and_transient_retries_keep_emotion_voice_and_sampler_identical(self):
        for first_failure in ('incomplete','transient'):
            sent=[]
            def serve(request):
                payload=json.loads(request.content); sent.append(payload)
                if len(sent)==1:
                    if first_failure=='transient': return httpx.Response(503)
                    return response(delta()+done(payload['max_new_tokens']))
                return response(delta()+done())
            providers=LocalProviders(DummySTT(),transport=httpx.MockTransport(serve))
            try:
                with self.subTest(first_failure=first_failure):
                    _=[chunk async for chunk in providers.audio(STYLED,REFERENCE)]
                    self.assertEqual(len(sent),2)
                    self.assertEqual(sent[0],sent[1])
                    for payload in sent:
                        self.assertEqual(payload['extra_params'],EXPECTED)
                        self.assertEqual(payload['input'],STYLED)
                        self.assertEqual(payload['voice'],'default')
                        self.assertEqual((payload['ref_audio'],payload['ref_text']),REFERENCE)
            finally: await providers.close()

    async def test_active_ending_adaptive_retry_changes_only_the_token_budget(self):
        text='I want to feel safe here and keep these memories with me for now.'
        styled='<|speaker:0|> [calm, restrained voice] '+text
        prefix='I want to feel safe here and keep'
        # One second of synthetic varying PCM qualifies as an active ending;
        # scripted recognition supplies the exact ordered prefix, then full text.
        pcm=np.tile(np.array([8192,-8192],dtype='<i2'),22050).tobytes()
        sent=[]
        def serve(request):
            payload=json.loads(request.content); sent.append(payload)
            return response(delta(pcm)+done(payload['max_new_tokens'] if len(sent)==1 else 1),
                            widths=(1_048_576,))
        providers=LocalProviders(ScriptedSTT([prefix,text]),transport=httpx.MockTransport(serve),verify_voice=True)
        try:
            output=b''.join([chunk async for chunk in providers.audio(styled,REFERENCE)])
            self.assertEqual(output,pcm); self.assertEqual(len(sent),2)
            self.assertEqual(sent[0]['max_new_tokens'],488)
            self.assertEqual(sent[1]['max_new_tokens'],744)
            self.assertEqual({k:v for k,v in sent[0].items() if k!='max_new_tokens'},
                             {k:v for k,v in sent[1].items() if k!='max_new_tokens'})
            self.assertEqual(sent[1]['extra_params'],EXPECTED)
            self.assertEqual(sent[1]['input'],styled)
            self.assertEqual((sent[1]['ref_audio'],sent[1]['ref_text']),REFERENCE)
        finally: await providers.close()

    async def test_inspector_captures_the_actual_sampler_without_exposing_reference_audio(self):
        sent=[]
        def serve(request):
            sent.append(json.loads(request.content)); return response(delta()+done())
        async def publish(_event): pass
        inspection=RequestInspection(publish)
        providers=LocalProviders(DummySTT(),transport=httpx.MockTransport(serve))
        try:
            with inspection.scope(3):
                _=[chunk async for chunk in providers.audio(STYLED,REFERENCE)]
            captured=inspection.snapshot()['requests']
            self.assertEqual(len(captured),1)
            payload=copy.deepcopy(sent[0]); payload.pop('ref_audio')
            self.assertEqual(captured[0]['payload'],payload)
            self.assertEqual(captured[0]['payload']['extra_params'],EXPECTED)
            self.assertEqual(captured[0]['payload']['input'],STYLED)
            self.assertIn('ref_audio',captured[0]['omitted_fields'])
            self.assertNotIn(REFERENCE[0],json.dumps(captured))
        finally: inspection.close(); await providers.close()

    async def test_voice_overrides_do_not_change_actor_or_director_sampling(self):
        sent=[]
        plan={'intention':'Answer the visitor.','emotion':'calm','delivery':'Warm and measured.',
              'event':'none','trigger_action':'hold','evidence':'','beat_done':False,'beat_evidence':''}
        def serve(request):
            payload=json.loads(request.content); sent.append(payload)
            if request.url.path=='/v1/audio/speech': return response(delta()+done())
            if payload.get('stream'):
                return httpx.Response(200,text='data: {"choices":[{"delta":{"content":"Hello."}}]}\n\ndata: [DONE]\n\n')
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(plan)}}]})
        providers=LocalProviders(DummySTT(),transport=httpx.MockTransport(serve))
        try:
            _=[chunk async for chunk in providers.audio(STYLED)]
            _=[word async for word in providers.text(example(),[{'role':'user','content':'Hello.'}])]
            await providers.direct(example(),[],'Hello.',DirectorSettings(),VoiceSettings())
            voice,actor,director=sent
            self.assertEqual(voice['extra_params'],EXPECTED)
            self.assertEqual(actor['temperature'],0.7)
            self.assertEqual(director['temperature'],0.2)
            for payload in (actor,director):
                self.assertNotIn('extra_params',payload)
                self.assertNotIn('top_k',payload)
                self.assertNotIn('top_p',payload)
        finally: await providers.close()


class DramaDeploymentSamplingTests(unittest.TestCase):
    def test_sampling_constants_match_the_supported_runtime_contract(self):
        self.assertEqual(dict(REQUEST_SAMPLING),EXPECTED)

    def test_overlay_changes_only_the_intended_main_sampler_and_keeps_each_profile_gpu_budget(self):
        runner=importlib.import_module('scripts.run_live_tts')
        for name in ('shared','quality-2gpu','quality-3gpu'):
            tts=profile(name)['tts']; original=copy.deepcopy(tts)
            base=Path('/tmp/fake-drama-runtime/deploy/fish_omni.yaml')
            with self.subTest(profile=name):
                overlay=json.loads(runner.render_overlay(base,tts))
                self.assertEqual(overlay['base_config'],str(base))
                self.assertEqual(tts,original)
                stages={stage['stage_id']:stage for stage in overlay['stages']}
                self.assertEqual(set(stages),{0,1})
                main,codec=stages[0],stages[1]
                self.assertEqual(main['hf_overrides'],{'fish_omni_max_top_k':4096})
                self.assertEqual(main['default_sampling_params'],EXPECTED)
                self.assertEqual(main['gpu_memory_utilization'],tts['main_memory'])
                self.assertEqual(main['max_num_seqs'],1)
                self.assertEqual(main['max_num_batched_tokens'],1024)
                self.assertEqual(main['compilation_config']['max_cudagraph_capture_size'],1024)
                self.assertEqual(codec,{'stage_id':1,'gpu_memory_utilization':tts['codec_memory'],'max_num_seqs':1})

    def test_runtime_sampling_guard_runs_before_importing_the_gpu_runtime(self):
        runner=importlib.import_module('scripts.run_live_tts')
        imported=[]; real_import=builtins.__import__
        def watched_import(name,*args,**kwargs):
            if name=='vllm_omni' or name.startswith('vllm_omni.'):
                imported.append(name)
                raise AssertionError('The GPU runtime was imported before its sampling guard.')
            return real_import(name,*args,**kwargs)
        with patch.object(runner,'ensure_live_sampling',side_effect=RuntimeError('guard blocked runtime')) as guard:
            with patch.dict('os.environ',{'LIVE_PROFILE':'shared','CUDA_VISIBLE_DEVICES':'0'}):
                with patch('builtins.__import__',side_effect=watched_import):
                    with self.assertRaisesRegex(RuntimeError,'guard blocked runtime'):
                        runner.main()
        guard.assert_called_once_with()
        self.assertEqual(imported,[])


if __name__=='__main__': unittest.main()
