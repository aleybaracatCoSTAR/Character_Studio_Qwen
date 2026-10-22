#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mode="${1:-preview}"
if [[ "$mode" != preview && "$mode" != gpu ]]; then
  echo 'Usage: bash scripts/setup.sh [preview|gpu]' >&2; exit 2
fi
export PATH="$HOME/.local/bin:$PATH"
if [[ "$mode" == gpu ]]; then
  if [[ -z "${SLURM_JOB_ID:-}" || -z "${CUDA_VISIBLE_DEVICES:-}" ]]; then
    echo 'Run GPU setup inside an allocated GPU job, not on login01.' >&2; exit 1
  fi
  nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
fi
if command -v uv >/dev/null 2>&1; then
  [[ -d .venv ]] || uv venv --python 3.12 .venv
  install=(uv pip install --python .venv/bin/python)
else
  if [[ "$mode" == gpu ]]; then
    echo 'uv is required for this GPU recipe. Reuse your existing ~/.local/bin/uv installation.' >&2; exit 1
  fi
  python3 -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ is required"'
  [[ -d .venv ]] || python3 -m venv .venv
  install=(.venv/bin/python -m pip install)
fi
if [[ "$mode" == gpu ]]; then
  "${install[@]}" torch==2.10.0 torchvision==0.25.0 --index-url https://download.pytorch.org/whl/cu128
  "${install[@]}" -r requirements-gpu.txt
else
  "${install[@]}" -r requirements-dev.txt
fi
echo "Setup complete ($mode). See README.md for the next step."
