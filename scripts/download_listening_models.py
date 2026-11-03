"""Explicit pinned checkpoint downloads and offline validation for experimental listening."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

NEMOTRON_ID = 'nvidia/nemotron-speech-streaming-en-0.6b'
NEMOTRON_REVISION = 'ebe59e5a817142986528bbbee5dba8db7b38ed50'
SMARTTURN_ID = 'pipecat-ai/smart-turn-v3'
SMARTTURN_REVISION = 'f766f81d3cfdf7737ac64aad813d91bbfd56bf93'
SMARTTURN_FILENAME = 'smart-turn-v3.2-cpu.onnx'
NEMOTRON_FILES = (
    'config.json', 'generation_config.json', 'model.safetensors',
    'processor_config.json', 'tokenizer.json', 'tokenizer_config.json',
    'README.md', 'bias.md', 'explainability.md', 'privacy.md', 'safety.md',
)
NEMOTRON_MANIFEST = 'listening-checkpoint.json'
MAX_ARTIFACT_BYTES = 4 * 1024**3
_HASH = re.compile(r'[0-9a-f]{64}\Z')


class ListeningFilesError(RuntimeError):
    """Pinned listening artifacts are missing, changed or unsafe to open."""


def default_store() -> Path:
    live = Path(os.environ.get('LIVE_VOICE_STORE') or f'/scratch4weeks/{os.environ.get("USER", "")}/live-voice-store')
    return Path(os.environ.get('LISTENING_STORE') or live / 'listening')


def model_paths(store: Path) -> tuple[Path, Path]:
    return Path(store) / 'nemotron', Path(store) / 'smart-turn' / SMARTTURN_FILENAME


def _safe_file(path: Path, root: Path) -> None:
    try:
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            raise ListeningFilesError('A listening checkpoint file is missing or is not a regular file inside the listening store.')
        for parent in path.parents:
            if parent == root:
                break
            if parent.is_symlink():
                raise ListeningFilesError('Listening model subdirectories must not be symbolic links.')
        if not 0 < path.stat().st_size <= MAX_ARTIFACT_BYTES:
            raise ListeningFilesError('A listening checkpoint file has an invalid size.')
    except OSError as error:
        raise ListeningFilesError('A listening checkpoint file could not be read. Check its location and permissions.') from error


def _record(path: Path, root: Path) -> dict:
    _safe_file(path, root)
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return {'bytes': path.stat().st_size, 'sha256': digest.hexdigest()}


def _json_file(path: Path, root: Path, limit=65_536) -> dict:
    _safe_file(path, root)
    try:
        if path.stat().st_size > limit:
            raise ValueError()
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, OSError, UnicodeError) as error:
        raise ListeningFilesError('A listening checkpoint manifest or configuration is invalid.') from error


def _validate_record(path: Path, root: Path, expected: dict) -> dict:
    if (not isinstance(expected, dict) or type(expected.get('bytes')) is not int
            or not isinstance(expected.get('sha256'), str) or not _HASH.fullmatch(expected['sha256'])):
        raise ListeningFilesError('A listening checkpoint manifest lacks a valid size or SHA-256.')
    actual = _record(path, root)
    if actual != expected:
        raise ListeningFilesError('A listening checkpoint changed after download. Run the explicit listening model download again.')
    return actual


def validate_store(store: Path) -> dict:
    """Check exact pinned identities, file lists, sizes and hashes without network or models."""
    root = Path(store)
    if root.is_symlink():
        raise ListeningFilesError('The listening store must be an ordinary directory, not a symbolic link.')
    model, turn = model_paths(root)
    manifest = _json_file(model / NEMOTRON_MANIFEST, root)
    if (manifest.get('schema') != 1 or manifest.get('model_id') != NEMOTRON_ID
            or manifest.get('revision') != NEMOTRON_REVISION
            or not isinstance(manifest.get('files'), dict)
            or set(manifest['files']) != set(NEMOTRON_FILES)):
        raise ListeningFilesError('The Nemotron manifest does not match the pinned English streaming checkpoint.')
    count = total = 0
    for filename in NEMOTRON_FILES:
        record = _validate_record(model / filename, root, manifest['files'][filename])
        total += record['bytes']
        count += 1
    config = _json_file(model / 'config.json', root)
    if (config.get('model_type') != 'nemotron_asr_streaming'
            or config.get('architectures') != ['NemotronAsrStreamingForRNNT']):
        raise ListeningFilesError('The local model configuration is not the pinned native streaming RNNT architecture.')
    turn_manifest = _json_file(turn.with_suffix('.onnx.json'), root)
    if (turn_manifest.get('schema') != 1 or turn_manifest.get('model_id') != SMARTTURN_ID
            or turn_manifest.get('revision') != SMARTTURN_REVISION
            or turn_manifest.get('filename') != SMARTTURN_FILENAME):
        raise ListeningFilesError('The turn-detector manifest must identify Smart Turn v3.2 CPU, not v3.1 or the GPU artifact.')
    record = _validate_record(turn, root, {key: turn_manifest.get(key) for key in ('bytes', 'sha256')})
    return {'model_dir': str(model), 'turn_model': str(turn), 'files': count + 1,
            'bytes': total + record['bytes'], 'model_revision': NEMOTRON_REVISION,
            'turn_revision': SMARTTURN_REVISION}


validate_local_files = validate_store


def _write_manifest(path: Path, value: dict) -> None:
    if path.is_symlink():
        raise ListeningFilesError('Listening checkpoint manifests must not be symbolic links.')
    descriptor, temporary = tempfile.mkstemp(prefix='.listening-manifest-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def download_models(store: Path, *, download=None) -> dict:
    """Called only by explicit --download; never used by the serving runtime."""
    root = Path(store)
    try:
        return validate_store(root)
    except ListeningFilesError:
        pass
    model, turn = model_paths(root)
    for directory in (root, model, turn.parent):
        if directory.is_symlink():
            raise ListeningFilesError('Listening checkpoint directories must not be symbolic links.')
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if download is None:
        from huggingface_hub import hf_hub_download
        download = hf_hub_download
    for filename in NEMOTRON_FILES:
        if (model / filename).is_symlink():
            raise ListeningFilesError('Listening checkpoint files must not be symbolic links.')
        download(repo_id=NEMOTRON_ID, filename=filename, revision=NEMOTRON_REVISION,
                 local_dir=str(model), token=False)
    if turn.is_symlink():
        raise ListeningFilesError('Listening checkpoint files must not be symbolic links.')
    download(repo_id=SMARTTURN_ID, filename=SMARTTURN_FILENAME, revision=SMARTTURN_REVISION,
             local_dir=str(turn.parent), token=False)
    config = _json_file(model / 'config.json', root)
    if (config.get('model_type') != 'nemotron_asr_streaming'
            or config.get('architectures') != ['NemotronAsrStreamingForRNNT']):
        raise ListeningFilesError('The downloaded Nemotron configuration does not match the pinned model.')
    manifest = {'schema': 1, 'model_id': NEMOTRON_ID, 'revision': NEMOTRON_REVISION,
                'files': {name: _record(model / name, root) for name in NEMOTRON_FILES}}
    turn_manifest = {'schema': 1, 'model_id': SMARTTURN_ID, 'revision': SMARTTURN_REVISION,
                     'filename': SMARTTURN_FILENAME, **_record(turn, root)}
    _write_manifest(model / NEMOTRON_MANIFEST, manifest)
    _write_manifest(turn.with_suffix('.onnx.json'), turn_manifest)
    return validate_store(root)


def main(argv=None, *, download=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--download', action='store_true', help='Explicitly download the pinned files; no GPU is required.')
    action.add_argument('--check', action='store_true', help='Validate local files and hashes offline; no model is loaded.')
    parser.add_argument('--store', type=Path, default=default_store())
    args = parser.parse_args(argv)
    try:
        report = download_models(args.store, download=download) if args.download else validate_store(args.store)
    except Exception:
        print('Listening model files could not be verified. Check the listening store and run this script with --download; no models were started.', file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    print('Pinned listening files verified. No model was loaded and no GPU test was performed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
