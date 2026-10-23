# Latest change: director orchestration, 23 October 2026

See [DIRECTOR.md](DIRECTOR.md) for the current architecture, separate-model configuration, UI controls and limits. The director now combines conversational intention and optional trigger assessment in one validated JSON call, then directs both the actor and Drama 3. Default role separation reuses the existing Qwen checkpoint. Male/female choices use real bundled VCTK references with separate attribution. Fresh calls clear browser reply maps and transcripts; stale sockets cannot append to new calls. The new director path has CPU regression coverage; it has not yet been tested against the live GPU model. Older notes below describe the evolution and baseline modes.

# Developer handoff

## Objective

Compare Qwen3-Omni-30B-A3B-Instruct with CharacterPlex for character identity, conversational sense and narrative direction. Keep the existing CharacterPlex deployment untouched. This is a self-contained starter, not a migration of the old Character Studio.

## Implemented

Character editing, browser-local character saving, JSON download, prompt inspection, recorded audio input, text chat, generated audio playback, bounded conversation history, single-generation locking, input validation and explicit preview mode. Anna is the only bundled character and contains no real personal data.

## Validation and remaining work

Update, 23 October 2026 (context and performance): the user tested the initial controls and reports exact-cue-only state changes and mostly flat audio. Literal matching has been removed. `LocalProviders.assess_trigger` now makes a separate, bounded local Qwen JSON request using recent dialogue, character facts and the trigger. `TriggerDecision` validates activate/release/hold plus concrete evidence. Failure is visible and leaves state unchanged; no keyword fallback is substituted. The backstage controller handles interpretation; dialogue generation receives the resulting runtime state without changing the character definition. Evidence distinguishes a threat from a completed action. Reassurance can release an active state. The legacy `spoken_cue` JSON key remains as example wording for saved-profile compatibility.

Delivery now includes a free performance direction, descriptive emotion defaults and sentence-level chunks for expressive replies (200-character target, comma splits disabled). This is stronger conditioning, not evidence of a better acted performance; the existing reference may carry neutral prosody. `scripts/evaluate_emotion.py` runs 12 real-model context cases and optional same-reference natural/short-tag/full-direction TTS comparisons without restarting models. The previous trigger-only controller was subsequently tested on the real Qwen server: 11/12 small fictional cases matched. Negated action while inactive returned release rather than hold; runtime guards release to active states. All three voice comparison takes were generated, but emotional quality has not been established by listening. Do not report contextual accuracy, improved acoustic emotion or latency without the results/listening test.

Update, 23 October 2026 (shutdown): the first restart after the controls update found port 18092 still occupied. The previous cleanup signalled model parents without waiting for their workers. The launcher now starts each model server in an isolated POSIX process group through `scripts/live_process.py`, signals only its tracked groups and allows 20 seconds of graceful shutdown before stopping remaining workers. It waits for its parent processes to exit before returning. Repeated Ctrl+C does not abort this bounded cleanup. Existing port occupants are still never automatically killed. Three real CPU-process/socket tests cover child port release, an unresponsive worker and protecting the calling shell/unrelated processes; the updated lifecycle still needs a Surrey GPU stop/restart check.

Update, 23 October 2026 (live turn-taking and emotional controls): the user started the combined stack; the real forwarded `/api/status` reported STT, LLM and TTS ready. Their speaker-based call repeatedly interrupted every response. VAD used to cancel on roughly 60 ms of detected speech, before ASR could establish intent, and finished turns were never marked complete. The updated app discards microphone input while generating and playing, resumes only after ordered phrase acknowledgements plus `playback_done` and a 350 ms echo tail, and uses **Stop reply** for deliberate interruption. Completed replies are no longer relabelled as interrupted on the next turn.

**How they speak** now offers live emotion and expression selections plus an optional bounded WAV/transcript reference. Emotion and events become inline Drama 3 TTS tags, separate from spoken text/history; controls snapshot at the start of an answer. No named Drama 3 voice-preset catalogue is invented. The start WebSocket limit is 3 MiB for a validated reference of at most 2 MiB; microphone packets and control messages retain their smaller limits. Reference clips are never persisted by the app. A short word-only phrase is left untagged, and tags are inserted after opening words to avoid the documented leading-tag stall.

**Emotional trigger** is one editable reaction per call. Anna's example is reset → anxious → fear of losing memories and asking the visitor to stop. The initial version used an action button or exact spoken cue; the current version uses the contextual assessment described above, alongside the action button. Resolution keeps the event in history. No physical button hardware or automatic multi-trigger graph is implemented. The original character definition remains intact.

These controls and the cancellation regression have CPU tests and the UI has been inspected, including persisted emotion settings. The updated code has not yet been deployed to the GPU job or evaluated for expressive audio quality. See [live setup](LIVE_SETUP.md) for the workflow.

Update, 23 October 2026: a real standalone Drama 3 request through the Mac SSH tunnel succeeded for "Hello, I am Anna." with no client warmup and a 120-token cap. First PCM arrived at 3.084 s, the complete response at 3.304 s, producing 2.786 s of 44.1 kHz mono audio. This validates one TTS request and transport, not character quality or the full live pipeline. The previous HTTP 400 came from sending `sample_rate`; the endpoint rejects that field even at its native rate, so both clients now omit it. The test client also reports the server error body. Generated media remains outside Git.

Update, 22 October 2026: the user launched the baseline on Surrey and reports that it is too slow for the intended live experience. A separate **live STT → small Qwen text model → Drama 3 pipeline is now implemented**; see [setup](LIVE_SETUP.md) and [mode comparison](LIVE_VOICE.md). It has not been run against the actual three model services on H200. The user encountered mismatched vLLM/Omni and Torch/Audio CUDA installations during the standalone Drama 3 test. The installer now pins the serving pair and all three CUDA 13.0 Torch-family wheels and checks CLI imports before model load.

67 Python tests pass: 23 baseline checks, six standalone Drama 3 client checks, 35 live checks and three process-lifecycle checks. Eight Node tests cover chunked resampling, packet encoding, scheduled playback, interruption and honest phrase acknowledgements. JavaScript and shell syntax checks pass. Baseline and live preview pages have been inspected in a browser, including character-name edits. These checks do not validate inference, microphone hardware or model output quality.

## Live runtime handoff

- `live_voice/app.py`: one same-origin WebSocket caller, input limits, readiness and cleanup.
- `live_voice/turns.py`: 20 ms VAD frames, 480 ms silence endpoint, 15-second utterance limit, early text clauses, six recent pairs with a character-budget bound.
- `live_voice/providers.py`: Faster-Whisper on CPU, vLLM text SSE and Drama 3 raw PCM streaming. Reference voice is either an explicitly selected WAV/transcript or synthetic audio generated in the current call.
- `live_voice/session.py`: bounded two-phrase queue, cancellation, timing and playback-confirmed history. The browser discards stale turn IDs. Partial phrases are omitted after interruption.
- `frontend/live.js`, `live-audio.mjs`, `live-capture-worklet.js`: continuous capture, protected microphone turns, resampling, Web Audio scheduling, playback completion, mute, manual interruption, voice controls and typed tests.
- `live_voice/voice.py`, `reactions.py`: reference validation, Drama 3 performance cues, validated contextual decisions and persistent emotional-state instructions.
- `scripts/run_live_stack.sh`: starts only its own TTS/text subprocesses, waits for health, runs the UI and cleans up on exit. Does not release the Slurm job.
- `scripts/run_live_stack.sh --prepare` installs the separate live app environment and resumes the pinned STT/text downloads, then launches. Existing Drama 3 dependencies and weights are reused. Launchers default to native GPU sampling (`VLLM_USE_FLASHINFER_SAMPLER=0`) to carry forward the first Surrey sampler workaround. Explicit overrides are preserved.
- No incremental word transcript, model-level simultaneous speech, performer cue API or mid-call character mutation is implemented.

Pinned text model: `Qwen/Qwen3-4B-Instruct-2507` at `cdbee75f17c01a7cc42f958dc650907174af0554`. STT: `Systran/faster-whisper-base.en` at `3d3d5dee26484f91867d81cb899cfcf72b96be6c`. See `scripts/download_live_models.py`. Drama 3 pins remain in its experiment scripts. GPU serving uses vLLM 0.32.0 + Omni `798fef99a33fba3ad3c2439d6ec10fe838c0076b`, Torch 2.13.0+cu130, Audio 2.11.0+cu130 and Vision 0.28.0+cu130. Do not replace the explicit backend with `auto`.

The next required integration check is to deploy the new code to the running H200 allocation, then test warmed typed and microphone turns, speaker echo protection, Stop reply, reference voice, emotional delivery, reset activation and clearing. Capture resolved Linux dependencies and measured VRAM after success. The experimental GPU budgets total 75%; they are not a proven memory result. Character instructions guide output but do not guarantee fact consistency. Shorter streaming waits do not establish stronger narrative intelligence.

**The original Qwen-Omni baseline and the complete live stack have not been independently validated on GPU by the agent.** The standalone Drama 3 request above is the limited verified exception. Use the interactive H200 recipe in `SURREY.md` for the baseline. Measure peak VRAM, first response time, text-only output, recorded voice input and spoken output. Try all three speakers. Capture the model's contradictions verbatim before modifying the prompt. A passing application check is not evidence of better narrative intelligence.

## Pinned starting point

- Checkpoint: `Qwen/Qwen3-Omni-30B-A3B-Instruct`.
- Revision: `26291f793822fb6be9555850f06dfe95f2d7e695` (public metadata inspected 22 October 2026).
- Transformers 5.2.0, PyTorch 2.10.0, torchvision 0.25.0, Accelerate 1.12.0, Python 3.12 for the GPU recipe.
- BF16, one visible CUDA GPU, SDPA. No quantisation or CPU offload fallback.
- Port 18091 by default, separate from the previous CharacterPlex 18090 example.

`Qwen3OmniMoeForConditionalGeneration.generate` has different text-only and text+audio returns. The adapter handles the 5.2.0 tensor/audio tuple and text-only `.sequences` result, and removes prompt tokens before decoding. Review this if updating Transformers. Do not copy a 4.57 example into the pinned 5.2 adapter without checking the API.

## Useful API calls

`GET /api/status` reports `preview`, `not_loaded`, `loading`, `ready` or `error`. `GET /api/example` returns Anna. `POST /api/prompt` accepts the character JSON and returns instructions. `POST /api/chat` accepts a multipart form with `payload` JSON and optional `audio` WAV. See `ChatRequest` in `studio/app.py` for the contract. A game engine could call this API for turn-based tests; it is not a low-latency streaming transport.

## Known design choices

Character edits reset the conversation, including the current story moment. Keeping a live conversation while changing director notes needs a separate, validated runtime state design. Recent audio is reprocessed on follow-ups, which can increase latency. There is no cancellation of in-flight inference from the browser. Closing the page or SSH tunnel does not cancel the Slurm allocation.

The server deliberately returns a short error rather than recording prompt/audio content in logs. For inference debugging, add local diagnostics that avoid personal data. Dependency versions are starting pins, not a complete transitive lockfile; record a resolved lock after the first successful Linux GPU install.

## Next changes after the first GPU run

1. Fix actual adapter or environment failures, then preserve a successful Linux dependency lock and measured resource requirements.
2. Evaluate a small consistent set of character-fact and false-assumption questions against both models.
3. Validate the new streaming mode before adding performer controls, a director-note API, or integration with the existing Character Studio.
