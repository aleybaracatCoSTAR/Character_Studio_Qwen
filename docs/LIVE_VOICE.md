# Voice modes and latency

There are now two separate application modes. See **[Live setup](LIVE_SETUP.md)** for the continuous-microphone experiment.

| Mode | Input and output | Implementation |
|---|---|---|
| Original Qwen-Omni baseline | Record, send, wait for a complete spoken reply | `studio/engine.py`, Transformers 5.2 |
| Live Drama 3 pipeline | Automatic pause detection, streamed text, one continuous voice take per reply, interruption | `live_voice/`, vLLM services |

## Original baseline

The original application passes recorded audio directly to Qwen3-Omni; it does not use separate STT. It calls synchronous `model.generate`, sends a complete base64 WAV, and starts playback only after the response arrives. Earlier audio inputs are reprocessed on follow-up turns. The user reports that this mode is too slow for the intended performance experience. We have not measured its full GPU timing breakdown.

Qwen's [own demo instructions](https://github.com/QwenLM/Qwen3-Omni#launch-local-web-ui-demo) describe its vLLM path as faster than Transformers. The [official Omni realtime example](https://github.com/vllm-project/vllm-omni/blob/798fef99a33fba3ad3c2439d6ec10fe838c0076b/examples/online_serving/realtime_web/README.md) is another runtime comparison, but it is not the runtime implemented in this studio. Do not infer native streaming-input decoding from a realtime endpoint name.

## New live pipeline

The small English Faster-Whisper model finalises a detected utterance after a **400 ms pause**. It can prepare recognition at **200 ms of silence**, while waiting for that final endpoint. Both snapshots retain the same 80 ms audio tail; the prepared result is used only if the final PCM matches byte for byte. Resumed speech changes the audio and forces a separate final recognition. Provisional text never enters the conversation, director or actor. Browser capture now sends **40 ms batches**, previously 80 ms, and the server continues examining 20 ms frames. These changes overlap some recognition with the pause; they have not been measured for H200 end-to-end speed.

The [director](DIRECTOR.md) then plans an acting intention and vocal delivery (a separate request to the same Qwen server by default, with another endpoint configurable). The selected Qwen actor generates a short character response as a token stream. The app waits for the actor's complete short reply, gives it to Drama 3 in one request with one delivery cue, and validates the whole take before playback. Text still streams as it is generated. This complete-reply policy is unchanged: it adds start latency in exchange for consistent delivery across sentences. The compact profile uses Qwen3-4B; the larger profiles are documented in [Multi-GPU setup](MULTI_GPU.md). This is a cascaded, interruptible voice system, not a native full-duplex speech-to-speech model.

The microphone waits during generation and playback; **Stop reply** cancels unfinished text and TTS requests, clears scheduled browser audio, discards stale turn packets and retains only fully played replies in history. An interrupted reply is conservatively omitted from assistant memory. This avoids the first speaker test's repeated cancellations from VAD. Female/Male choices use bundled VCTK reference clips; Automatic uses the first synthetic reply as a reference, and Custom accepts a WAV and transcript. Voice emotion, non-verbal expression and a persistent emotional trigger can be configured in the sidebar; see [setup](LIVE_SETUP.md). Voice stability, expressive quality, recognition and actual latency still need listening tests.

The page reports the final endpoint pause, recognition's **remaining wait** after that endpoint, director time, complete actor-text time and complete validated TTS time. A prepared-recognition flag means exact final audio reused a job started during the pause; recognition wait is not its full runtime. TTS timing includes validation before audio release and a retry when needed, rather than raw first streamed PCM. **Server estimate to first audio** excludes SSH/network transit and browser buffering; it does not measure sound heard by the participant.

**CPU tests verify preparation, context, transport and cancellation, not model inference or H200 memory use.** No live-model check was performed for this responsiveness update. The combined one-H200 memory configuration is experimental. First-run kernel compilation must be separated from warmed turn measurements.

### Active-call memory

The session keeps the latest **12 user/assistant pairs** plus a RAM archive bounded to **100 turns or 100,000 characters**. A lexical search can retrieve up to **three older original quote pairs** related to the next utterance. Conversation content is bounded to **10,000 characters for the actor**, **3,000 for a story-aware director** and **6,000 for an ordinary director**, separately from character and instruction text. Older quotes remain in their original order, without an additional summary model. Lexical matching can miss context; this is not a guarantee of perfect recall or reasoning.

Participant utterances can be recalled after final recognition. Assistant words enter memory only after playback is acknowledged, so generated but unheard replies are excluded. Private stage cues direct the current turn but stay out of later participant recall; their resulting AI speech can be remembered after playback. Conversation memory is limited to the active call, stays in RAM and is not retained across sessions. Character-profile settings saved in the browser are separate from this conversation memory.

### Playback continuity

The browser builds a 120 ms PCM reserve before playback or recovery from an underrun, then coalesces small packets into at least 60 ms batches. Every short final tail is flushed when its phrase completes. The original 44.1 kHz sample rate is preserved; buffering does not accelerate, time-stretch or discard speech. Phrase acknowledgements and microphone release wait for buffered samples and scheduled audio to finish. This absorbs small variations in packet arrival, but cannot remove a long model-generation pause or reconstruct words that TTS did not generate.

The TTS budget is a bounded 240–1600 tokens based on spoken words and characters, excluding performance and speaker controls. This is a maximum generation length, not a speed control. A take that reaches the maximum is discarded before playback; a reply receives one retry with the identical emotion cue, text and voice reference, with a visible notice. Delivery is never changed to natural speech to make a retry succeed. Each `phrase_done` message includes generated audio seconds, chunk count and generation time. The October 26 real-model comparison reproduced missing speech with the old stacked mid-sentence cues; [Reply completion and emotion lock](VOICE_COMPLETION.md) records the replacement cue format, measurements and limits. A completion event does not prove that every requested word was spoken.

Drama 3 remains a [TTS model](https://huggingface.co/fishaudio/drama-3), with its own model license. Its standalone [three-line voice experiment](../experiments/drama3/README.md) remains useful for comparing delivery without involving recognition or character reasoning.
