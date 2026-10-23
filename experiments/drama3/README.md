# Drama 3 voice test

**Drama 3 is text-to-speech.** It can speak scripted character dialogue; it does not listen to an audience or decide a character's next reply. This optional experiment is separate from the Qwen application, with its own `.venv-drama3` environment, store and port. No model is downloaded or GPU job started by adding these files.

Built with Fish Audio. Read the [model card](https://huggingface.co/fishaudio/drama-3) and the actual [Fish Audio Community License](https://huggingface.co/fishaudio/drama-3/blob/main/LICENSE) before use. The model has custom terms; it is not Apache/MIT. This starter is for an internal evaluation. The model card's metadata and license heading differ, so use the linked license file for the terms rather than assuming the metadata is complete.

## What we are testing

Three short original Anna lines: a calm introduction, frustration and hope. The client requests raw streamed PCM, writes playable WAVs, and measures arrival of the first audio bytes, total completion time and audio duration. First audio received is not the same as first audio heard. Optional `--play` sends arriving PCM to an existing local `ffplay` installation. Otherwise, listen afterwards in the generated `listen.html`.

This tests voice and delivery, not character reasoning. Without a reference, voice identity is not guaranteed across requests. To compare deliveries in a consistent voice, supply a short reference WAV with its exact transcript using `--ref-audio` and `--ref-text`. Those files go to your own model server, not Fish Audio's hosted API.

## 1. Use your Surrey GPU terminal

If Qwen is still running on the GPU you intend to reuse, stop that server with Ctrl+C first. Keep the allocation. Do not start both memory-hungry servers on that same GPU for this initial test.

From the repository root:

```bash
git status
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_ed25519_github -o IdentitiesOnly=yes' git pull --ff-only
bash experiments/drama3/setup.sh
```

The setup uses vLLM **0.32.0** with the pinned vLLM-Omni 0.32 development revision. It explicitly installs the CUDA **13.0** wheels for Torch **2.13.0**, TorchAudio **2.11.0** and TorchVision **0.28.0**, matching [vLLM's declared dependencies](https://github.com/vllm-project/vllm/blob/v0.32.0/requirements/cuda.txt). The audio and Torch version numbers differ intentionally. Their CUDA builds must agree. All packages are installed together, then their serving imports are checked before loading a model. The [official recipe](https://recipes.vllm.ai/fishaudio/drama-3) provides the serving API and hardware reference, but its older 0.31 installation pin is incompatible with this newer Omni revision. Its reference GPU is an H100 80 GB; your H200 is the intended first test target here. GPU inference still needs validation. Installation and initial compilation can take time.

### Repair an earlier installation

For **Torch/TorchVision `+cu132` with TorchAudio `+cu130`**, pull the latest repository and run the focused CUDA repair. It uses exact official wheel URLs and SHA-256 hashes from `requirements-cu130.txt`, rather than choosing the build from an index. It targets the existing Surrey Linux x86_64 Python 3.12 environment and keeps model downloads:

```bash
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_ed25519_github -o IdentitiesOnly=yes' git pull --ff-only
bash experiments/drama3/repair_cuda.sh
```

Wait for **Serving imports OK** before running `bash experiments/drama3/run.sh`. The repair replaces Torch, Audio and Vision and lets the resolver install their required runtime libraries. It does not reinstall the two serving packages or fetch model weights. The full setup also uses these direct wheel pins. An earlier successful setup that still shows `cu132` does not establish how those packages were installed; preserve its output if this recurs.

If you instead see `cannot import name 'async_request_profile'` or a vLLM/Omni version mismatch, use `bash experiments/drama3/setup.sh` to install the serving pair as well. The driver reporting CUDA 13.2 does not require CUDA 13.2 Python wheels. Do not use `--torch-backend=auto` for this environment. The preflight requires all three package versions to end in `+cu130` and imports the actual serving CLI. The corrected runtime dependency range is in [Omni's pinned requirements](https://github.com/vllm-project/vllm-omni/blob/798fef99a33fba3ad3c2439d6ec10fe838c0076b/requirements/common.txt); the missing function is in [vLLM 0.32.0](https://github.com/vllm-project/vllm/blob/v0.32.0/vllm/benchmarks/lib/endpoint_request_func.py). A failed GPU server never opens port 18093, so a Mac health-check failure is expected until this is repaired.

## 2. Download and launch

```bash
bash experiments/drama3/download.sh
bash experiments/drama3/run.sh
```

The model revision is pinned to `b2b5ee998034b2975720906c815e7fb0e9f007bd`. The default store is `/scratch4weeks/$USER/drama3-test-store`; these are different weights from Qwen. Port **18093** is private to this test. The official runtime's single-GPU configuration is used. Its CUDA profile enables private MPS for its two stages; if the cluster refuses it, save the error rather than changing system services.

Wait for the server to finish loading. In another terminal on that same compute node:

```bash
curl --fail http://127.0.0.1:18093/health
```

The first synthesis may compile kernels. The client sends one warmup request before measuring the examples. Compare warmed results rather than treating startup as conversational latency. A 300-token generation cap bounds each short example; raise it only if a longer line is cut off.

## 3. Tunnel from your Mac

Replace `YOUR_COMPUTE_NODE` with the node running Drama 3:

```bash
ssh -N -i ~/.ssh/id_ed25519_costar -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -L 127.0.0.1:18093:127.0.0.1:18093 \
  -J jq575gb@costar-login01.surrey.ac.uk \
  jq575gb@YOUR_COMPUTE_NODE.costar-cluster.lan
```

## 4. Run the client on your Mac

From your local copy of this repository, in a separate Mac terminal:

```bash
python3 experiments/drama3/test_voice.py
```

It uses only Python's standard library. Audio and measurements are saved **on the machine where this client runs**. Open `outputs/drama3-voice-test/listen.html` in a browser to compare the three WAVs. To hear incoming chunks during generation, if `ffplay` is already installed locally:

```bash
python3 experiments/drama3/test_voice.py --play
```

For one custom line:

```bash
python3 experiments/drama3/test_voice.py --text "I have been waiting all morning for someone to ask me that."
```

You can also run the client on Surrey, then copy the output files back. The `--play` option is intended for the computer with speakers, not a headless GPU node.

## 5. Finish

Stop the Drama 3 server with Ctrl+C. When you are done with the allocation, use `squeue -u "$USER"`, cancel that job's actual ID with `scancel`, and check the queue again. Closing an SSH tunnel does not release the GPU.

## Validation

On 23 October 2026, a real request through the Mac-to-Surrey SSH tunnel succeeded for "Hello, I am Anna." with `--warmup 0 --max-tokens 120`: first PCM at 3.084 s, complete response at 3.304 s, 2.786 s of mono 44.1 kHz audio. This is one request, not a warmed latency benchmark or a voice-quality evaluation. Generated files stay in ignored `outputs/`, not Git.

The request must **omit `sample_rate`**: this pinned Drama 3 endpoint rejects it, including a value of 44100. The client writes returned PCM at the model's native 44.1 kHz. Both the standalone client and live provider use this contract. Server validation errors now include the response's actual reason rather than only "400 Bad Request".

The user also reached a warmup crash because FlashInfer sampling tried to invoke missing `ninja`. For that specific path, `export VLLM_USE_FLASHINFER_SAMPLER=0` before launch selects vLLM's native GPU sampler. This does not disable all FlashInfer components. Keep the existing matched CUDA wheels. Kernel setup is separate from request validation.

The client and request/stream handling have CPU tests. Combined live-stack GPU memory use, microphone interaction and expressive voice consistency remain unvalidated. The client reports end-to-end transport timing, not a promise of the model card's benchmark numbers.
