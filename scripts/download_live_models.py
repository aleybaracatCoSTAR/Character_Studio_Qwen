"""Explicit, revision-pinned downloads for a chosen live profile. No inference."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from live_voice.deployment import checkpoints, profile


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--profile',default=os.environ.get('LIVE_PROFILE','shared'))
    args=parser.parse_args();models=checkpoints(profile(args.profile))
    from huggingface_hub import snapshot_download
    store=Path(os.environ.get('LIVE_VOICE_STORE',f"/scratch4weeks/{os.environ.get('USER','YOUR_USER')}/live-voice-store")).expanduser()
    store.mkdir(parents=True,exist_ok=True)
    for directory,(model,revision) in models.items():
        print(f'Downloading {model} to {store/directory}',flush=True)
        snapshot_download(model,revision=revision,local_dir=store/directory,token=False,
                          allow_patterns=['*.json','*.safetensors','*.bin','*.txt','*.model','*.tiktoken','*.jinja','README.md','LICENSE*'])
    (store/f'checkpoints-{args.profile}.json').write_text(json.dumps(models,indent=2)+'\n')
    print('Selected text and recognition checkpoints downloaded. Drama 3 reuses its separate store.')


if __name__=='__main__':main()
