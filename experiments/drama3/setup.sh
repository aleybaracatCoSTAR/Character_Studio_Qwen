#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
export PATH="$HOME/.local/bin:$PATH"
if [[ -z "${SLURM_JOB_ID:-}" || -z "${CUDA_VISIBLE_DEVICES:-}" ]]; then
  echo 'Run this inside your Surrey GPU allocation.' >&2; exit 1
fi
command -v uv >/dev/null || { echo 'uv is missing from ~/.local/bin.' >&2; exit 1; }
export DRAMA3_STORE="${DRAMA3_STORE:-/scratch4weeks/$USER/drama3-test-store}"
export UV_CACHE_DIR="$DRAMA3_STORE/uv-cache"
mkdir -p "$DRAMA3_STORE"
nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv,noheader
[[ -d .venv-drama3 ]] || uv venv --python 3.12 .venv-drama3
.venv-drama3/bin/python - <<'PY'
import platform, sys
if sys.version_info[:2] != (3, 12) or platform.system() != 'Linux' or platform.machine() != 'x86_64':
    raise SystemExit('This setup targets Surrey Linux x86_64 with Python 3.12.')
PY
uv pip install --python .venv-drama3/bin/python --upgrade --torch-backend=cu130 \
  --reinstall-package torch --reinstall-package torchaudio --reinstall-package torchvision \
  -r experiments/drama3/requirements-cu130.txt \
  'vllm==0.32.0' \
  'vllm-omni @ git+https://github.com/vllm-project/vllm-omni.git@798fef99a33fba3ad3c2439d6ec10fe838c0076b'
.venv-drama3/bin/python experiments/drama3/check_install.py
echo 'Drama 3 environment installed. Model files have not been downloaded.'
echo 'Next: follow experiments/drama3/README.md, Download and launch.'
