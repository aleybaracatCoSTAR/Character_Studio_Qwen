#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
case "${1:-}" in
  --help|-h) echo 'Usage: bash experiments/qwen_stream/setup.sh [--check]'; echo 'Install only the isolated serving environment; --check imports existing dependencies only.'; exit 0 ;;
  --check) [[ $# == 1 ]] || exit 1; exec .venv-qwen-stream/bin/python experiments/qwen_stream/check_install.py ;;
  '') ;;
  *) echo 'Unknown option. Use --help.' >&2; exit 1 ;;
esac
export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null || { echo 'Install uv first, then rerun this setup.' >&2; exit 1; }
[[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || {
  echo 'This serving environment requires Linux x86_64. Run setup on Surrey, not your Mac.' >&2; exit 1;
}
[[ ! -L .venv-qwen-stream ]] || { echo 'Refusing a symlink for the isolated environment.' >&2; exit 1; }
export LIVE_VOICE_STORE="${LIVE_VOICE_STORE:-/scratch4weeks/$USER/live-voice-store}"
export QWEN_STREAM_STORE="${QWEN_STREAM_STORE:-$LIVE_VOICE_STORE/qwen-stream}"
export UV_CACHE_DIR="$QWEN_STREAM_STORE/uv-cache"
export HF_HUB_CACHE="${HF_HUB_CACHE:-$QWEN_STREAM_STORE/huggingface/hub}"
[[ -d .venv-qwen-stream ]] || uv venv --python 3.12 .venv-qwen-stream
uv pip install --python .venv-qwen-stream/bin/python --upgrade --torch-backend cu130 \
  --reinstall-package torch --reinstall-package torchaudio --reinstall-package torchvision \
  -r experiments/qwen_stream/requirements-cu130.txt
.venv-qwen-stream/bin/python experiments/qwen_stream/check_install.py
echo 'Environment ready. No checkpoint was downloaded or loaded.'
echo 'Next: .venv-qwen-stream/bin/python experiments/qwen_stream/download.py'
