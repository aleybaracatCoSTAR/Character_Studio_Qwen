#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export LIVE_VOICE_STORE="${LIVE_VOICE_STORE:-/scratch4weeks/$USER/live-voice-store}"
exec .venv-drama3/bin/python scripts/run_live_tts.py
