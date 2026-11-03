#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null || { echo 'uv is required in ~/.local/bin.' >&2; exit 1; }
export LIVE_VOICE_STORE="${LIVE_VOICE_STORE:-/scratch4weeks/$USER/live-voice-store}"
export LISTENING_STORE="${LISTENING_STORE:-$LIVE_VOICE_STORE/listening}"
export UV_CACHE_DIR="$LISTENING_STORE/uv-cache"
mkdir -p "$LISTENING_STORE"
if [[ -L .venv-listening ]]; then
  echo '.venv-listening must be its own directory, not a link to an existing environment.' >&2
  exit 1
fi
[[ -d .venv-listening ]] || uv venv --python 3.12 .venv-listening
.venv-listening/bin/python - <<'PY'
from pathlib import Path
import platform
import sys
expected = (Path.cwd() / '.venv-listening').resolve()
if Path(sys.prefix).resolve() != expected:
    raise SystemExit('Use this checkout’s separate .venv-listening environment.')
if sys.version_info[:2] != (3, 12) or platform.system() != 'Linux' or platform.machine() != 'x86_64':
    raise SystemExit('Experimental listening setup targets Surrey Linux x86_64 with Python 3.12.')
PY
uv pip install --python .venv-listening/bin/python -r experiments/listening/requirements.txt
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv-listening/bin/python - <<'PY'
from importlib import metadata
import fastapi
import httpx
import librosa
import numpy
import onnxruntime
import torch
import uvicorn
import websockets
from transformers import AutoModelForRNNT, AutoProcessor, WhisperFeatureExtractor
from transformers.models.nemotron_asr_streaming import NemotronAsrStreamingFeatureExtractor
if torch.__version__ != '2.13.0+cu130' or torch.version.cuda != '13.0':
    raise SystemExit('The isolated listening environment must use the pinned Torch cu130 wheel.')
if metadata.version('transformers') != '5.13.0':
    raise SystemExit('The isolated listening environment must use Transformers 5.13.0.')
if 'CPUExecutionProvider' not in onnxruntime.get_available_providers():
    raise SystemExit('Smart Turn needs ONNX Runtime’s CPUExecutionProvider.')
NemotronAsrStreamingFeatureExtractor()
print('Listening CPU imports and feature extractor OK. No checkpoint was loaded; no GPU test was performed.')
PY
echo 'Listening environment installed separately; existing model environments were not changed.'
echo 'Next: .venv-listening/bin/python scripts/download_listening_models.py --download'
echo 'Then: bash scripts/run_live_stack.sh --profile quality-3gpu --listening nemotron-smart-turn'
