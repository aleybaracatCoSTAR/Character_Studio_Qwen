# Native streaming Qwen3-Omni comparison

This experimental profile keeps Character Studio, its character library, the separate director and the local archives. Qwen3-Omni receives the microphone audio and generates text and streaming speech itself. The existing Qwen text model + Drama 3 profile remains available unchanged.

The comparison runs **one stack at a time on three allocated H200s**:

| Allocated GPU | Native experiment | Existing baseline |
|---|---|---|
| First | Qwen3-Omni Thinker | Drama 3 |
| Second | Qwen3-Omni Talker and Code2Wav | Qwen character text model |
| Third | Qwen3-32B director | Qwen3-32B director |

These are positions in `CUDA_VISIBLE_DEVICES`, not assumptions about physical GPU numbers. CPU speech recognition still supplies a transcript for director/archive work in the default listening mode; the native model receives the original recorded audio. Input is processed at a conversation turn boundary. This profile streams the **reply**, rather than providing a continuous full-duplex speech connection.

## 1. Install once on Surrey

From the repository directory on Surrey, run:

```bash
export LIVE_VOICE_STORE="/scratch4weeks/$USER/live-voice-store"
export QWEN_MODEL_DIR="/scratch4weeks/$USER/qwen-character-store/model"
bash experiments/qwen_stream/setup.sh
```

This creates **`.venv-qwen-stream` only**. It does not change `.venv-live`, `.venv-drama3` or `.venv-listening`. Its package cache uses scratch storage. Installation and checkpoint download do not need a GPU allocation, subject to your cluster's login-node policy. If the existing portal environment is missing, prepare it separately with `bash scripts/setup_live.sh`.

The pinned Python 3.12/Linux x86_64 environment uses vLLM `0.32.0`, vLLM-Omni commit `798fef99a33fba3ad3c2439d6ec10fe838c0076b`, Transformers `5.16.1` and the same explicit CUDA 13.0 wheel set as the existing Surrey experiment. It never installs from Git HEAD. An import-only check is available:

```bash
bash experiments/qwen_stream/setup.sh --check
```

## 2. Prepare or reuse checkpoints

```bash
.venv-qwen-stream/bin/python experiments/qwen_stream/download.py
```

The original **Qwen/Qwen3-Omni-30B-A3B-Instruct**, revision `26291f793822fb6be9555850f06dfe95f2d7e695`, is reused from `QWEN_MODEL_DIR` when its original `studio-checkpoint.json` marker and required files match. Otherwise this explicit command downloads the pinned snapshot (about 71 GB if absent). It also reuses the baseline's `director-32b` and `stt` checkpoints; it downloads only those missing dependencies, not the baseline actor or Drama 3.

Custom existing locations can be supplied with `--model-dir PATH --store PATH`; set the same `QWEN_MODEL_DIR` and `LIVE_VOICE_STORE` when running the stack. Logs and generated deployment files go under `$LIVE_VOICE_STORE/qwen-stream` (`QWEN_STREAM_STORE` can override this).

```bash
.venv-qwen-stream/bin/python experiments/qwen_stream/download.py --check
```

This offline check verifies pinned identity markers, configuration, a safe complete shard index and nonempty files. It does not re-hash all 71 GB or demonstrate a successful GPU model load. No serving path downloads models automatically.

## 3. Stop the baseline stack, then start the experiment

Use Ctrl+C in the terminal running the old stack and wait for its workers to stop. This experiment refuses active listeners on ports `18091`, `18092`, `18093`, `18094` or `18097`; it does not kill an earlier server. End an unused old Slurm allocation before requesting another if you no longer need it.

In an **existing three-H200 allocation**:

```bash
cd /scratch4weeks/$USER/Character_Studio_Qwen
export LIVE_VOICE_STORE="/scratch4weeks/$USER/live-voice-store"
export QWEN_MODEL_DIR="/scratch4weeks/$USER/qwen-character-store/model"
bash experiments/qwen_stream/run_stack.sh --check
bash experiments/qwen_stream/run_stack.sh
```

For a fair comparison with the existing Nemotron/Smart Turn listening experiment, keep that same listening mode on both stacks:

```bash
bash experiments/qwen_stream/run_stack.sh --listening nemotron-smart-turn
```

This requires the already prepared `.venv-listening` and its pinned local checkpoints. It reuses the existing listening launcher on the director's GPU and reduces the director memory reservation to 75%, exactly as the baseline listening experiment does. It does not install or download listening dependencies. Use `--listening baseline` on both stacks when comparing the default CPU recognition mode.

Or, from the repository on the login node, submit a separate background allocation:

```bash
sbatch experiments/qwen_stream/run.slurm.sbatch
```

The batch launcher also forwards `--listening nemotron-smart-turn` when appended to this command.

Do not run both launch methods. The batch file requests three H200s on `proto`, 24 CPU cores, 256 GB RAM and four hours. Its log is `slurm-qwen-stream-JOBID.log`. The stack log prints the actual allocated GPU placement; model logs are `qwen-stream/logs/omni.log` and `qwen-stream/logs/director.log`. Stopping this stack stops only the workers it created. The Slurm allocation is separate; cancel a finished batch job with `scancel JOBID`.

## 4. Open the same portal address

Keep using **http://127.0.0.1:18091/**. If the new job runs on a different compute node, update the compute hostname in your Mac tunnel. For example, when Slurm reports `costar07`:

```bash
ssh -N -i ~/.ssh/id_ed25519_costar \
  -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -L 127.0.0.1:18091:127.0.0.1:18091 \
  -J jq575gb@costar-login01.surrey.ac.uk \
  jq575gb@costar07.costar-cluster.lan
```

Using the same browser origin preserves the browser's existing character library. The experimental stack selects the native engine by default; select a character and start a new call. Its built-in voice choices are **Chelsie, Ethan and Aiden**. Drama voice references/cloning and delivery tags do not transfer to these Qwen voices.

To compare again, stop this experiment and run the existing baseline command in a suitable three-GPU allocation:

```bash
bash scripts/run_live_stack.sh --profile quality-3gpu
```

Append the same `--listening nemotron-smart-turn` option if it was used for the native test.

The server ports are local-only: app `18091`, native Omni `18097`, director `18094`, and optional experimental listening `18095`. A browser engine selector does not start or move GPU models; the chosen engine must have a healthy server. Simultaneous comparison needs additional resources and manual configuration; this launcher deliberately runs sequentially.

## Runtime contract and practical limits

The experiment inherits the pinned [official Qwen3 deployment](https://github.com/vllm-project/vllm-omni/blob/798fef99a33fba3ad3c2439d6ec10fe838c0076b/vllm_omni/deploy/qwen3_omni_moe.yaml), reducing concurrent sequences to one and adjusting each stage's GPU memory reservation for H200. It retains upstream chunk sizes, the Talker seed and sampling settings, and the CUDA rotary-embedding safeguard. It does not modify installed upstream source.

The [official online serving example](https://github.com/vllm-project/vllm-omni/blob/798fef99a33fba3ad3c2439d6ec10fe838c0076b/examples/online_serving/qwen3_omni/README.md) uses `/v1/chat/completions` with streaming text and audio modalities. At this pin, audio SSE events contain base64 PCM in `choices[].delta.content`, with `audio_metadata` reporting **24,000 Hz, mono PCM16**. The provider waits for terminal completion across both modalities; a text-ending chunk alone does not end speech. Standard request `max_tokens` constrains the Thinker, while Talker/Code2Wav keep their separate deployment limits.

Native audio starts playing before the complete reply exists, so it cannot receive the baseline's complete-take speech-coverage check before playback. A stream can still stall, fail, or produce speech that diverges from its text. Director notes and retrieved material condition the model; they do not guarantee character consistency or faithful spoken output. Compare the actual responses and time to first sound. **Only CPU/fixture validation has been performed locally; GPU startup, latency and voice quality must be measured on Surrey.**
