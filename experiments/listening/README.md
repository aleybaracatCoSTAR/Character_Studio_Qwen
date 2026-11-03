# Experimental GPU listening: Nemotron + Smart Turn

This optional listening path uses NVIDIA Nemotron for cache-aware streaming English speech recognition and Smart Turn v3.2 for waveform-based end-of-turn decisions. The character model, director, Drama 3 voice, character library and archive remain part of the existing portal. This is an experiment to measure responsiveness; setup or file checks do not establish a working GPU run or a latency improvement.

The experiment has its own **`.venv-listening`** environment and **`$LIVE_VOICE_STORE/listening`** model store. Setup does not install into `.venv-live` or `.venv-drama3`. It does not install NeMo, TorchAudio, TorchVision, GPU ONNX Runtime, or the upstream Smart Turn requirements file.

## 1. Install the isolated environment

From this checkout on Surrey Linux x86-64:

```bash
cd /scratch4weeks/$USER/Character_Studio_Qwen
export LIVE_VOICE_STORE="/scratch4weeks/$USER/live-voice-store"
bash scripts/setup_listening.sh
```

Setup uses the existing `uv` command to create a Python 3.12 environment, installs the pinned packages in `requirements.txt`, and performs CPU imports plus feature-extractor construction. It loads no checkpoint and runs no GPU inference. A GPU allocation is not needed for setup or downloading files.

The isolated PyTorch wheel is **2.13.0+cu130**, matching the existing CUDA 13.0 wheel family. Transformers is **5.13.0**, which includes native `AutoModelForRNNT` support. Nemotron's feature extractor also requires **librosa 0.11.0**, including when the input is raw audio samples. Smart Turn uses **onnxruntime 1.26.0 CPU**. The setup checks these imports without altering the working voice-model environments.

Package pins and metadata were checked for Python 3.12 compatibility; an actual installation and GPU inference still need to be tested on Surrey. There is no automatic setup on portal launch.

## 2. Explicitly download the pinned model files

```bash
.venv-listening/bin/python scripts/download_listening_models.py --download
```

This command is the only step that fetches checkpoints. It downloads the Transformers checkpoint and configuration/tokenizer files for:

- `nvidia/nemotron-speech-streaming-en-0.6b`
- Revision `ebe59e5a817142986528bbbee5dba8db7b38ed50`

It separately downloads exactly:

- `pipecat-ai/smart-turn-v3`
- Revision `f766f81d3cfdf7737ac64aad813d91bbfd56bf93`
- **`smart-turn-v3.2-cpu.onnx`**

The download does not select the `.nemo` or GGUF Nemotron variants, Smart Turn v3.1, or the larger GPU ONNX artifact. The models are public; this downloader disables implicit Hugging Face token use. It also saves Nemotron's model card and accompanying bias/privacy/safety/explainability documents.

Files are stored here:

```text
$LIVE_VOICE_STORE/listening/
  nemotron/
    model.safetensors
    config.json
    generation_config.json
    processor_config.json
    tokenizer.json
    tokenizer_config.json
    listening-checkpoint.json
    ... model documentation
  smart-turn/
    smart-turn-v3.2-cpu.onnx
    smart-turn-v3.2-cpu.onnx.json
```

The sidecar manifests record exact source revisions, allowed filenames, sizes and SHA-256 hashes. Repeating `--download` reuses the files if all checks pass. No model is loaded by the downloader.

To verify files offline:

```bash
.venv-listening/bin/python scripts/download_listening_models.py --check
```

This reads and hashes the files; it makes no network requests, imports no model runtime, and does not access a GPU. A missing file, changed hash, wrong architecture, wrong revision or symbolic-link model path fails the check. The server uses local files only rather than downloading missing checkpoints during a call.

`LISTENING_STORE` can override the listening store, for example before setup, download and launch. Keep the same value for all three. The store includes a separate `uv-cache` used during package installation.

## 3. Ready to start

After setup and the offline file check pass, use your allocated **three-H200** job terminal:

```bash
cd /scratch4weeks/$USER/Character_Studio_Qwen
export LIVE_VOICE_STORE="/scratch4weeks/$USER/live-voice-store"
bash scripts/run_live_stack.sh --profile quality-3gpu --listening nemotron-smart-turn
```

The experimental service uses port **18095** internally; the portal remains on **18091**, with your existing portal tunnel. No extra browser-facing speech endpoint is needed. The launcher assigns the listener to the director's allocated GPU and reduces the director reservation for this experiment; it does not request a fourth GPU.

To return to the existing listening path, stop the stack with Ctrl+C and restart without the flag:

```bash
bash scripts/run_live_stack.sh --profile quality-3gpu
```

Stopping the stack and releasing a Slurm GPU allocation remain separate actions.

## What to test

The Nemotron checkpoint is English-only. Start with English speech and the same character, prompts and microphone setup used with the existing listener. The initial streaming configuration uses six lookahead tokens, corresponding to **560 ms audio chunks**. That chunk duration is a model setting, not a guarantee of total conversational delay. Smart Turn analyzes waveform context to help distinguish a hesitation from the end of a turn; it is not a speech recognizer.

Compare recognition accuracy, missed words, interrupted pauses, time from the last spoken word to the first reply, and behavior around background audience noise. Keep a fallback to the existing listener until an actual GPU test establishes that this path works better for the experience. No speedup or H200 timing is implied by CPU tests.

## Official sources and licenses

- [NVIDIA Nemotron model card and Transformers streaming example](https://huggingface.co/nvidia/nemotron-speech-streaming-en-0.6b/tree/ebe59e5a817142986528bbbee5dba8db7b38ed50). This checkpoint is governed by the [NVIDIA Open Model License](https://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-open-model-license/), not an Apache model license. The card states commercial and noncommercial use are supported under its terms.
- [Transformers 5.13 Nemotron feature extractor](https://github.com/huggingface/transformers/blob/v5.13.0/src/transformers/models/nemotron_asr_streaming/feature_extraction_nemotron_asr_streaming.py), including the Torch/librosa dependencies.
- [Smart Turn v3.2 pinned artifacts](https://huggingface.co/pipecat-ai/smart-turn-v3/tree/f766f81d3cfdf7737ac64aad813d91bbfd56bf93). The model card declares **BSD-2-Clause** licensing. Smart Turn is a separate model with its own terms.

Weights, caches and environment directories stay outside the published source files. This experiment performs inference; it does not fine-tune models or upload audio to a hosted inference service.
