# Multi-GPU quality profiles

**Reference character/archive deployment:** `quality-3gpu` runs two separate Qwen text models and Drama 3, with CPU Faster-Whisper selected by `--listening baseline`. Start with the [current architecture handoff](DEVELOPER_ARCHITECTURE.md) and [screenshots](SCREENSHOTS.md). This guide's one- and two-GPU profiles remain alternatives; the main developer handoff uses three GPUs.

The live app can run its narrative director and character actor on larger models, with Drama 3 on its own GPU. These are **single-node H200 profiles**. Additional devices make space for those models; they do not change the knowledge or acting skill of an unchanged checkpoint. Quality and latency need rehearsal comparisons.

| Profile | Allocated H200s | Actor | Director | Placement |
|---|---:|---|---|---|
| `shared` | 1 | Qwen3-4B-Instruct-2507 | Separate role on the actor server | Existing compact setup |
| `quality-2gpu` | 2 | Qwen3-30B-A3B-Instruct-2507 | Separate role on the larger actor server | Drama 3 on first GPU; text roles on second |
| `quality-3gpu` | 3 | Qwen3-30B-A3B-Instruct-2507 | Separate Qwen3-32B server | Drama 3 first; actor second; director third |

**Start with `quality-3gpu` when testing a genuinely separate director model.** The two-GPU alternative upgrades the actor while sharing its server for director requests. The existing one-GPU profile remains the default, so pulling code does not silently download large checkpoints.

Official model cards: [Qwen3-30B-A3B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-30B-A3B-Instruct-2507) (non-thinking MoE), [Qwen3-32B](https://huggingface.co/Qwen/Qwen3-32B). The director uses the latter in non-thinking mode for bounded structured responses. Both are BF16 in these profiles. The actor and director each fit their own H200 allocation target; these scripts do not use tensor parallelism. vLLM describes [when to use tensor/pipeline parallelism](https://docs.vllm.ai/en/latest/serving/parallelism_scaling/). Do not infer that distributing the same model across more devices will improve its answers.

## Background job

For a server you want to keep running after closing your allocation terminal, submit the included batch file from `login01`. It requests one node in `proto`, three H200s, 24 CPUs and 256 GiB of host memory for four hours. It starts the existing `quality-3gpu` stack once in the foreground of that batch allocation; no additional model installation or download is performed.

```bash
cd "/scratch4weeks/$USER/Character_Studio_Qwen" &&
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_ed25519_github -o IdentitiesOnly=yes' git pull --ff-only &&
LIVE_DEFAULT_ENGINE=baseline LIVE_VOICE_PLAYBACK=streaming \
  sbatch scripts/run_live_slurm.sbatch --listening baseline
```

`sbatch` prints the new job ID and returns to the login prompt. The job can remain pending until resources are available. Once it starts, its combined app/startup output is in `slurm-character-JOB_ID.log` in this repository directory; replace `JOB_ID` with the returned number. Service details remain under `live-voice-store/logs/`. These local job logs are ignored by Git.

```bash
squeue -u "$USER" -o "%.18i %.20j %.2t %.10L %R"
```

Use the actual allocated node in your Mac tunnel; do not assume it is still `costar07`. The server allocation is independent of the terminal used to submit it, but the Mac SSH forwarding connection must stay open while using the browser. Closing the tunnel disconnects the browser; it does not itself stop the batch server. Batch mode does not preserve an active call's RAM-only dialogue across a client disconnect, and does not prevent scheduler cancellation, memory limits or the four-hour time limit. [Slurm batch-job behavior](https://slurm.schedmd.com/sbatch.html).

Do not start another stack from an overlapping shell while the batch job is already serving. Pull updates from the login node, stop your actual batch job with `scancel JOB_ID`, wait until it has ended, then submit the batch file again. There is no automatic job submission, retry, cancellation of other jobs or increase in the requested RAM.

The wrapper requires Slurm's job/submission-directory variables and a repository containing the live launcher. Running it with plain `bash` on the login node is rejected. It delegates to `srun` inside the batch allocation so Slurm still owns the GPU processes. Local tests use a fake `srun` and fake model launcher; no batch job was submitted during implementation.

### CPU binding during batch startup

If startup reports `CPU binding outside of job step allocation` / `Unable to satisfy cpu bind request`, the application step has not launched. Submitting a new batch job from an interactive GPU shell can carry a CPU mask for the previous allocation into the new job; site automatic-binding policy can also affect the step. This error alone does not establish a GPU or model failure.

The wrapper now clears only `SLURM_CPU_BIND`, `SLURM_CPU_BIND_LIST`, `SLURM_CPU_BIND_TYPE` and `SLURM_CPU_BIND_VERBOSE`, then explicitly starts its one step with `--cpu-bind=none` and the current allocation's `SLURM_CPUS_PER_TASK` (24 when unset). It preserves the new job's identity, CPU allocation and `CUDA_VISIBLE_DEVICES`, forwards the listening flag and propagates the application exit status. Resource requests remain three H200s and 24 CPUs. See [Slurm's CPU-binding and environment precedence](https://slurm.schedmd.com/srun.html).

Pull this update before submitting another batch job. Prefer submission from `login01`. If you already have an interactive three-GPU allocation, run `bash scripts/run_live_stack.sh --profile quality-3gpu --listening nemotron-smart-turn` inside it instead of requesting a second allocation. The baseline works by substituting `--listening baseline`. Fake-step tests cover inherited masks and preservation of current allocation variables; the actual Surrey scheduler launch still needs confirmation.

## 1. Request an interactive allocation on the Surrey login node

Stop an earlier stack and release its old allocation when finished with it. Do not launch large models on `login01`.

```bash
srun --partition=proto --nodes=1 --ntasks=1 \
  --gres=gpu:nvidia_h200:3 --cpus-per-task=24 --mem=256G \
  --time=04:00:00 --pty bash
```

This requests resources; it may queue or be rejected by the cluster's current limits. Once allocated, check `hostname`, `echo "$CUDA_VISIBLE_DEVICES"` and `nvidia-smi`. The launcher must see three distinct devices in the current Slurm allocation. It selects tokens from that list, including nonzero IDs or UUIDs, rather than assuming physical GPUs 0, 1 and 2 belong to you.

## 2. Pull and prepare in that allocated GPU terminal

```bash
cd /scratch4weeks/$USER/Character_Studio_Qwen
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_ed25519_github -o IdentitiesOnly=yes' git pull --ff-only
bash scripts/run_live_stack.sh --profile quality-3gpu --prepare
```

The working Drama 3 environment and model download are reused. `--prepare` installs only the live app dependencies and downloads the selected, revision-pinned text checkpoints. It does not upgrade vLLM, Torch or the matched CUDA wheels. The two larger BF16 text checkpoints are approximately 125–130 GB in total before cache/working-space overhead; allow at least 160 GB additional free storage. This is an estimate from parameter counts, not a measured download or GPU memory reading.

Directories under `/scratch4weeks/$USER/live-voice-store`:

- `actor-30b/`: Qwen3-30B-A3B-Instruct-2507.
- `director-32b/`: Qwen3-32B; only downloaded for the three-GPU profile.
- `llm/`: the earlier 4B model remains available for `shared`.
- `stt/`: existing small English speech recognition checkpoint, reused on CPU.
- `logs/tts.log`, `logs/llm.log`, `logs/director.log`: service logs.

Each text checkpoint is pinned in `config/live_profiles.json`; `checkpoints-<profile>.json` records its download selection. Scratch storage is temporary; follow the cluster's retention policy for material you need to keep.

After preparation, subsequent starts use:

```bash
bash scripts/run_live_stack.sh --profile quality-3gpu
```

For two GPUs, request `--gres=gpu:nvidia_h200:2` and choose `--profile quality-2gpu`. Three GPUs are required for `quality-3gpu`; the launcher refuses insufficient allocations before downloading or starting models.

## 3. Open from your Mac

The interface still uses **http://127.0.0.1:18091/**. Keep the existing tunnel if it points at the allocated node. If `hostname` now reports a different node, reconnect the tunnel to that node; use the command in [LIVE_SETUP.md](LIVE_SETUP.md#4-open-it-from-your-mac), replacing `costar07` as needed.

Only port 18091 needs forwarding. On Surrey:

| Port | Service |
|---:|---|
| 18091 | Interface, speech recognition and WebSocket orchestration |
| 18092 | Actor text API |
| 18093 | Drama 3 TTS |
| 18094 | Separate director API, three-GPU profile only |
| 18095 | Optional isolated Nemotron/Smart Turn service; no browser tunnel needed |

**Director script** displays the configured profile and model IDs. The page's ready check includes the separate director server when configured. `shared` and the two-GPU profile use separate requests on one text server; the three-GPU profile has two separate model processes on different GPUs.

## 4. Try narrative control

Load Anna's five-beat example under **Director script**. Start with **Performer only** to check voice, facts and controls. Move to **Director** to evaluate evidence-based transitions. Use Hold/Next/Previous, send a private note, and use Cue character to ask for a response to the new situation. The narrative guide explains [the control rules](NARRATIVE.md).

Compare the same participant messages and reference voice across the one- and three-GPU profiles. Check consistency, repetition, response to reassurance, whether quoted evidence warrants the next beat, audible emotion, first-audio delay and pauses between phrases. The larger director has a 12-second HTTP timeout (12.5-second outer bound); it is not guaranteed to finish that quickly or to improve performance. A failed call leaves current narrative/trigger state intact; maximum-time fallback and performer controls remain available.

## Resource and shutdown notes

The dedicated actor and director each budget 85% of their own GPU; Drama 3 budgets 70% for its main stage and 10% for its codec on its own GPU. These are configuration budgets, **not measured VRAM requirements**. Context is capped at 16,384 tokens for quality text servers. The small profile retains its existing 8,192-token cap and combined GPU budget. Recognition remains on CPU.

The launcher keeps each server in its own process group, waits for health and stops only its tracked groups on Ctrl+C. Existing ports are never taken over. Wait for cleanup before relaunching. It does not cancel the Slurm allocation; use `squeue -u "$USER"` and cancel your actual job ID when finished. The independent director can also be routed to an already running endpoint as documented in [DIRECTOR.md](DIRECTOR.md), but a quality profile's local director is managed by its launcher.

If a job is killed, inspect its accounting record before assigning a cause:

```bash
sacct -j JOB_ID --format=JobID,User,State%30,Reason%45,ExitCode,Elapsed,Timelimit,ReqMem,MaxRSS
```

`SIGKILL`/`0:9` alone does not establish out-of-memory. A per-step `MaxRSS` is that step's peak, not a timestamped simultaneous total across steps; do not add separate peaks as proof that the job exceeded its allocation. A finished job can disappear from `squeue` while remaining in `sacct`. If the original interactive allocation is cancelled, work started through another overlapping step can be terminated with it. Match cancellation UID with `id -u` and use the scheduler's `Reason` or Surrey support when the cause remains unclear. [Slurm accounting fields](https://slurm.schedmd.com/sacct.html).

**Validation:** model identities and pinned revisions were checked against their official Hugging Face repositories. CPU tests validate allocation mapping, distinct roles, bounded budgets and launch arguments. No new H200 allocation was submitted and these quality profiles have not yet been GPU-smoke-tested. Preserve the working dependency pins if a first load fails, and inspect the relevant log before changing packages.


## Separate listening experiment

**Optional listening experiment:** [Nemotron + Smart Turn setup](LISTENING_EXPERIMENT.md) adds GPU streaming input recognition to `quality-3gpu`. Pass `--listening nemotron-smart-turn` to the interactive launcher or batch script. Nemotron shares the third allocated GPU with a director budget of 75%; Smart Turn runs on CPU. The baseline budgets below remain unchanged when that option is absent. This does not request a fourth GPU.
