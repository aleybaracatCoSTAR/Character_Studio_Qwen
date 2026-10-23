# Live microphone + character + Drama 3

This experimental mode connects **English speech recognition → director → Qwen3-4B-Instruct-2507 actor → Drama 3**. Start a call once, speak, pause, and hear streamed phrases. The microphone waits while the character generates and plays a reply. Use **Stop reply** to take over. The original Qwen3-Omni 30B recorded-turn mode remains available separately.

**Status, 23 October 2026:** all three real Surrey services started and `/api/status` reported them ready. The first speaker-based microphone test repeatedly cancelled replies, exposing an overly eager VAD interruption path. The protected turn-taking and initial voice/trigger controls were deployed; the user reports literal-only trigger recognition and mostly flat vocal emotion. The older trigger-only contextual controller subsequently matched 11/12 small fictional cases; three voice comparisons were generated, with no established listening preference. A new [director orchestrator](DIRECTOR.md) now plans actor intentions and TTS performance together; its live quality and added latency remain unverified. Combined VRAM has not been measured. This is not native full-duplex speech generation. Recognition finalises each utterance after a pause.

For the old demo's narrative pattern, open [Narrative beats](NARRATIVE.md). To use larger actor/director models across two or three allocated H200s, follow [Multi-GPU setup](MULTI_GPU.md). The commands below retain the compact one-GPU default.

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

### Director

Open **Director** to enable guidance and enter the scene intention. The director plans a next conversational move and vocal delivery on each turn; inspect **Latest director note** above the transcript. It defaults to a separate request to the existing Qwen server. See [architecture, separate LLM configuration and limits](DIRECTOR.md). Changing the scene direction applies to the next reply. The actor prompt now asks for progression rather than repeated biography; this is a prompting change, not a model upgrade.

### Voice and emotion

Open **How they speak**. The speaking-style text guides the dialogue. **Emotion** (Natural, Calm, Excited, Sad, Angry, Anxious or Whispering) and **Expression** (None, Sigh or Laugh) add inline Drama 3 delivery tags to TTS input. These two controls remain available during a call and apply to the next answer. Tags stay out of visible dialogue and conversation history. Events are inserted once per answer. Add a **Vocal direction**, or leave it blank to use a descriptive emotion preset. Emotional replies retain complete sentences up to a 200-character chunk target, rather than splitting at every comma; natural replies keep the earlier short chunks. This trades some first-audio delay for more performance context. Short single-word phrases receive no tag.

Choose **Voice → Female** or **Male** for a bundled British reference, or **Automatic** to anchor on the first synthetic phrase. The preview plays the reference recording, not a new synthesis. Credits and license are in [assets/voices/NOTICE.md](../assets/voices/NOTICE.md). For a custom voice, choose **Custom reference**, then a **2–15 second uncompressed PCM16 WAV**, mono or stereo at 8–48 kHz, no larger than 2 MB, and enter its exact spoken words. The browser previews the selected clip, then sends it in the bounded start message. Drama 3 receives it as `ref_audio` plus `ref_text`. It is kept in RAM for the call, not written into the character profile or automatically saved to browser storage. Reselect it after reloading. End the call before changing a reference.

These choices use real reference conditioning, rather than a named Drama 3 preset-voice catalogue. Emotional tags are natural-language cues rather than guaranteed acoustic settings. They are inserted after the opening words because leading tags can stall some takes. See the [Drama 3 recipe](https://recipes.vllm.ai/fishaudio/drama-3#event-and-emotion-tags) and [model examples](https://huggingface.co/fishaudio/drama-3).

### Emotional trigger: Anna's reset button

Open **Emotional trigger**. Anna starts with an editable example: **Press reset → Anxious → fear of losing her memories; ask the visitor to stop**. Configure the stage action, feeling and changed behaviour before the call. During the call, **Press reset** fires it immediately and produces a reaction; this intentional stage action may interrupt the current answer. **Clear reaction** resolves the state for subsequent answers. Core identity and biography remain intact.

The **Example wording** is an illustration, not a string to match. With the director disabled, the same local Qwen model makes a separate trigger-only JSON decision from character facts, the configured trigger and recent conversation: **activate**, **release** or **hold**. It is prompted to interpret paraphrases, pronouns, threatened or completed actions, negation, hypothetical discussion and reassurance. For example, “My finger is already on it” can activate anxiety when earlier dialogue established that “it” is the memory-reset button. A question about how reset works alone should hold the normal state; backing away and withdrawing the threat can release anxiety. These are intended behaviours, not measured accuracy guarantees. The action button remains a direct rehearsal control; no physical hardware is connected yet.

With the director enabled, this trigger decision is included in its planning call instead of running an additional classifier; its intention reaches the actor and its delivery reaches TTS. The following timing describes the legacy trigger-only path. The decision is validated before use. Context-check failure leaves the current state unchanged and is shown in the interface; there is no hidden keyword or exact-match fallback. The context request uses deterministic JSON output, a four-second HTTP timeout and a 4.5-second outer bound. Its measured time appears separately from dialogue generation. The additional model request is sequential on the existing one-sequence text server, so it adds latency without loading another model.

**How should it sound?** sets a vocal-performance description for the trigger, separate from behavioural direction. Drama 3 receives both the emotion label and performance descriptor, inserted after the opening words. A neutral reference may also influence delivery; fuller cues do not guarantee convincing acted emotion. An appropriately performed reference clip can be tested through the existing voice-reference upload.

The active state persists across replies, adds the behavioural direction to the current scene prompt and overrides vocal emotion with `[anxious]` (or the selected trigger emotion). Contextual resolution or the Clear reaction button restores the ordinary voice setting and tells the character the threat is resolved. Threats are described as threats, rather than falsely treated as actions that have already happened. The model still needs to be evaluated for how convincingly it performs that state.

## 6. Measure rather than guess

For a real-model contextual check, run in the GPU checkout after pulling the updated code (the existing model services can stay running):

```bash
.venv-live/bin/python scripts/evaluate_emotion.py
```

Add `--speech` to generate a listening comparison: the same words and synthetic voice reference with natural delivery, the previous short emotion tag and fuller performance direction. Results are written on the machine running the script under ignored `outputs/emotion-evaluation/`: `context-results.json`, three WAV files and `listen.html`. The 12 fictional test cases are a small diagnostic set, not a general intelligence benchmark. Audio duration and request timing do not establish emotional quality. End the live call before running these tests. No model service is restarted by the script.

The page shows recognition, first text token and first TTS chunk timing. **Server to first audio** starts near the detected end of speech and ends when the server receives its first PCM chunk. It excludes SSH/network delivery and browser output buffering; it does not measure first audio heard. Typed tests omit the speech/pause path. Compare several warmed turns as well as first synthesis.

Record errors, time to first audible reply, pauses between phrases, whether interruption works, and whether Anna keeps her facts. A more responsive pipeline does not by itself establish better character intelligence.

## 7. Finish

End the call, then press Ctrl+C in the launch terminal. The script starts each inference server in its own process group and terminates only those groups. Allow up to 20 seconds for graceful shutdown; remaining workers in those groups are then stopped. Wait for the terminal prompt before relaunching. This does **not** release the Slurm allocation. When finished with the GPU, inspect `squeue -u "$USER"` and cancel your actual current job ID with `scancel`.

If a server from an older launcher still holds a port, inspect it in the allocated GPU terminal with `ss -ltnp '( sport = :18092 or sport = :18093 )'`. Identify its owning process before sending a stop signal. The launcher refuses to replace a server occupying its ports and does not automatically kill processes discovered there.

## Files and protocol

`live_voice/app.py` owns one same-origin WebSocket call. `turns.py` detects utterances and bounds history. `providers.py` wraps local STT, streamed text and streamed TTS. `session.py` runs the pipeline and cancels requests. `frontend/live.js` handles the microphone; `live-audio.mjs` schedules playback and acknowledges completed phrases. Audio frames carry turn and phrase IDs so old chunks are discarded after interruption.

Character details, trigger definitions and ordinary delivery selections are saved in the local browser. Reference clips are not. The live app keeps audio and conversation in RAM for the active call, does not write recordings or transcripts, and sends inference requests to the configured local services. Review those services' logs and the host environment before using sensitive material. The single-caller localhost setup is not a public multi-user deployment.

## UI-only preview

With the development requirements installed, no GPU or model download is required:

```bash
python -m studio --live --preview --port 18097
```

Open http://127.0.0.1:18097/. Calls are disabled and no AI replies are simulated.
