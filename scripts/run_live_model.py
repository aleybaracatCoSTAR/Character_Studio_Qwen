"""Launch one role on a device from the existing Slurm allocation, never physical GPU 0 by assumption."""
import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from live_voice.deployment import ROOT, assigned_devices, model_command, profile


def main():
    parser=argparse.ArgumentParser();parser.add_argument('role',choices=['actor','director']);args=parser.parse_args()
    config=profile()
    try:
        devices=assigned_devices(config,os.environ.get('CUDA_VISIBLE_DEVICES',''))
        command=model_command(config,args.role,os.environ['LIVE_VOICE_STORE'],ROOT/'.venv-drama3/bin/vllm')
        env=os.environ.copy();env['CUDA_VISIBLE_DEVICES']=devices[args.role]
        env.setdefault('VLLM_USE_FLASHINFER_SAMPLER','0')
    except (ValueError,KeyError) as exc:raise SystemExit(str(exc))
    os.execve(command[0],command,env)


if __name__=='__main__':main()
