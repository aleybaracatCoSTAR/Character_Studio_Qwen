# Live microphone + character + Drama 3

This experimental mode connects **English speech recognition → Qwen3-4B-Instruct-2507 → Drama 3**. Start a call once, speak, pause, and hear streamed phrases. The microphone waits while the character generates and plays a reply. Use **Stop reply** to take over. The original Qwen3-Omni 30B recorded-turn mode remains available separately.

**Status, 23 October 2026:** all three real Surrey services started and `/api/status` reported them ready. The first speaker-based microphone test repeatedly cancelled replies, exposing an overly eager VAD interruption path. The new protected turn-taking, voice reference, delivery and emotional-trigger controls have CPU tests and a browser preview; microphone behaviour and expressive delivery still need a listening test after deploying this update. Combined VRAM has not been measured. This is not native full-duplex speech generation. Recognition finalises each utterance after a pause.

## 1. Prepare on Surrey

Use the terminal inside your allocated H200 job, in this repository. Stop earlier Qwen or Drama 3 servers with Ctrl+C before reusing the GPU. The launcher checks ports 18091–18093 and refuses to replace an existing server.

```bash
cd /scratch4weeks/$USER/Character_Studio_Qwen
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_ed25519_github -o IdentitiesOnly=yes' git pull --ff-only
```

The launcher reuses the working Drama 3 `.venv-drama3` GPU environment and model download. The live app and CPU speech recognition use `.venv-live`, keeping their dependencies separate. For a fresh installation, first complete [Drama 3 setup and its voice test](../experiments/drama3/README.md). That setup pins vLLM 0.32.0 and the Omni revision, with **CUDA 13.0 Torch/Audio/Vision wheels**. Your driver may report CUDA 13.2; that does not require CUDA 13.2 Python wheels.

## 2. Download the additional listening and text models

The `--prepare` launch command below installs the live app environment, then fetches a roughly 8 GB small text checkpoint and a small English Faster-Whisper checkpoint into `/scratch4weeks/$USER/live-voice-store`. The download resumes existing files. This mode uses Qwen3-4B for dialogue; the original Qwen-Omni 30B checkpoint remains a separate experiment.

For separate manual preparation, use `bash scripts/setup_live.sh` followed by `.venv-live/bin/python scripts/download_live_models.py`.

If Drama 3 has not been downloaded already, also run:

```bash
bash experiments/drama3/download.sh
```

Drama 3 remains in `/scratch4weeks/$USER/drama3-test-store`. Review its [Fish Audio license](https://huggingface.co/fishaudio/drama-3/blob/main/LICENSE) for your intended use. New app code is MIT; model terms are separate.

## 3. Launch all services

```bash
bash scripts/run_live_stack.sh --prepare
```

Keep this terminal open. After preparation, the script starts Drama 3, waits for it, starts the small text model, then opens the live application. It reports progress while models load and shows the end of the relevant log if a service fails. Subsequent launches can use `bash scripts/run_live_stack.sh` to reuse the prepared files directly. The page reports whether all three services are ready.

Both model launchers default to `VLLM_USE_FLASHINFER_SAMPLER=0`, using vLLM's native GPU sampler. This carries forward the workaround for the missing-`ninja` FlashInfer sampler crash found in the first Surrey test. It affects token sampling, not whether the model runs on the GPU. You can explicitly override this after preparing a working FlashInfer kernel/toolchain configuration.

| Port | Service | Log |
|---|---|---|
| 18091 | Live interface + WebSocket | Current terminal |
| 18092 | Small Qwen text model | `live-voice-store/logs/llm.log` |
| 18093 | Drama 3 TTS | `live-voice-store/logs/tts.log` |

Speech recognition runs on the CPU in the interface process. The experimental GPU split reserves 50% for Drama 3's main stage, 7% for its codec and 18% for the text model. These are memory budgets, not measured usage. They target one H200 and one caller. See `scripts/run_live_tts.sh` and `scripts/run_live_llm.sh`.

If startup stops, read the indicated log, for example:

```bash
tail -n 60 /scratch4weeks/$USER/live-voice-store/logs/tts.log
```

Do not proceed to tunnel debugging while a model server cannot start. Do not change system MPS services if the cluster rejects the runtime's private MPS setup; retain that error for the administrator.

## 4. Open it from your Mac

Check `hostname` in the GPU terminal. For **costar07**, run this in a separate **Mac terminal**:

```bash
ssh -N -i ~/.ssh/id_ed25519_costar -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -L 127.0.0.1:18091:127.0.0.1:18091 \
  -J jq575gb@costar-login01.surrey.ac.uk \
  jq575gb@costar07.costar-cluster.lan
```

Replace `costar07` if your allocation uses another node. Leave the tunnel open and visit **http://127.0.0.1:18091/**. Only this port needs forwarding; the app connects to its model servers on Surrey.

## 5. Try the call

1. Choose **Show details**, edit Anna, then wait for **Voice services ready**.
2. Choose **Test with text** to check reasoning and streamed playback without a microphone.
3. End that session, choose **Start call**, and grant microphone access. Speak a short sentence and pause for about half a second.
4. Let the answer finish, or choose **Stop reply** to take over. Only phrases that finished playing enter the model's remembered response. The microphone resumes 350 ms after playback, allowing speaker echo to decay.

Headphones help prevent output returning through the microphone. The server discards microphone frames throughout recognition/generation/playback and the echo tail, rather than buffering them as another turn. The endpoint uses 480 ms of silence; each utterance is capped at 15 seconds. Transcripts are produced after each detected utterance, not word by word.

Short text clauses go to Drama 3 before the full answer finishes. Raw audio chunks are scheduled immediately in Web Audio. Without an uploaded reference, each call's first generated phrase becomes a synthetic voice reference for later phrases. This does not guarantee voice consistency and does not clone the participant's microphone input.

### Voice and emotion

Open **How they speak**. The speaking-style text guides the dialogue. **Emotion** (Natural, Calm, Excited, Sad, Angry, Anxious or Whispering) and **Expression** (None, Sigh or Laugh) add inline Drama 3 delivery tags to TTS input. These two controls remain available during a call and apply to the next answer. Tags stay out of visible dialogue and conversation history. Events are inserted once per answer. Short single-word phrases receive no tag.

To select a different voice, choose a **2–15 second uncompressed PCM16 WAV**, mono or stereo at 8–48 kHz, no larger than 2 MB, and enter its exact spoken words. The browser previews the selected clip, then sends it in the bounded start message. Drama 3 receives it as `ref_audio` plus `ref_text`. It is kept in RAM for the call, not written into the character profile or automatically saved to browser storage. Reselect it after reloading. End the call before changing a reference.

This is voice conditioning, not a named preset-voice catalogue. Emotional tags are natural-language cues rather than guaranteed acoustic settings. They are inserted after the opening words because leading tags can stall some takes. See the [Drama 3 recipe](https://recipes.vllm.ai/fishaudio/drama-3#event-and-emotion-tags) and [model examples](https://huggingface.co/fishaudio/drama-3).

### Emotional trigger: Anna's reset button

Open **Emotional trigger**. Anna starts with an editable example: **Press reset → Anxious → fear of losing her memories; ask the visitor to stop**. Configure the stage action, feeling and changed behaviour before the call. During the call, **Press reset** fires it immediately and produces a reaction; this intentional stage action may interrupt the current answer. **Clear reaction** resolves the state for subsequent answers. Core identity and biography remain intact.

The default exact spoken cue is **“I pressed your reset button”**. Once recognised, it activates the same state before dialogue and audio generation. Questions such as “What does your reset button do?” do not activate it. This is explicit cue matching, not general intent recognition. The action button is a rehearsal control; no physical reset-button hardware is connected yet.

The active state persists across replies, adds the behavioural direction to the current scene prompt and overrides vocal emotion with `[anxious]` (or the selected trigger emotion). Clearing it restores the ordinary voice setting and tells the character the threat is resolved. The model still needs to be evaluated for how convincingly it performs that state.

## 6. Measure rather than guess

The page shows recognition, first text token and first TTS chunk timing. **Server to first audio** starts near the detected end of speech and ends when the server receives its first PCM chunk. It excludes SSH/network delivery and browser output buffering; it does not measure first audio heard. Typed tests omit the speech/pause path. Compare several warmed turns as well as first synthesis.

Record errors, time to first audible reply, pauses between phrases, whether interruption works, and whether Anna keeps her facts. A more responsive pipeline does not by itself establish better character intelligence.

## 7. Finish

End the call, then press Ctrl+C in the launch terminal. The script terminates only the model servers it started. This does **not** release the Slurm allocation. When finished with the GPU, inspect `squeue -u "$USER"` and cancel your actual current job ID with `scancel`.

## Files and protocol

`live_voice/app.py` owns one same-origin WebSocket call. `turns.py` detects utterances and bounds history. `providers.py` wraps local STT, streamed text and streamed TTS. `session.py` runs the pipeline and cancels requests. `frontend/live.js` handles the microphone; `live-audio.mjs` schedules playback and acknowledges completed phrases. Audio frames carry turn and phrase IDs so old chunks are discarded after interruption.

Character details, trigger definitions and ordinary delivery selections are saved in the local browser. Reference clips are not. The live app keeps audio and conversation in RAM for the active call, does not write recordings or transcripts, and sends inference requests to the configured local services. Review those services' logs and the host environment before using sensitive material. The single-caller localhost setup is not a public multi-user deployment.

## UI-only preview

With the development requirements installed, no GPU or model download is required:

```bash
python -m studio --live --preview --port 18097
```

Open http://127.0.0.1:18097/. Calls are disabled and no AI replies are simulated.
