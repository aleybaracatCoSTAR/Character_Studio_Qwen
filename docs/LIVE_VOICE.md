# Current voice behaviour and the next runtime test

The original application is a **record-and-send baseline**, not a live call. If the user types, Qwen generates speech from that text interaction. If the user records, the recording is supplied directly to Qwen as audio; this application does not transcribe it with a separate STT model first. Neither path currently streams playback.

## Confirmed delays in this code

`studio/engine.py` calls the synchronous Transformers `model.generate`. It returns the completed text/audio result. `studio/app.py` then sends a complete base64 WAV, and the browser starts playback after the whole response has arrived. Earlier audio inputs are retained and processed again on follow-up turns; there is no persistent model session or cross-request KV reuse.

These behaviours explain avoidable waiting, but no GPU timing breakdown has been collected. Do not attribute every delay to one cause or promise a speedup without measuring it. The user has now reported that this baseline is too slow for the intended performance experience.

## Intended interaction

Start a call, speak naturally, detect when the speaker finishes, play generated chunks as they arrive, and support interruption with honest conversation history. This requires changes to serving and audio transport, not just a microphone button.

Qwen's [own demo instructions](https://github.com/QwenLM/Qwen3-Omni#launch-local-web-ui-demo) warn that Transformers inference is significantly slower than its vLLM path. The [vLLM-Omni realtime web example](https://github.com/vllm-project/vllm-omni/blob/798fef99a33fba3ad3c2439d6ec10fe838c0076b/examples/online_serving/realtime_web/README.md) provides a Qwen profile with server VAD, streamed replies and interruption. Its documentation explicitly distinguishes committed Qwen turns from native streaming-input KV decoding.

**That runtime is not integrated into this studio yet.** Benchmark its official client with the existing checkpoint before promising PersonaPlex-style latency or native simultaneous speech. Check the deployment's actual capability response rather than inferring capabilities from the endpoint name. The official default Qwen stage placement uses two GPUs; a single-H200 arrangement needs separate configuration and validation.

## Drama 3's separate role

The [Drama 3 experiment](../experiments/drama3/README.md) evaluates expressive scripted voice and streaming arrival time. It does not fix the Qwen call transport or provide a conversational brain. It could later be the voice component of a larger dialogue system, but that would need a listening and reasoning system as well, with its own latency measurements.
