"""Explicit pinned downloads, reusing the original Qwen, director and STT caches."""
import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from experiments.qwen_stream.runtime import MODEL_ID, REVISION, paths, profile, STT, validate_model, validate_store, write_json, _json, _shards, _nonempty, _recorded_models

PATTERNS = ['*.json', '*.safetensors', '*.bin', '*.txt', '*.model', '*.tiktoken', '*.jinja', 'README.md', 'LICENSE*']


def prepare(location, *, snapshot_download):
    """Called only by explicit setup; a valid pinned cache avoids network entirely."""
    try:
        validate_model(location['model'])
        print('Reusing pinned Qwen3-Omni checkpoint.', flush=True)
    except (ValueError, OSError):
        print('Downloading pinned Qwen3-Omni checkpoint (about 71 GB if absent).', flush=True)
        snapshot_download(MODEL_ID, revision=REVISION, local_dir=location['model'],
                          allow_patterns=PATTERNS, max_workers=4, token=False)
        # Written only after the pinned snapshot call succeeds.
        write_json(location['model'] / 'studio-checkpoint.json', {'model': MODEL_ID, 'revision': REVISION})
        validate_model(location['model'])
    cfg = profile('quality-3gpu')['director']
    needed = {cfg['directory']: (cfg['id'], cfg['revision']), 'stt': STT}
    recorded = _recorded_models(location)
    for directory, identity in needed.items():
        target = location['live'] / directory
        valid = recorded.get(directory) in (list(identity), tuple(identity))
        if valid:
            try:
                _json(target / 'config.json')
                _nonempty(target / 'model.bin') if directory == 'stt' else _shards(target)
            except (ValueError, OSError):
                valid = False
        if valid:
            print('Reusing pinned ' + directory + ' checkpoint.', flush=True)
        else:
            print('Downloading pinned ' + directory + ' checkpoint.', flush=True)
            snapshot_download(identity[0], revision=identity[1], local_dir=target,
                              allow_patterns=PATTERNS, max_workers=4, token=False)
        # This experiment's marker leaves the baseline profile marker unchanged.
        recorded[directory] = list(identity)
        write_json(location['experiment'] / 'checkpoints.json', recorded)
    validate_store(location)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Offline file/provenance check only; never downloads or loads models.')
    parser.add_argument('--model-dir', help='Existing Qwen3-Omni model directory (otherwise QWEN_MODEL_DIR or original scratch cache).')
    parser.add_argument('--store', help='Existing LIVE_VOICE_STORE containing director-32b and stt.')
    args = parser.parse_args()
    env = dict(os.environ)
    if args.model_dir:
        env['QWEN_MODEL_DIR'] = args.model_dir
    if args.store:
        env['LIVE_VOICE_STORE'] = args.store
    location = paths(env)
    try:
        if args.check:
            validate_store(location)
            print('Pinned checkpoint files present. No model was loaded or downloaded.')
            return
        from huggingface_hub import snapshot_download
        prepare(location, snapshot_download=snapshot_download)
        print('Pinned experiment checkpoints ready. No model was loaded.')
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
