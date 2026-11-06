"""Offline paths, checkpoint checks and GPU placement for the isolated experiment."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from importlib.metadata import distribution

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from live_voice.deployment import check_ports, model_command, profile, STT
from live_voice.listening_config import listening_mode, listening_profile, EXPERIMENT, PORT as LISTENING_PORT

MODEL_ID = 'Qwen/Qwen3-Omni-30B-A3B-Instruct'
REVISION = '26291f793822fb6be9555850f06dfe95f2d7e695'
OMNI_REVISION = '798fef99a33fba3ad3c2439d6ec10fe838c0076b'
PORTS = (18091, 18092, 18093, 18094, 18097)


def paths(environ=None):
    env = os.environ if environ is None else environ
    user = env.get('USER', 'YOUR_USER')
    store = Path(env.get('LIVE_VOICE_STORE', f'/scratch4weeks/{user}/live-voice-store')).expanduser().resolve()
    return {
        'live': store,
        'model': Path(env.get('QWEN_MODEL_DIR', f'/scratch4weeks/{user}/qwen-character-store/model')).expanduser().resolve(),
        'experiment': Path(env.get('QWEN_STREAM_STORE', str(store / 'qwen-stream'))).expanduser().resolve(),
    }


def allocation(visible):
    tokens = [token.strip() for token in visible.split(',')]
    if len(tokens) != 3 or any(not token or any(c.isspace() for c in token) for token in tokens) or len(set(tokens)) != 3:
        raise ValueError('The sequential Qwen streaming profile needs exactly three distinct allocated GPUs.')
    return {'omni': ','.join(tokens[:2]), 'director': tokens[2]}


def _json(path):
    if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('A required local checkpoint metadata file is missing or invalid: ' + str(path))
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, UnicodeError) as exc:
        raise ValueError('Invalid local checkpoint metadata: ' + str(path)) from exc
    if not isinstance(value, dict):
        raise ValueError('Invalid local checkpoint metadata: ' + str(path))
    return value


def _nonempty(path):
    if not path.is_file() or path.stat().st_size <= 0:
        raise ValueError('A required checkpoint file is missing or empty: ' + str(path))


def _shards(directory, expected_count=None):
    index = _json(directory / 'model.safetensors.index.json')
    weights = index.get('weight_map')
    if not isinstance(weights, dict) or not weights:
        raise ValueError('The checkpoint weight index is invalid: ' + str(directory))
    names = set(weights.values()) if all(isinstance(v, str) for v in weights.values()) else set()
    if not names or (expected_count is not None and len(names) != expected_count):
        raise ValueError('The checkpoint shard count is invalid: ' + str(directory))
    for name in names:
        if Path(name).name != name or not name.endswith('.safetensors') or name in ('.', '..'):
            raise ValueError('The checkpoint weight index contains an unsafe filename.')
        _nonempty(directory / name)


def _recorded_models(location):
    result = {}
    for marker in (location['live'] / 'checkpoints-quality-3gpu.json',
                   location['experiment'] / 'checkpoints.json'):
        if marker.is_file():
            result.update(_json(marker))
    return result


def validate_model(directory):
    directory = Path(directory)
    marker = _json(directory / 'studio-checkpoint.json')
    if marker.get('model') != MODEL_ID or marker.get('revision') != REVISION:
        raise ValueError('The Qwen3-Omni checkpoint marker does not match the pinned experiment.')
    config = _json(directory / 'config.json')
    if config.get('model_type') != 'qwen3_omni_moe' or config.get('enable_audio_output') is not True:
        raise ValueError('The local checkpoint is not the audio-enabled Qwen3-Omni model.')
    for name in ('preprocessor_config.json', 'tokenizer_config.json', 'chat_template.json', 'vocab.json', 'merges.txt'):
        _nonempty(directory / name)
    _shards(directory, 15)


def validate_store(location=None):
    location = paths() if location is None else location
    validate_model(location['model'])
    recorded = _recorded_models(location)
    cfg = profile('quality-3gpu')
    director = cfg['director']
    for directory, identity in ((director['directory'], (director['id'], director['revision'])), ('stt', STT)):
        if recorded.get(directory) not in (list(identity), tuple(identity)):
            raise ValueError('Missing pinned checkpoint provenance for ' + directory + '. Run experiments/qwen_stream/download.py.')
    director_path = location['live'] / director['directory']
    _json(director_path / 'config.json')
    _shards(director_path)
    _nonempty(location['live'] / 'stt' / 'model.bin')
    _json(location['live'] / 'stt' / 'config.json')
    return location


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            json.dump(payload, output, indent=2)
            output.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def render_deploy(destination, *, base=None):
    # Resolve the exact installed upstream deploy rather than copying a drifting template.
    base = Path(base) if base is not None else Path(distribution('vllm-omni').locate_file('vllm_omni/deploy/qwen3_omni_moe.yaml'))
    if not base.is_file():
        raise ValueError('The pinned vLLM-Omni Qwen3 deployment template is missing.')
    overlay = _json(Path(__file__).with_name('deploy_overrides.json'))
    overlay['base_config'] = str(base.resolve())
    write_json(destination, overlay)
    return Path(destination)


def command(role, location, *, executable=None, deploy=None):
    executable = str(executable or ROOT / '.venv-qwen-stream/bin/vllm')
    if role == 'director':
        return model_command(listening_profile(profile('quality-3gpu')), 'director', location['live'], executable)
    if role != 'omni' or deploy is None:
        raise ValueError('Choose omni with a deployment file, or director.')
    return [executable, 'serve', str(location['model']), '--omni',
            '--served-model-name', MODEL_ID, '--host', '127.0.0.1', '--port', '18097',
            '--deploy-config', str(deploy)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Check local files and allocation without loading or downloading models.')
    parser.add_argument('--role', choices=('omni', 'director'))
    args = parser.parse_args()
    if not args.check and not args.role:
        parser.error('Choose --check or --role.')
    if not os.environ.get('SLURM_JOB_ID'):
        parser.error('Use an allocated three-H200 Slurm job.')
    try:
        assigned = allocation(os.environ.get('CUDA_VISIBLE_DEVICES', ''))
        location = validate_store()
        if args.check:
            check_ports(PORTS + ((LISTENING_PORT,) if listening_mode() == EXPERIMENT else ()))
            print('Qwen streaming preflight OK; no model was loaded.', flush=True)
            print('Allocated placement:', assigned, flush=True)
            return
        env = dict(os.environ, CUDA_VISIBLE_DEVICES=assigned[args.role],
                   HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
        deploy = None
        if args.role == 'omni':
            deploy = render_deploy(location['experiment'] / 'generated' / 'qwen3_omni_moe.json')
        argv = command(args.role, location, deploy=deploy)
        os.execve(argv[0], argv, env)
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
