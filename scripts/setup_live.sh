#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null || { echo 'uv is required in ~/.local/bin.' >&2; exit 1; }
export LIVE_VOICE_STORE="${LIVE_VOICE_STORE:-/scratch4weeks/$USER/live-voice-store}"
export UV_CACHE_DIR="$LIVE_VOICE_STORE/uv-cache"
mkdir -p "$LIVE_VOICE_STORE"
[[ -d .venv-live ]] || uv venv --python 3.12 .venv-live
uv pip install --python .venv-live/bin/python -r requirements-live.txt
if [[ ! -x .venv-drama3/bin/vllm ]]; then
  echo 'Next install the GPU serving environment: bash experiments/drama3/setup.sh'
else
  .venv-drama3/bin/python experiments/drama3/check_install.py
fi
echo 'Next: .venv-live/bin/python scripts/download_live_models.py'
