#!/usr/bin/env bash
#SBATCH --job-name=qwen-studio
#SBATCH --partition=proto
#SBATCH --gres=gpu:nvidia_h200:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=96G
#SBATCH --time=04:00:00
#SBATCH --output=logs/qwen-%j.log

set -euo pipefail
cd "$SLURM_SUBMIT_DIR"
export QWEN_STORE="${QWEN_STORE:-/scratch4weeks/$USER/qwen-character-store}"
export QWEN_MODEL_DIR="${QWEN_MODEL_DIR:-$QWEN_STORE/model}"
export QWEN_PORT="${QWEN_PORT:-18091}"
echo "Job $SLURM_JOB_ID on $(hostname), port $QWEN_PORT"
# Dependencies and checkpoint must already exist. No installation or download here.
exec bash scripts/run.sh
