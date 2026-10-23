"""Keep Drama 3 and both of its stages on this profile's assigned GPU."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from live_voice.deployment import assigned_devices, profile


def main():
    import vllm_omni
    config=profile();devices=assigned_devices(config,os.environ.get('CUDA_VISIBLE_DEVICES',''))
    base=Path(vllm_omni.__file__).parent/'deploy/fish_omni.yaml'
    if not base.is_file():raise SystemExit('Drama 3 deployment config is missing. Rerun its setup.')
    tts=config['tts'];store=Path(os.environ['LIVE_VOICE_STORE']);store.mkdir(parents=True,exist_ok=True)
    destination=store/'drama3-live.yaml'
    overlay='base_config: '+json.dumps(str(base))+f'''
stages:
  - stage_id: 0
    gpu_memory_utilization: {tts['main_memory']}
    max_num_seqs: 1
    max_num_batched_tokens: 1024
    compilation_config:
      max_cudagraph_capture_size: 1024
      cudagraph_capture_sizes: [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024]
  - stage_id: 1
    gpu_memory_utilization: {tts['codec_memory']}
    max_num_seqs: 1
'''
    destination.write_text(overlay)
    env=os.environ.copy();env['CUDA_VISIBLE_DEVICES']=devices['tts']
    os.execvpe('bash',['bash','experiments/drama3/run.sh','--deploy-config',str(destination)],env)


if __name__=='__main__':main()
