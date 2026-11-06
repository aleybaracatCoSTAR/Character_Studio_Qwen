#!/usr/bin/env bash
#SBATCH --job-name=character-live
#SBATCH --partition=proto
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --gres=gpu:nvidia_h200:3
#SBATCH --cpus-per-task=24
#SBATCH --mem=256G
#SBATCH --time=04:00:00
#SBATCH --output=slurm-character-%j.log

# Submit from the repository with sbatch; the allocation is independent of SSH.
set -euo pipefail
if [[ -z "${SLURM_JOB_ID:-}" || -z "${SLURM_SUBMIT_DIR:-}" ]]; then
  echo 'Submit this file from the repository with sbatch scripts/run_live_slurm.sbatch.' >&2
  exit 1
fi
cd "$SLURM_SUBMIT_DIR"
[[ -f scripts/run_live_stack.sh ]] || {
  echo 'Submit from the Character_Studio_Qwen repository directory.' >&2
  exit 1
}
export PYTHONUNBUFFERED=1
printf 'Character Studio batch job %s on %s\n' "$SLURM_JOB_ID" "$(hostname)"
if character_revision=$(git rev-parse --short HEAD 2>/dev/null); then
  printf 'Code revision: %s\n' "$character_revision"
fi
export LIVE_VOICE_PLAYBACK="${LIVE_VOICE_PLAYBACK:-streaming}"
printf 'Drama 3 playback: %s\n' "$LIVE_VOICE_PLAYBACK"
if [[ "$LIVE_VOICE_PLAYBACK" == 'streaming' ]]; then
  echo 'Audio streams before the full take is checked; incomplete takes never restart after playback begins.'
fi
if [[ "${LIVE_VERIFY_VOICE:-1}" == '0' ]]; then
  echo 'Spoken-word verification: disabled by environment'
else
  echo 'Spoken-word verification: enabled'
fi
# sbatch may have been submitted from inside an interactive srun allocation.
# Its CPU mask names the old allocation's cores and is invalid in the new job.
# Keep the new job/GPU allocation variables; clear only inherited affinity.
unset SLURM_CPU_BIND SLURM_CPU_BIND_LIST SLURM_CPU_BIND_TYPE SLURM_CPU_BIND_VERBOSE
exec srun --ntasks=1 --cpus-per-task="${SLURM_CPUS_PER_TASK:-24}" --cpu-bind=none \
  bash scripts/run_live_stack.sh --profile quality-3gpu "$@"
