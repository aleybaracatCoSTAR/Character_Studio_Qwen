#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
check_only=0
export LIVE_LISTENING_MODE="${LIVE_LISTENING_MODE:-baseline}"
while (($#)); do
case "$1" in
  --help|-h)
    echo 'Usage: bash experiments/qwen_stream/run_stack.sh [--check] [--listening baseline|nemotron-smart-turn]'
    echo 'Sequential comparison: three allocated H200s; stop the baseline stack first.'
    echo '--check validates imports, checkpoints, allocation and free ports without loading models.'
    exit 0 ;;
  --check) check_only=1; shift ;;
  --listening) [[ $# -gt 1 ]] || { echo 'Supply a listening mode.' >&2; exit 1; }; export LIVE_LISTENING_MODE="$2"; shift 2 ;;
  *) echo 'Unknown option. Use --help.' >&2; exit 1 ;;
esac
done
[[ "$LIVE_LISTENING_MODE" == baseline || "$LIVE_LISTENING_MODE" == nemotron-smart-turn ]] || {
  echo 'Choose listening mode baseline or nemotron-smart-turn.' >&2; exit 1;
}
[[ -n "${SLURM_JOB_ID:-}" && -n "${CUDA_VISIBLE_DEVICES:-}" ]] || {
  echo 'Use an allocated three-H200 Slurm job.' >&2; exit 1;
}
[[ -x .venv-qwen-stream/bin/python && -x .venv-qwen-stream/bin/vllm ]] || {
  echo 'Run bash experiments/qwen_stream/setup.sh first.' >&2; exit 1;
}
[[ -x .venv-live/bin/python ]] || {
  echo 'The portal app environment is missing. Run bash scripts/setup_live.sh first.' >&2; exit 1;
}
export LIVE_VOICE_STORE="${LIVE_VOICE_STORE:-/scratch4weeks/$USER/live-voice-store}"
export QWEN_STREAM_STORE="${QWEN_STREAM_STORE:-$LIVE_VOICE_STORE/qwen-stream}"
export UV_CACHE_DIR="$QWEN_STREAM_STORE/uv-cache"
export HF_HUB_CACHE="${HF_HUB_CACHE:-$QWEN_STREAM_STORE/huggingface/hub}"
export LIVE_PROFILE=quality-3gpu
export LIVE_STT_MODEL_DIR="$LIVE_VOICE_STORE/stt"
export LIVE_STT_DEVICE=cpu
export LIVE_DEFAULT_ENGINE=qwen-omni
export LIVE_OMNI_URL=http://127.0.0.1:18097
export LIVE_OMNI_MODEL=Qwen/Qwen3-Omni-30B-A3B-Instruct
export LIVE_LLM_URL=http://127.0.0.1:18092
export LIVE_TTS_URL=http://127.0.0.1:18093
export LIVE_DIRECTOR_URL=http://127.0.0.1:18094
export LIVE_DIRECTOR_MODEL=live-director
export LIVE_DIRECTOR_TIMEOUT="${LIVE_DIRECTOR_TIMEOUT:-12}"
export VLLM_USE_FLASHINFER_SAMPLER="${VLLM_USE_FLASHINFER_SAMPLER:-0}"
export PYTHONUNBUFFERED=1
.venv-qwen-stream/bin/python experiments/qwen_stream/check_install.py
.venv-qwen-stream/bin/python experiments/qwen_stream/runtime.py --check
if [[ "$LIVE_LISTENING_MODE" == nemotron-smart-turn ]]; then
  [[ -x .venv-listening/bin/python ]] || { echo 'Run bash scripts/setup_listening.sh first.' >&2; exit 1; }
  .venv-listening/bin/python - <<'PYLISTEN'
from pathlib import Path
import os
from scripts.download_listening_models import validate_store
validate_store(Path(os.environ.get('LISTENING_STORE') or str(Path(os.environ['LIVE_VOICE_STORE'])/'listening')))
print('Experimental listening checkpoints verified; no model was loaded.', flush=True)
PYLISTEN
fi
if ((check_only)); then exit 0; fi
mkdir -p "$QWEN_STREAM_STORE/logs"
omni_pid=''; director_pid=''; listening_pid=''
cleanup(){
  trap - EXIT
  trap '' INT TERM
  local groups=()
  [[ -z "$omni_pid" ]] || groups+=("$omni_pid")
  [[ -z "$director_pid" ]] || groups+=("$director_pid")
  [[ -z "$listening_pid" ]] || groups+=("$listening_pid")
  if ((${#groups[@]})); then
    echo "Stopping only this launch's Qwen streaming and director workers…"
    .venv-live/bin/python scripts/live_process.py stop "${groups[@]}" || true
    [[ -z "$omni_pid" ]] || wait "$omni_pid" 2>/dev/null || true
    [[ -z "$director_pid" ]] || wait "$director_pid" 2>/dev/null || true
    [[ -z "$listening_pid" ]] || wait "$listening_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
wait_health(){
  local port="$1" pid="$2" label="$3" next_update=$((SECONDS+30)) deadline=$((SECONDS+1200))
  while ((SECONDS < deadline)); do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "$label exited. Last log lines:" >&2
      tail -n 45 "$QWEN_STREAM_STORE/logs/$label.log" >&2
      return 1
    fi
    if curl -fsS --max-time 2 "http://127.0.0.1:$port/health" >/dev/null 2>&1; then return 0; fi
    if ((SECONDS >= next_update)); then
      echo "Waiting for $label. Log: $QWEN_STREAM_STORE/logs/$label.log"
      next_update=$((SECONDS+30))
    fi
    sleep 1
  done
  echo "$label did not become healthy; inspect its log." >&2
  return 1
}
if [[ "$LIVE_LISTENING_MODE" == nemotron-smart-turn ]]; then
  echo "Starting experimental listening. Log: $QWEN_STREAM_STORE/logs/listening.log"
  .venv-live/bin/python scripts/live_process.py start bash scripts/run_live_listening.sh \
    >"$QWEN_STREAM_STORE/logs/listening.log" 2>&1 & listening_pid=$!
  wait_health 18095 "$listening_pid" listening
  export LIVE_LISTENING_URL=http://127.0.0.1:18095
fi
echo "Starting native Qwen3-Omni. Log: $QWEN_STREAM_STORE/logs/omni.log"
.venv-live/bin/python scripts/live_process.py start .venv-qwen-stream/bin/python \
  experiments/qwen_stream/runtime.py --role omni >"$QWEN_STREAM_STORE/logs/omni.log" 2>&1 & omni_pid=$!
wait_health 18097 "$omni_pid" omni
echo "Starting the separate director. Log: $QWEN_STREAM_STORE/logs/director.log"
.venv-live/bin/python scripts/live_process.py start .venv-qwen-stream/bin/python \
  experiments/qwen_stream/runtime.py --role director >"$QWEN_STREAM_STORE/logs/director.log" 2>&1 & director_pid=$!
wait_health 18094 "$director_pid" director
echo 'Starting the same Character Studio interface: http://127.0.0.1:18091/'
echo 'This profile streams native Qwen audio; the Qwen+Drama baseline remains a separate launch.'
echo 'Ctrl+C stops this launch; its Slurm allocation is separate.'
.venv-live/bin/python -m studio --live --port 18091
