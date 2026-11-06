#!/usr/bin/env bash
#SBATCH --job-name=character-omni
#SBATCH --partition=proto
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --gres=gpu:nvidia_h200:3
#SBATCH --cpus-per-task=24
#SBATCH --mem=256G
#SBATCH --time=04:00:00
#SBATCH --output=slurm-qwen-stream-%j.log

set -euo pipefail
[[ -n "${SLURM_JOB_ID:-}" && -n "${SLURM_SUBMIT_DIR:-}" ]] || {
  echo 'Submit from the repository: sbatch experiments/qwen_stream/run.slurm.sbatch' >&2; exit 1;
}
cd "$SLURM_SUBMIT_DIR"
[[ -f experiments/qwen_stream/run_stack.sh ]] || {
  echo 'Submit from the Character_Studio_Qwen repository directory.' >&2; exit 1;
}
export PYTHONUNBUFFERED=1
printf 'Qwen streaming batch job %s on %s\n' "$SLURM_JOB_ID" "$(hostname)"
# Remove only affinity inherited from a previous interactive allocation.
unset SLURM_CPU_BIND SLURM_CPU_BIND_LIST SLURM_CPU_BIND_TYPE SLURM_CPU_BIND_VERBOSE
exec srun --ntasks=1 --cpus-per-task="${SLURM_CPUS_PER_TASK:-24}" --cpu-bind=none \
  bash experiments/qwen_stream/run_stack.sh "$@"
