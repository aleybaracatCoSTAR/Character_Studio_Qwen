# Voice modes and latency

There are now two separate application modes. See **[Live setup](LIVE_SETUP.md)** for the continuous-microphone experiment.

| Mode | Input and output | Implementation |
|---|---|---|
| Original Qwen-Omni baseline | Record, send, wait for a complete spoken reply | `studio/engine.py`, Transformers 5.2 |
| Live Drama 3 pipeline | Automatic pause detection, streamed text and phrase audio, interruption | `live_voice/`, vLLM services |

## Original baseline

The original application passes recorded audio directly to Qwen3-Omni; it does not use separate STT. It calls synchronous `model.generate`, sends a complete base64 WAV, and starts playback only after the response arrives. Earlier audio inputs are reprocessed on follow-up turns. The user reports that this mode is too slow for the intended performance experience. We have not measured its full GPU timing breakdown.

Qwen's [own demo instructions](https://github.com/QwenLM/Qwen3-Omni#launch-local-web-ui-demo) describe its vLLM path as faster than Transformers. The [official Omni realtime example](https://github.com/vllm-project/vllm-omni/blob/798fef99a33fba3ad3c2439d6ec10fe838c0076b/examples/online_serving/realtime_web/README.md) is another runtime comparison, but it is not the runtime implemented in this studio. Do not infer native streaming-input decoding from a realtime endpoint name.

## New live pipeline

The small English Faster-Whisper model transcribes a detected utterance after a 480 ms pause. The [director](DIRECTOR.md) first plans an acting intention and vocal delivery (a separate request to the same Qwen server by default, with another endpoint configurable). Qwen3-4B-Instruct-2507 then generates a short character response as a token stream. Complete clauses go to Drama 3 while the remaining text is still being generated, and arriving PCM is played directly. This removes the application's full-answer/full-WAV wait. It is a cascaded, interruptible voice system; it is not a native full-duplex speech-to-speech model.

The microphone waits during generation and playback; **Stop reply** cancels unfinished text and TTS requests, clears scheduled browser audio, discards stale turn packets and retains only fully played phrases in history. A partially heard phrase is conservatively omitted. This avoids the first speaker test's repeated cancellations from VAD. Female/Male choices use bundled VCTK reference clips; Automatic uses the first synthetic phrase as a reference, and Custom accepts a WAV and transcript. Voice emotion, non-verbal expression and a persistent emotional trigger can be configured in the sidebar; see [setup](LIVE_SETUP.md). Voice stability, expressive quality, recognition and actual latency still need listening tests.

The app reports server-side timing stages, not a promise of a particular response time. **CPU tests verify transport and cancellation, not model inference or H200 memory use.** The combined one-H200 memory configuration is experimental. First-run kernel compilation must be separated from warmed turn measurements.

### Playback continuity

The browser builds a 120 ms PCM reserve before playback or recovery from an underrun, then coalesces small packets into at least 60 ms batches. Every short final tail is flushed when its phrase completes. The original 44.1 kHz sample rate is preserved; buffering does not accelerate, time-stretch or discard speech. Phrase acknowledgements and microphone release wait for buffered samples and scheduled audio to finish. This absorbs small variations in packet arrival, but cannot remove a long model-generation pause or reconstruct words that TTS did not generate.

The previous TTS budget was 13 tokens per input word, capped at 400. That hard generation limit could cut off a slower delivery. The live provider now allows a bounded 240–1600 tokens based on spoken words and characters, excluding performance tags. This changes maximum generation length, not speaking speed. Drama 3 may finish before the limit; a take that continues emitting silence may instead last longer. Each `phrase_done` message includes generated audio seconds, chunk count and generation time to help separate synthesis from playback delays. These changes have regression coverage; a Surrey listening comparison is still required.

Drama 3 remains a [TTS model](https://huggingface.co/fishaudio/drama-3), with its own model license. Its standalone [three-line voice experiment](../experiments/drama3/README.md) remains useful for comparing delivery without involving recognition or character reasoning.
