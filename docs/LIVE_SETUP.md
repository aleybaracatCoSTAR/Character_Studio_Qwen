# Live microphone + character + Drama 3

This experimental mode connects **English speech recognition → Qwen3-4B-Instruct-2507 → Drama 3**. Start a call once, speak, pause, and hear streamed phrases. Speaking again cancels the previous reply. The original Qwen3-Omni 30B recorded-turn mode remains available separately.

**Status:** application, transport, cancellation and audio scheduling have CPU tests. The browser preview has been inspected. The complete three-service GPU stack, combined VRAM use, voice consistency and conversational delay still need a Surrey H200 test. This is not native full-duplex speech generation. Recognition finalises each utterance after a pause.

## 1. Prepare on Surrey

Use the terminal inside your allocated H200 job, in this repository. Stop earlier Qwen or Drama 3 servers with Ctrl+C before reusing the GPU. The launcher checks ports 18091–18093 and refuses to replace an existing server.

```bash
cd /scratch4weeks/$USER/Character_Studio_Qwen
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_ed25519_github -o IdentitiesOnly=yes' git pull --ff-only
bash experiments/drama3/setup.sh
bash scripts/setup_live.sh
```

Drama 3 uses its own `.venv-drama3` GPU environment. The live app and CPU speech recognition use `.venv-live`, keeping their dependencies separate. Setup pins vLLM 0.32.0 and the Omni revision, with **CUDA 13.0 Torch/Audio/Vision wheels**. It checks serving imports before launching a model. Your driver may report CUDA 13.2; that does not require CUDA 13.2 Python wheels. Existing model downloads are kept.

## 2. Download the additional listening and text models

```bash
.venv-live/bin/python scripts/download_live_models.py
```

This explicitly downloads a roughly 8 GB small text checkpoint and a small English Faster-Whisper checkpoint into `/scratch4weeks/$USER/live-voice-store`. It does not load the original Qwen-Omni 30B checkpoint.

If Drama 3 has not been downloaded already, also run:

```bash
bash experiments/drama3/download.sh
```

Drama 3 remains in `/scratch4weeks/$USER/drama3-test-store`. Review its [Fish Audio license](https://huggingface.co/fishaudio/drama-3/blob/main/LICENSE) for your intended use. New app code is MIT; model terms are separate.

## 3. Launch all services

```bash
bash scripts/run_live_stack.sh
```

Keep this terminal open. The script starts Drama 3, waits for it, starts the small text model, then opens the live application. First startup and synthesis can compile kernels. The page reports whether all three services are ready.

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
4. Speak again during a reply to interrupt, or choose **Stop reply**. Only phrases that finished playing enter the model's remembered response.

Headphones help prevent output returning through the microphone. The endpoint uses 480 ms of silence; each utterance is capped at 15 seconds. Transcripts are produced after each detected utterance, not word by word.

Short text clauses go to Drama 3 before the full answer finishes. Raw audio chunks are scheduled immediately in Web Audio. Each call's first generated phrase becomes a synthetic voice reference for later phrases. This does not guarantee voice consistency and does not clone the participant's voice. The speaking-style field guides dialogue; precise emotional delivery needs a separate evaluation.

## 6. Measure rather than guess

The page shows recognition, first text token and first TTS chunk timing. **Server to first audio** starts near the detected end of speech and ends when the server receives its first PCM chunk. It excludes SSH/network delivery and browser output buffering; it does not measure first audio heard. Typed tests omit the speech/pause path. Compare several warmed turns as well as first synthesis.

Record errors, time to first audible reply, pauses between phrases, whether interruption works, and whether Anna keeps her facts. A more responsive pipeline does not by itself establish better character intelligence.

## 7. Finish

End the call, then press Ctrl+C in the launch terminal. The script terminates only the model servers it started. This does **not** release the Slurm allocation. When finished with the GPU, inspect `squeue -u "$USER"` and cancel your actual current job ID with `scancel`.

## Files and protocol

`live_voice/app.py` owns one same-origin WebSocket call. `turns.py` detects utterances and bounds history. `providers.py` wraps local STT, streamed text and streamed TTS. `session.py` runs the pipeline and cancels requests. `frontend/live.js` handles the microphone; `live-audio.mjs` schedules playback and acknowledges completed phrases. Audio frames carry turn and phrase IDs so old chunks are discarded after interruption.

Character details are saved in the local browser. The live app keeps audio and conversation in RAM for the active call, does not write recordings or transcripts, and sends inference requests to the configured local services. Review those services' logs and the host environment before using sensitive material. The single-caller localhost setup is not a public multi-user deployment.

## UI-only preview

With the development requirements installed, no GPU or model download is required:

```bash
python -m studio --live --preview --port 18097
```

Open http://127.0.0.1:18097/. Calls are disabled and no AI replies are simulated.
