"""Reproducible single-node GPU placement; no model is downloaded or started here."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STT = ('Systran/faster-whisper-base.en','3d3d5dee26484f91867d81cb899cfcf72b96be6c')


def profile(name=None):
    name = name or os.environ.get('LIVE_PROFILE','shared')
    profiles=json.loads((ROOT/'config/live_profiles.json').read_text())
    if name not in profiles: raise ValueError('Unknown live profile: '+name)
    return profiles[name]


def assigned_devices(config, visible):
    devices=[x.strip() for x in visible.split(',') if x.strip()]
    if len(devices)!=len(set(devices)): raise ValueError('The GPU allocation contains duplicate devices.')
    if len(devices)<config['gpus']:
        raise ValueError(f"This profile needs {config['gpus']} allocated GPUs; CUDA_VISIBLE_DEVICES contains {len(devices)}.")
    return {role:devices[spec['slot']] for role in ('actor','director','tts') if (spec:=config[role])}


def checkpoints(config):
    result={'stt':STT}
    for role in ('actor','director'):
        if config[role]:
            spec=config[role];result[spec['directory']]=(spec['id'],spec['revision'])
    return result


def model_command(config, role, store, executable):
    spec=config.get(role)
    if not spec: raise ValueError('This profile shares its actor server with the director.')
    directory=Path(store)/spec['directory']
    if not (directory/'config.json').is_file(): raise ValueError('Missing checkpoint: '+str(directory))
    return [str(executable),'serve',str(directory),'--served-model-name',
            'live-character' if role=='actor' else 'live-director',
            '--host','127.0.0.1','--port','18092' if role=='actor' else '18094',
            '--dtype','bfloat16','--gpu-memory-utilization',str(spec['memory']),
            '--max-model-len','16384' if config['gpus']>1 else '8192',
            '--max-num-seqs','1','--max-num-batched-tokens','1024']
