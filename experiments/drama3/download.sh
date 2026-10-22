#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
export DRAMA3_STORE="${DRAMA3_STORE:-/scratch4weeks/$USER/drama3-test-store}"
export HF_HOME="$DRAMA3_STORE/huggingface"
export HF_HUB_DISABLE_IMPLICIT_TOKEN=1
export DRAMA3_MODEL_DIR="${DRAMA3_MODEL_DIR:-$DRAMA3_STORE/model}"
[[ -x .venv-drama3/bin/hf ]] || { echo 'Run setup.sh first.' >&2; exit 1; }
mkdir -p "$DRAMA3_MODEL_DIR"
echo 'Download fishaudio/drama-3 under the Fish Audio Community License.'
echo 'Read: https://huggingface.co/fishaudio/drama-3/blob/main/LICENSE'
echo "Destination: $DRAMA3_MODEL_DIR"
.venv-drama3/bin/hf download fishaudio/drama-3 \
  --revision b2b5ee998034b2975720906c815e7fb0e9f007bd \
  --local-dir "$DRAMA3_MODEL_DIR"
