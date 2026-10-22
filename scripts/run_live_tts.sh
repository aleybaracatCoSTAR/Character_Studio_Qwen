#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export LIVE_VOICE_STORE="${LIVE_VOICE_STORE:-/scratch4weeks/$USER/live-voice-store}"
mkdir -p "$LIVE_VOICE_STORE"
# Resolve the base config in the matched serving environment, not an unrelated checkout.
.venv-drama3/bin/python - <<'PY'
import json, os
from pathlib import Path
import vllm_omni
base = Path(vllm_omni.__file__).parent / 'deploy' / 'fish_omni.yaml'
if not base.exists(): raise SystemExit('Drama 3 deployment config is missing. Rerun its setup.')
config = 'base_config: ' + json.dumps(str(base)) + '''
stages:
  - stage_id: 0
    gpu_memory_utilization: 0.50
    max_num_seqs: 1
    max_num_batched_tokens: 1024
    compilation_config:
      max_cudagraph_capture_size: 1024
      cudagraph_capture_sizes: [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024]
  - stage_id: 1
    gpu_memory_utilization: 0.07
    max_num_seqs: 1
'''
(Path(os.environ['LIVE_VOICE_STORE'])/'drama3-live.yaml').write_text(config)
PY
exec bash experiments/drama3/run.sh --deploy-config "$LIVE_VOICE_STORE/drama3-live.yaml"
