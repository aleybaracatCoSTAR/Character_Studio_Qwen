# Developer handoff

## Objective

Compare Qwen3-Omni-30B-A3B-Instruct with CharacterPlex for character identity, conversational sense and narrative direction. Keep the existing CharacterPlex deployment untouched. This is a self-contained starter, not a migration of the old Character Studio.

## Implemented

Character editing, browser-local character saving, JSON download, prompt inspection, recorded audio input, text chat, generated audio playback, bounded conversation history, single-generation locking, input validation and explicit preview mode. Anna is the only bundled character and contains no real personal data.

## Validation and remaining work

Update, 22 October 2026: the user has launched the baseline on Surrey and reports that it is too slow and does not provide the intended live speech experience. We have not observed a completed inference trace or measured that session. See `LIVE_VOICE.md`; the streaming runtime change remains outstanding. A separate Drama 3 TTS evaluation client has five additional CPU tests (28 tests total), but its GPU inference has not been run here.

23 CPU tests pass, covering WAV validation/resampling, prompt construction, history ordering, failure rollback, session isolation, concurrent-request rejection and HTTP responses. JavaScript and shell syntax checks also pass. Preview mode has been inspected in a browser, including editing a name, inspecting the updated instructions and reloading to verify local saving. These checks do not validate inference, microphone hardware or model output quality.

**GPU inference has not been independently validated by the agent.** Use the interactive H200 recipe in `SURREY.md` for the baseline. Measure peak VRAM, first response time, text-only output, recorded voice input and spoken output. Try all three speakers. Capture the model's contradictions verbatim before modifying the prompt. A passing application check is not evidence of better narrative intelligence.

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
3. Only then consider streaming, performer controls, a director-note API, or integration with the existing Character Studio.
