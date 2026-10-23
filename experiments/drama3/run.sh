#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
export PATH="$PWD/.venv-drama3/bin:$HOME/.local/bin:$PATH"
export VLLM_USE_FLASHINFER_SAMPLER="${VLLM_USE_FLASHINFER_SAMPLER:-0}"
export DRAMA3_STORE="${DRAMA3_STORE:-/scratch4weeks/$USER/drama3-test-store}"
export HF_HOME="$DRAMA3_STORE/huggingface"
export HF_HUB_DISABLE_IMPLICIT_TOKEN=1
export DRAMA3_MODEL_DIR="${DRAMA3_MODEL_DIR:-$DRAMA3_STORE/model}"
if [[ -z "${SLURM_JOB_ID:-}" || -z "${CUDA_VISIBLE_DEVICES:-}" ]]; then
  echo 'Start this inside your allocated GPU job.' >&2; exit 1
fi
[[ -x .venv-drama3/bin/vllm ]] || { echo 'Run setup.sh first.' >&2; exit 1; }
[[ -f "$DRAMA3_MODEL_DIR/config.json" ]] || { echo 'Run download.sh first.' >&2; exit 1; }
.venv-drama3/bin/python experiments/drama3/check_install.py
exec .venv-drama3/bin/vllm serve "$DRAMA3_MODEL_DIR" --omni \
  --served-model-name fishaudio/drama-3 --host 127.0.0.1 --port "${DRAMA3_PORT:-18093}" "$@"
