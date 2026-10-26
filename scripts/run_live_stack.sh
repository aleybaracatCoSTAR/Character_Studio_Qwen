#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
prepare=0
export LIVE_PROFILE="${LIVE_PROFILE:-shared}"
while (($#)); do
  case "$1" in
    --prepare) prepare=1; shift ;;
    --profile) [[ $# -gt 1 ]] || { echo 'Supply a profile name.' >&2; exit 1; }; export LIVE_PROFILE="$2"; shift 2 ;;
    --help|-h)
      echo 'Usage: bash scripts/run_live_stack.sh [--profile shared|quality-2gpu|quality-3gpu] [--prepare]'
      echo 'Profiles require 1, 2 or 3 allocated H200s on the same node. --prepare downloads missing checkpoints.'
      exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done
if [[ -z "${SLURM_JOB_ID:-}" || -z "${CUDA_VISIBLE_DEVICES:-}" ]]; then
  echo 'Use your allocated H200 job terminal.' >&2; exit 1
fi
export LIVE_VOICE_STORE="${LIVE_VOICE_STORE:-/scratch4weeks/$USER/live-voice-store}"
export LIVE_STT_MODEL_DIR="$LIVE_VOICE_STORE/stt"
export LIVE_STT_DEVICE="${LIVE_STT_DEVICE:-cpu}"
export DRAMA3_PORT=18093
export PATH="$PWD/.venv-drama3/bin:$HOME/.local/bin:$PATH"
# The first Surrey test hit FlashInfer's sampler JIT without a CUDA build toolchain.
# Keep sampling on vLLM's native GPU path unless explicitly overridden.
export VLLM_USE_FLASHINFER_SAMPLER="${VLLM_USE_FLASHINFER_SAMPLER:-0}"
[[ -x .venv-drama3/bin/python ]] || { echo 'Install Drama 3 first: bash experiments/drama3/setup.sh' >&2; exit 1; }
.venv-drama3/bin/python - <<'PYGPU'
import os
from live_voice.deployment import profile,assigned_devices
try:
    cfg=profile();devices=assigned_devices(cfg,os.environ['CUDA_VISIBLE_DEVICES'])
except ValueError as exc:raise SystemExit(str(exc))
print('Live profile:',os.environ['LIVE_PROFILE'],'· allocated placement:',devices,flush=True)
PYGPU
.venv-drama3/bin/python experiments/drama3/check_install.py
.venv-drama3/bin/python - <<'PY'
from live_voice.deployment import profile,check_ports
ports=[18091,18092,18093]+([18094] if profile()['director'] else [])
try: check_ports(ports)
except ValueError as exc: raise SystemExit(str(exc))
PY
if [[ "$prepare" == 1 ]]; then
  echo 'Preparing the live app environment; reusing the working Drama 3 GPU environment.'
  bash scripts/setup_live.sh
  .venv-live/bin/python scripts/download_live_models.py
fi
[[ -x .venv-live/bin/python ]] || { echo 'Run bash scripts/run_live_stack.sh --prepare for first-time setup.' >&2; exit 1; }
.venv-live/bin/python - <<'PYMODELS'
import os
from pathlib import Path
from live_voice.deployment import profile
cfg=profile();store=Path(os.environ['LIVE_VOICE_STORE'])
paths=[store/'stt/model.bin']+[store/cfg[role]['directory']/'config.json' for role in ('actor','director') if cfg[role]]
missing=[str(p) for p in paths if not p.is_file()]
if missing:raise SystemExit('Missing models: '+', '.join(missing)+'. Run this profile with --prepare.')
PYMODELS
mkdir -p "$LIVE_VOICE_STORE/logs"
tts_pid=''; llm_pid=''; director_pid=''
cleanup(){
  trap - EXIT
  trap '' INT TERM
  local groups=()
  [[ -z "$tts_pid" ]] || groups+=("$tts_pid")
  [[ -z "$llm_pid" ]] || groups+=("$llm_pid")
  [[ -z "$director_pid" ]] || groups+=("$director_pid")
  if ((${#groups[@]})); then
    echo "Stopping this launch's inference servers and waiting for their workers to exit…"
    .venv-live/bin/python scripts/live_process.py stop "${groups[@]}" || true
    [[ -z "$tts_pid" ]] || wait "$tts_pid" 2>/dev/null || true
    [[ -z "$llm_pid" ]] || wait "$llm_pid" 2>/dev/null || true
    [[ -z "$director_pid" ]] || wait "$director_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
wait_health(){
  local port="$1" pid="$2" label="$3"
  local next_update=$((SECONDS+30))
  for ((attempt=0;attempt<900;attempt++)); do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "$label exited. Last log lines:" >&2
      tail -n 45 "$LIVE_VOICE_STORE/logs/$label.log" >&2
      return 1
    fi
    if curl -fsS --max-time 2 "http://127.0.0.1:$port/health" >/dev/null 2>&1; then return 0; fi
    if ((SECONDS >= next_update)); then
      echo "Waiting for $label to load. Log: $LIVE_VOICE_STORE/logs/$label.log"
      next_update=$((SECONDS+30))
    fi
    sleep 1
  done
  echo "$label did not become healthy. Read its log." >&2; return 1
}
echo "Starting Drama 3. Log: $LIVE_VOICE_STORE/logs/tts.log"
.venv-live/bin/python scripts/live_process.py start bash scripts/run_live_tts.sh >"$LIVE_VOICE_STORE/logs/tts.log" 2>&1 & tts_pid=$!
wait_health 18093 "$tts_pid" tts
echo "Starting the text model. Log: $LIVE_VOICE_STORE/logs/llm.log"
.venv-live/bin/python scripts/live_process.py start bash scripts/run_live_llm.sh >"$LIVE_VOICE_STORE/logs/llm.log" 2>&1 & llm_pid=$!
wait_health 18092 "$llm_pid" llm
if .venv-live/bin/python -c 'from live_voice.deployment import profile; raise SystemExit(0 if profile()["director"] else 1)'; then
  echo "Starting the separate director model. Log: $LIVE_VOICE_STORE/logs/director.log"
  .venv-live/bin/python scripts/live_process.py start bash scripts/run_live_director.sh >"$LIVE_VOICE_STORE/logs/director.log" 2>&1 & director_pid=$!
  wait_health 18094 "$director_pid" director
  export LIVE_DIRECTOR_URL=http://127.0.0.1:18094
  export LIVE_DIRECTOR_MODEL=live-director
fi
export LIVE_DIRECTOR_TIMEOUT="${LIVE_DIRECTOR_TIMEOUT:-$(.venv-live/bin/python -c 'from live_voice.deployment import profile; print(profile()["director_timeout"])')}"
echo 'Starting the live interface on http://127.0.0.1:18091/' 
echo 'Ctrl+C stops the services started by this script; the Slurm allocation is separate.'
.venv-live/bin/python -m studio --live --port 18091
