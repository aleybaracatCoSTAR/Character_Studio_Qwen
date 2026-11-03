#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv-listening/bin/python scripts/run_live_listening.py
