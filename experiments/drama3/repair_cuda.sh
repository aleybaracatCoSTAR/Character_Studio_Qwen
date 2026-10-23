#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null || { echo 'uv is missing from ~/.local/bin.' >&2; exit 1; }
[[ -x .venv-drama3/bin/python ]] || { echo 'Run bash experiments/drama3/setup.sh first.' >&2; exit 1; }
.venv-drama3/bin/python - <<'PY'
import platform, sys
if sys.version_info[:2] != (3, 12) or platform.system() != 'Linux' or platform.machine() != 'x86_64':
    raise SystemExit('This repair targets the Surrey Linux x86_64 Python 3.12 environment. Use your Surrey terminal.')
PY
export DRAMA3_STORE="${DRAMA3_STORE:-/scratch4weeks/$USER/drama3-test-store}"
export UV_CACHE_DIR="$DRAMA3_STORE/uv-cache"
mkdir -p "$DRAMA3_STORE"
uv pip install --python .venv-drama3/bin/python --torch-backend=cu130 \
  --reinstall-package torch --reinstall-package torchaudio --reinstall-package torchvision \
  -r experiments/drama3/requirements-cu130.txt
.venv-drama3/bin/python experiments/drama3/check_install.py
echo 'CUDA package repair checked. Existing model files were kept.'
