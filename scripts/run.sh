#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -x .venv/bin/python ]]; then
  echo 'First run: bash scripts/setup.sh preview (laptop) or gpu (GPU job).' >&2; exit 1
fi
exec .venv/bin/python -m studio "$@"
