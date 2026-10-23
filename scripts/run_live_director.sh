#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv-drama3/bin/python scripts/run_live_model.py director
