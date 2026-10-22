#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export LIVE_VOICE_STORE="${LIVE_VOICE_STORE:-/scratch4weeks/$USER/live-voice-store}"
[[ -f "$LIVE_VOICE_STORE/llm/config.json" ]] || { echo 'Download the live models first.' >&2; exit 1; }
exec .venv-drama3/bin/vllm serve "$LIVE_VOICE_STORE/llm" \
  --served-model-name live-character --host 127.0.0.1 --port 18092 \
  --dtype bfloat16 --gpu-memory-utilization 0.18 \
  --max-model-len 8192 --max-num-seqs 1 --max-num-batched-tokens 1024
