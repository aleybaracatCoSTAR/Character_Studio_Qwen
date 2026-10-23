# Multi-GPU quality profiles

The live app can run its narrative director and character actor on larger models, with Drama 3 on its own GPU. These are **single-node H200 profiles**. Additional devices make space for those models; they do not change the knowledge or acting skill of an unchanged checkpoint. Quality and latency need rehearsal comparisons.

| Profile | Allocated H200s | Actor | Director | Placement |
|---|---:|---|---|---|
| `shared` | 1 | Qwen3-4B-Instruct-2507 | Separate role on the actor server | Existing compact setup |
| `quality-2gpu` | 2 | Qwen3-30B-A3B-Instruct-2507 | Separate role on the larger actor server | Drama 3 on first GPU; text roles on second |
| `quality-3gpu` | 3 | Qwen3-30B-A3B-Instruct-2507 | Separate Qwen3-32B server | Drama 3 first; actor second; director third |

**Start with `quality-3gpu` when testing a genuinely separate director model.** The two-GPU alternative upgrades the actor while sharing its server for director requests. The existing one-GPU profile remains the default, so pulling code does not silently download large checkpoints.

Official model cards: [Qwen3-30B-A3B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-30B-A3B-Instruct-2507) (non-thinking MoE), [Qwen3-32B](https://huggingface.co/Qwen/Qwen3-32B). The director uses the latter in non-thinking mode for bounded structured responses. Both are BF16 in these profiles. The actor and director each fit their own H200 allocation target; these scripts do not use tensor parallelism. vLLM describes [when to use tensor/pipeline parallelism](https://docs.vllm.ai/en/latest/serving/parallelism_scaling/). Do not infer that distributing the same model across more devices will improve its answers.

## 1. Request the allocation on the Surrey login node

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

**Director script** displays the configured profile and model IDs. The page's ready check includes the separate director server when configured. `shared` and the two-GPU profile use separate requests on one text server; the three-GPU profile has two separate model processes on different GPUs.

## 4. Try narrative control

Load Anna's five-beat example under **Director script**. Start with **Performer only** to check voice, facts and controls. Move to **Director** to evaluate evidence-based transitions. Use Hold/Next/Previous, send a private note, and use Cue character to ask for a response to the new situation. The narrative guide explains [the control rules](NARRATIVE.md).

Compare the same participant messages and reference voice across the one- and three-GPU profiles. Check consistency, repetition, response to reassurance, whether quoted evidence warrants the next beat, audible emotion, first-audio delay and pauses between phrases. The larger director has a 12-second HTTP timeout (12.5-second outer bound); it is not guaranteed to finish that quickly or to improve performance. A failed call leaves current narrative/trigger state intact; maximum-time fallback and performer controls remain available.

## Resource and shutdown notes

The dedicated actor and director each budget 85% of their own GPU; Drama 3 budgets 70% for its main stage and 10% for its codec on its own GPU. These are configuration budgets, **not measured VRAM requirements**. Context is capped at 16,384 tokens for quality text servers. The small profile retains its existing 8,192-token cap and combined GPU budget. Recognition remains on CPU.

The launcher keeps each server in its own process group, waits for health and stops only its tracked groups on Ctrl+C. Existing ports are never taken over. Wait for cleanup before relaunching. It does not cancel the Slurm allocation; use `squeue -u "$USER"` and cancel your actual job ID when finished. The independent director can also be routed to an already running endpoint as documented in [DIRECTOR.md](DIRECTOR.md), but a quality profile's local director is managed by its launcher.

**Validation:** model identities and pinned revisions were checked against their official Hugging Face repositories. CPU tests validate allocation mapping, distinct roles, bounded budgets and launch arguments. No new H200 allocation was submitted and these quality profiles have not yet been GPU-smoke-tested. Preserve the working dependency pins if a first load fails, and inspect the relevant log before changing packages.
