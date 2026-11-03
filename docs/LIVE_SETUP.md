# Live microphone + character + Drama 3

**3 November: optional faster-listening trial.** [Try Nemotron GPU streaming recognition + Smart Turn v3.2](LISTENING_EXPERIMENT.md) with the existing three-GPU profile. It has a separate environment, temporary live transcript and explicit comparison/rollback commands. The default baseline and whole-reply emotional delivery remain available. Actual Surrey timing and GPU fit still need testing.

This experimental mode connects **English speech recognition → director → Qwen3-4B-Instruct-2507 actor → Drama 3**. Start a call once, speak, pause, and hear one continuous voice take for each reply. The microphone waits while the character generates and plays a reply. Use **Stop reply** to take over. The original Qwen3-Omni 30B recorded-turn mode remains available separately.

**Status, 27 October 2026:** the current code adds earlier recognition preparation and bounded conversation recall to the [director orchestrator](DIRECTOR.md). CPU tests cover final-audio matching, cancellation, heard-history retrieval and transport. No live-model check was performed for this update, so there is no measured H200 speed improvement or new listening-quality result. The complete-reply TTS validation and one-emotion-per-reply policy remain unchanged. This is a cascaded voice system: recognition finalises each utterance after a pause.

For the old demo's narrative pattern, open [Narrative beats](NARRATIVE.md). To use larger actor/director models across two or three allocated H200s, follow [Multi-GPU setup](MULTI_GPU.md). The commands below retain the compact one-GPU default.

For an existing three-GPU installation that should run independently of an allocation terminal, submit `sbatch scripts/run_live_slurm.sbatch` from this repository on `login01`. It requests a single four-hour `proto` job with three H200s, 24 CPUs and 256 GiB host RAM, and starts the existing stack once. It does not request resources until you submit it. See [background-job instructions and logs](MULTI_GPU.md#background-job); the browser still needs its SSH tunnel to the actual allocated node.

For the ready-made **KADEN** station character, pull and restart the live stack, refresh the page and use the Character picker above Name. Your existing browser character remains selected on first load and retains separate settings. End a call before switching. KADEN loads five manual narrative beats and a male voice reference. Follow the [Lighthouse quick test](KADEN.md) for caller cues and the operator-selected ending; no additional model downloads are needed.

For a **character with an archive**, open `/archive/` after the same restart. Mika is an editable fictional keeper of a selected Queering the Map collection. Add a story by its numeric ID/individual entry URL, or paste a selected passage. Each character chooses which saved stories it may consult. Sources appear beside replies and in the request inspector. No extra models or installation are needed. This prototype searches selected stories, not the whole map; see [Archive mode](ARCHIVE.md) for data storage, attribution and limits.

## 1. Prepare on Surrey

Use the terminal inside your allocated H200 job, in this repository. Stop earlier Qwen or Drama 3 servers with Ctrl+C before reusing the GPU. The launcher checks ports 18091–18093 and refuses to replace an existing server.

```bash
cd /scratch4weeks/$USER/Character_Studio_Qwen
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_ed25519_github -o IdentitiesOnly=yes' git pull --ff-only
```

The launcher reuses the working Drama 3 `.venv-drama3` GPU environment and model download. The live app and CPU speech recognition use `.venv-live`, keeping their dependencies separate. For a fresh installation, first complete [Drama 3 setup and its voice test](../experiments/drama3/README.md). That setup pins vLLM 0.32.0 and the Omni revision, with **CUDA 13.0 Torch/Audio/Vision wheels**. Your driver may report CUDA 13.2; that does not require CUDA 13.2 Python wheels.

**29 October sampling correction:** a normal live TTS start now applies one byte-verified acoustic-head correction in the pinned environment and gives main-token sampling its full candidate capacity. It retains an original backup and refuses unknown source. No dependencies or checkpoints are reinstalled. Stop the old job, pull and restart before testing; [details, rollback and current batch commands](DRAMA3_SAMPLING.md).

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

If a port is reported in use, inspect actual listeners in the GPU terminal with `ss -ltnp '( sport = :18091 or sport = :18092 or sport = :18093 or sport = :18094 )'`. An earlier plain-bind preflight could falsely reject a recently closed connection in TIME_WAIT even when this list was empty. Pull the corrected launcher: it accepts a normal TCP restart while still refusing active listeners. An empty list is not a reason to kill processes or cancel the allocation.

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
2. Choose **Test with text** to check reasoning and reply playback without a microphone.
3. End that session, choose **Start call**, and grant microphone access. Speak a short sentence and pause for about half a second.
4. Let the answer finish, or choose **Stop reply** to take over. Only a reply that finished playing enters the model's remembered response. An interrupted reply is conservatively omitted. The microphone resumes 350 ms after playback, allowing speaker echo to decay.

Headphones help prevent output returning through the microphone. The server discards microphone frames throughout final recognition/generation/playback and the echo tail, rather than buffering them as another turn. The endpoint uses **400 ms of silence**; each utterance is capped at 15 seconds. The browser sends microphone batches every **40 ms**, previously 80 ms. The server still examines fixed 20 ms audio frames.

For an established utterance, speech recognition can start provisionally after **200 ms of silence**, while the server continues waiting for the 400 ms endpoint. Provisional and final audio retain the same 80 ms trailing silence. Only an exact byte-for-byte match permits the final turn to reuse that work. If speech resumes and changes the audio, the draft is discarded and the final utterance is recognised separately. No provisional transcript reaches the conversation, director or actor. Typed input, stage actions and call closure clear outstanding preparation. This moves some recognition work into the pause; it does not establish a measured response-time improvement. Visible transcripts still appear only after the final utterance, not word by word.

The complete actor reply goes to Drama 3 as one take, with one fixed delivery cue. Transport and speech checks finish before release to Web Audio, which builds a small 120 ms reserve and combines tiny packets into 60 ms batches. Its final short buffer is always flushed at the native sample rate. A limited take can be recovered only after exact normalized recognition and a strict quiet-tail check; then only the verified quiet suffix is trimmed, keeping 400 ms after the last nonquiet block. Other retained PCM samples are unchanged. See [sentence completion](VOICE_COMPLETION.md) for the earlier measured regression and current retry limits. Automatic voice anchors only a complete accepted take lasting 1–30 seconds; selected/uploaded references stay fixed. This does not guarantee voice consistency and does not clone the participant's microphone input.

### Conversation context

To inspect the actual input used for a reply, choose **See what the model sees** beside the conversation title. Select the actor, director, emotion-check or voice request and copy its JSON. Character facts, applied notes, conversation history and TTS delivery cues are visible; reference audio itself is omitted. See [the viewer guide](MODEL_INPUTS.md). The window receives updates only while open, and its snapshots clear when the session ends.

During an active call, the app keeps the latest **12 user/assistant pairs** and a RAM archive bounded to **100 turns or 100,000 characters**. When a new question relates to older material, a small lexical search can return up to **three older original quote pairs**, alongside recent dialogue. It does not generate a summary or invent replacement facts. Retrieval can miss paraphrases or select an unhelpful match; these bounds do not guarantee perfect recall or character consistency.

Conversation content is limited to **10,000 characters for the actor**, **3,000 for story-aware director requests** and **6,000 for ordinary director requests**. Those are conversation budgets, separate from character and instruction text. Only acknowledged, fully played AI replies enter assistant memory; generated but unheard replies do not. A private stage cue can direct its current turn but is excluded from later participant recall; the AI speech that follows it can be retained once played. This memory belongs to the current call only: there is no cross-session retention, disk transcript or additional summary model.

### Director

Open **Director script** in the character sidebar to enable guidance and enter the scene intention. The director plans a next conversational move and vocal delivery on each turn. Above the transcript, **Show director controls** expands the current beat, performer controls and latest director note; **Hide director controls** restores conversation space. This panel starts collapsed and incoming updates preserve your choice. The director defaults to a separate request to the existing Qwen server. See [architecture, separate LLM configuration and limits](DIRECTOR.md). Changing the scene direction applies to the next reply.

Entering another narrative beat prompts one character reply. Performer moves, timer moves and between-turn director advances wait for participant speech and existing playback. When a participant turn itself advances the beat, its answer serves as the new beat's reply. **Cue character** remains a separate rehearsal action. See [narrative rules](NARRATIVE.md).

### Voice and emotion

Open **How they speak**. The speaking-style text guides the dialogue. **Emotion** (Natural, Calm, Excited, Sad, Angry, Anxious or Whispering), optional **Expression** (None, Sigh or Laugh) and **Vocal direction** become one descriptive cue before the whole spoken reply. Nothing is inserted after its first two words. Delivery is fixed for the reply; live control changes apply to the next answer. The cue stays out of visible dialogue and heard history. A sigh or laugh is requested only at the start of the reply. The whole reply is synthesized as one utterance, so the voice is not reset between short sentences.

Choose **Voice → Female** or **Male** for a bundled British reference, or **Automatic** to anchor on the first synthetic reply. The preview plays the reference recording, not a new synthesis. Credits and license are in [assets/voices/NOTICE.md](../assets/voices/NOTICE.md). For a custom voice, choose **Custom reference**, then a **2–15 second uncompressed PCM16 WAV**, mono or stereo at 8–48 kHz, no larger than 2 MB, and enter its exact spoken words. The browser previews the selected clip, then sends it in the bounded start message. Drama 3 receives it as `ref_audio` plus `ref_text`. It is kept in RAM for the call, not written into the character profile or automatically saved to browser storage. Reselect it after reloading. End the call before changing a reference.

These choices use real reference conditioning, rather than a named Drama 3 preset-voice catalogue. Emotional cues are natural language, not guaranteed acoustic settings. The tested format uses a speaker marker followed by one descriptive cue, rather than stacked emotion labels. Unfinished takes are discarded before playback and retried once with the identical reply, emotional cue and reference; the page explains the retry. No retry switches a sentence to natural delivery. See [the real-model comparison](VOICE_COMPLETION.md) and [model examples](https://huggingface.co/fishaudio/drama-3).

When a valid stream reaches its generation allowance, the client distinguishes verified full speech followed by quiet audio from an incomplete or active ending. Exact normalized recognition of the trimmed waveform plus at least 800 ms of strict quiet tail can qualify it for playback. Otherwise it remains blocked. Only actively voiced output with a clean recognized prefix can enlarge the second request's token allowance, up to 1,600; a silent stall does not get more time. This retains the shared two-attempt maximum and the same words, reference and emotion. See [token-limit handling](VOICE_FAILURES.md#reaching-the-limit-does-not-reveal-what-was-spoken).

The live app now checks substantial spoken-word coverage with its existing recognizer before playback, including takes that report protocol completion but speak only a prefix. This adds a recognition step and retains the same two-attempt voice budget. Failure messages identify the rejection reason and safe counts. A recognition failure/timeout withholds unchecked audio. Automatic references must be complete accepted takes lasting 1–30 seconds. See [voice failure diagnosis](VOICE_FAILURES.md) for reason codes, service logs, ASR limitations and the explicit diagnostic opt-out. No new GPU listening result is implied.

Temporary network/service failures share the same single automatic retry as an incomplete take. If voice generation still fails before any audio is released, **Retry voice** appears beside the message. It regenerates the saved reply's voice with its original words and delivery; it does not ask the actor or director for a new answer. Retry notices clear after success. The final failure message remains visible, and a new turn or story revision invalidates the previous retry.

### Emotional trigger: Anna's reset button

Open **Emotional trigger**. Anna starts with an editable example: **Press reset → Anxious → fear of losing her memories; ask the visitor to stop**. Configure the stage action, feeling and changed behaviour before the call. During the call, **Press reset** fires it immediately and produces a reaction; this intentional stage action may interrupt the current answer. **Clear reaction** resolves the state for subsequent answers. Core identity and biography remain intact.

The **Example wording** is an illustration, not a string to match. With the director disabled, the same local Qwen model makes a separate trigger-only JSON decision from character facts, the configured trigger and recent conversation: **activate**, **release** or **hold**. It is prompted to interpret paraphrases, pronouns, threatened or completed actions, negation, hypothetical discussion and reassurance. For example, “My finger is already on it” can activate anxiety when earlier dialogue established that “it” is the memory-reset button. A question about how reset works alone should hold the normal state; backing away and withdrawing the threat can release anxiety. These are intended behaviours, not measured accuracy guarantees. The action button remains a direct rehearsal control; no physical hardware is connected yet.

With the director enabled, this trigger decision is included in its planning call instead of running an additional classifier; its intention reaches the actor and its delivery reaches TTS. The following timing describes the legacy trigger-only path. The decision is validated before use. Context-check failure leaves the current state unchanged and is shown in the interface; there is no hidden keyword or exact-match fallback. The context request uses deterministic JSON output, a four-second HTTP timeout and a 4.5-second outer bound. Its measured time appears separately from dialogue generation. The additional model request is sequential on the existing one-sequence text server, so it adds latency without loading another model.

**How should it sound?** sets a vocal-performance description for the trigger, separate from behavioural direction. The selected emotion's descriptive voice qualities and this performance direction form one cue before the whole reply. A neutral reference may also influence delivery; cues do not guarantee convincing acted emotion. An appropriately performed reference clip can be tested through the existing voice-reference upload.

The active state persists across replies, adds the behavioural direction to the current scene prompt and overrides the ordinary vocal emotion with the selected trigger emotion. Contextual resolution or the Clear reaction button restores the ordinary voice setting and tells the character the threat is resolved. Threats are described as threats, rather than falsely treated as actions that have already happened. The model still needs to be evaluated for how convincingly it performs that state.

## 6. Measure rather than guess

For a real-model contextual check, run in the GPU checkout after pulling the updated code (the existing model services can stay running):

```bash
.venv-live/bin/python scripts/evaluate_emotion.py
```

Add `--speech` to generate a listening comparison: the same words and synthetic voice reference with natural delivery, the previous short emotion tag and fuller performance direction. Results are written on the machine running the script under ignored `outputs/emotion-evaluation/`: `context-results.json`, three WAV files and `listen.html`. The 12 fictional test cases are a small diagnostic set, not a general intelligence benchmark. Audio duration and request timing do not establish emotional quality. End the live call before running these tests. No model service is restarted by the script.

The page separates **Final pause**, **Recognition wait**, **Director**, **Reply text** and **Complete voice take**. Recognition wait measures the remaining ASR wait after the endpoint, not the whole recognition job; “started during pause” appears only when exact matching prepared audio was reused. Reply text measures the actor's complete response, rather than only its first token. Complete voice take includes the completion validation required before any TTS audio is released, including a retry when needed. It is not a raw streaming first-chunk measurement.

**Server estimate to first audio** runs from the estimated end of speech to the server's first released validated PCM. It excludes SSH/network delivery and browser output buffering, and is not a measurement of the first sound heard. Typed tests omit the speech/pause path. Compare warmed turns with first synthesis and record browser-observed time to audible speech separately. No live-model timing check was run for this update.

Record errors, time to first audible reply, pauses between phrases, whether interruption works, and whether Anna keeps her facts. A more responsive pipeline does not by itself establish better character intelligence.

## 7. Finish

End the call, then press Ctrl+C in the launch terminal. The script starts each inference server in its own process group and terminates only those groups. Allow up to 20 seconds for graceful shutdown; remaining workers in those groups are then stopped. Wait for the terminal prompt before relaunching. This does **not** release the Slurm allocation. When finished with the GPU, inspect `squeue -u "$USER"` and cancel your actual current job ID with `scancel`.

If a server from an older launcher still holds a port, inspect it in the allocated GPU terminal with `ss -ltnp '( sport = :18092 or sport = :18093 )'`. Identify its owning process before sending a stop signal. The launcher refuses to replace a server occupying its ports and does not automatically kill processes discovered there.

## Files and protocol

`live_voice/app.py` owns one same-origin WebSocket call. `turns.py` detects utterances and bounds/retrieves history. `recognition.py` prepares ASR and permits reuse only for identical final audio. `providers.py` wraps local STT, streamed text and validated TTS. `session.py` runs the pipeline and cancels requests. `frontend/live.js` handles the microphone; `live-capture-worklet.js` batches capture and `live-audio.mjs` schedules playback and acknowledges completed replies. Audio frames carry turn and phrase IDs so old chunks are discarded after interruption.

Character details, trigger definitions and ordinary delivery selections are saved in the local browser. Reference clips are not. The live app keeps audio and conversation in RAM for the active call, does not write recordings or transcripts, and sends inference requests to the configured local services. Review those services' logs and the host environment before using sensitive material. The single-caller localhost setup is not a public multi-user deployment.

## UI-only preview

With the development requirements installed, no GPU or model download is required:

```bash
python -m studio --live --preview --port 18097
```

Open http://127.0.0.1:18097/. Calls are disabled and no AI replies are simulated.
