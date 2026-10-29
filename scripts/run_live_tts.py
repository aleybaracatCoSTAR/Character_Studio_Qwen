"""Keep Drama 3 and both of its stages on this profile's assigned GPU."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from live_voice.deployment import assigned_devices, profile
from live_voice.drama_sampling import MAIN_TOP_K, RESIDUAL_TOP_K, REQUEST_SAMPLING, ensure_live_sampling


def render_overlay(base, tts):
    """JSON is valid YAML and avoids a YAML dependency in the launcher."""
    return json.dumps({
        'base_config':str(base),
        'stages':[
            {'stage_id':0, 'gpu_memory_utilization':tts['main_memory'],
             'max_num_seqs':1, 'max_num_batched_tokens':1024,
             'compilation_config':{
                 'max_cudagraph_capture_size':1024,
                 'cudagraph_capture_sizes':[1,2,4,8,16,32,64,128,256,512,1024]},
             'hf_overrides':{'fish_omni_max_top_k':MAIN_TOP_K},
             'default_sampling_params':dict(REQUEST_SAMPLING)},
            {'stage_id':1, 'gpu_memory_utilization':tts['codec_memory'], 'max_num_seqs':1},
        ],
    }, indent=2) + '\n'


def main():
    config=profile();devices=assigned_devices(config,os.environ.get('CUDA_VISIBLE_DEVICES',''))
    repair=ensure_live_sampling()
    print(f"Drama 3 sampling repair: {repair['status']}; main candidates {MAIN_TOP_K}; "
          f"acoustic top-k {RESIDUAL_TOP_K}; temperature {REQUEST_SAMPLING['temperature']}; "
          f"top-p {REQUEST_SAMPLING['top_p']}; source {repair['source_sha256']}", flush=True)
    import vllm_omni
    base=Path(vllm_omni.__file__).parent/'deploy/fish_omni.yaml'
    if not base.is_file():raise SystemExit('Drama 3 deployment config is missing. Rerun its setup.')
    tts=config['tts'];store=Path(os.environ['LIVE_VOICE_STORE']);store.mkdir(parents=True,exist_ok=True)
    destination=store/'drama3-live.yaml'
    destination.write_text(render_overlay(base, tts))
    env=os.environ.copy();env['CUDA_VISIBLE_DEVICES']=devices['tts']
    os.execvpe('bash',['bash','experiments/drama3/run.sh','--deploy-config',str(destination)],env)


if __name__=='__main__':main()
