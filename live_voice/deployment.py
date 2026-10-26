"""Reproducible single-node GPU placement; no model is downloaded or started here."""
import json
import os
import errno
import socket
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STT = ('Systran/faster-whisper-base.en','3d3d5dee26484f91867d81cb899cfcf72b96be6c')


def check_ports(ports):
    """Allow a server restart after TIME_WAIT, while refusing active listeners.

    Match asyncio/Uvicorn's SO_REUSEADDR policy. A plain bind can mistake a
    recently closed accepted connection for a still-running test server.
    This does not use SO_REUSEPORT or stop any existing process.
    """
    for port in ports:
        # On some systems a reuse-enabled bind to a specific address can pass
        # while a wildcard listener exists. Refuse any reachable TCP listener
        # first, even if that process used different socket bind options.
        with socket.socket() as client:
            client.settimeout(.3)
            result = client.connect_ex(('127.0.0.1', port))
        if result == 0:
            raise ValueError(f'Port {port} is already in use. Inspect its owner with ss -ltnp before restarting.')
        if result != errno.ECONNREFUSED:
            raise OSError(result, f'Could not verify local port {port}')
        try:
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(('127.0.0.1', port))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                raise ValueError(f'Port {port} is already in use. Inspect its owner with ss -ltnp before restarting.') from exc
            raise


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
