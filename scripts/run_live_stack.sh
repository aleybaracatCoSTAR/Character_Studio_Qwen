#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -z "${SLURM_JOB_ID:-}" || -z "${CUDA_VISIBLE_DEVICES:-}" ]]; then
  echo 'Use your allocated H200 job terminal.' >&2; exit 1
fi
export LIVE_VOICE_STORE="${LIVE_VOICE_STORE:-/scratch4weeks/$USER/live-voice-store}"
export LIVE_STT_MODEL_DIR="$LIVE_VOICE_STORE/stt"
export LIVE_STT_DEVICE="${LIVE_STT_DEVICE:-cpu}"
export DRAMA3_PORT=18093
[[ -x .venv-live/bin/python ]] || { echo 'Run bash scripts/setup_live.sh first.' >&2; exit 1; }
[[ -f "$LIVE_STT_MODEL_DIR/model.bin" && -f "$LIVE_VOICE_STORE/llm/config.json" ]] || { echo 'Run scripts/download_live_models.py first.' >&2; exit 1; }
.venv-drama3/bin/python experiments/drama3/check_install.py
.venv-live/bin/python - <<'PY'
import socket
for port in (18091,18092,18093):
    try:
        with socket.socket() as s: s.bind(('127.0.0.1',port))
    except OSError: raise SystemExit(f'Port {port} is occupied. Stop your earlier test server before launching this stack.')
PY
mkdir -p "$LIVE_VOICE_STORE/logs"
tts_pid=''; llm_pid=''
cleanup(){
  [[ -z "$tts_pid" ]] || kill -TERM "$tts_pid" 2>/dev/null || true
  [[ -z "$llm_pid" ]] || kill -TERM "$llm_pid" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
wait_health(){
  local port="$1" pid="$2" label="$3"
  for ((attempt=0;attempt<900;attempt++)); do
    if ! kill -0 "$pid" 2>/dev/null; then echo "$label exited. Read $LIVE_VOICE_STORE/logs/$label.log" >&2; return 1; fi
    if curl -fsS --max-time 2 "http://127.0.0.1:$port/health" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  echo "$label did not become healthy. Read its log." >&2; return 1
}
echo "Starting Drama 3. Log: $LIVE_VOICE_STORE/logs/tts.log"
bash scripts/run_live_tts.sh >"$LIVE_VOICE_STORE/logs/tts.log" 2>&1 & tts_pid=$!
wait_health 18093 "$tts_pid" tts
echo "Starting the text model. Log: $LIVE_VOICE_STORE/logs/llm.log"
bash scripts/run_live_llm.sh >"$LIVE_VOICE_STORE/logs/llm.log" 2>&1 & llm_pid=$!
wait_health 18092 "$llm_pid" llm
echo 'Starting the live interface on http://127.0.0.1:18091/'
echo 'Ctrl+C stops the services started by this script; the Slurm allocation is separate.'
.venv-live/bin/python -m studio --live --port 18091
