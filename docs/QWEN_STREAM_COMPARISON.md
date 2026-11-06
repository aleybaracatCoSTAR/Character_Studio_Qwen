# Compare native Qwen3-Omni with Qwen + Drama 3

The new **Streaming · Qwen3-Omni** option uses native microphone audio as model input and plays validated 24 kHz PCM chunks as they arrive. **Current · Qwen + Drama 3** stays available with its original generation, voices and whole-reply speech checks. Both paths use the same saved character, heard conversation, director, story/trigger settings and selected local archives.

This is a **turn-based speech-input, streaming speech-output experiment**. The existing recognizer supplies text for archive retrieval and the director. Omni also receives the original recorded microphone audio, replacing that same turn's text in its actor request. It is not a continuous full-duplex connection, and it does not turn the microphone into a text-only actor input. Typed messages and performer cues remain supported.

## Run on Surrey

Follow the [isolated runtime setup](../experiments/qwen_stream/README.md). The first-time commands, from the repository on Surrey, are:

```bash
cd "/scratch4weeks/$USER/Character_Studio_Qwen"
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_ed25519_github -o IdentitiesOnly=yes' git pull --ff-only
bash experiments/qwen_stream/setup.sh
.venv-qwen-stream/bin/python experiments/qwen_stream/download.py
```

The setup changes only `.venv-qwen-stream`. It pins the serving source, Transformers and CUDA 13.0 wheels. The explicit download command reuses the original Qwen3-Omni checkpoint and the existing director/STT checkpoints when their markers/files match. If missing, the Omni model is about 71 GB. The working baseline environments and model files remain in place.

Stop the old model stack and wait for its workers to exit. Inside an existing three-H200 allocation:

```bash
bash experiments/qwen_stream/run_stack.sh --listening baseline
```

Or submit the new batch job from the repository after the old job has stopped:

```bash
sbatch experiments/qwen_stream/run.slurm.sbatch --listening baseline
```

Use **`--listening nemotron-smart-turn` on both stacks** instead if that is your current listening mode. It reuses the prepared listening environment/checkpoints. Do not change the listening model while measuring the voice-engine difference. The native stack uses the first two allocated GPUs for Thinker/Talker/Code2Wav and the third for the separate director, with the same optional listening placement as the baseline.

The portal remains on **port 18091**, so its browser character library remains available. Keep the same page path too: use **/archive/** for Robin's archive portal if that is where your character is saved; the main and archive pages keep separate browser libraries. Update the compute hostname in the existing SSH tunnel if Slurm assigns another node. No separate browser tunnel to the model's internal port 18097 is needed. Port checks refuse earlier servers and never stop unrelated processes. Use the foreground or batch method, not both.

## Make a useful comparison

1. Update the code and refresh the browser once before collecting results. Select the character and its archives, director, voice style and story settings.
2. Run the baseline, ask a short question, and let the reply finish playing. **Compare replies** retains that sample in page memory.
3. End the call and stop the baseline stack. Keep the browser page open while launching the native stack on the same port. Choose **Streaming · Qwen3-Omni** after it becomes ready, and start a fresh call.
4. Use the same input type and question. For a first test, typed input avoids transcription differences. Then compare microphone input separately.
5. Open **Compare replies** to see both transcripts, archive citation IDs, first-text/audio estimates and separate recognition, retrieval and director times. A matched pair requires identical authored settings, prompt, prior context, input type and finished browser playback. It is not a quality score.
6. Download the JSON before reloading the page. Results stay in browser memory until explicitly exported; the download uses the studio's existing bounded attachment service with a two-minute expiry. No audio or server connection credentials are included.

For consistency, try a fixed identity question, a mistaken assumption the character should correct, an earlier detail recalled several turns later, a manual story-beat change, and an archive-specific request such as **Show me an image of Agnolo Gaddi’s work**. Use the same selected collections and inspect the supplied citation IDs; imports growing between calls can change retrieval. Inspect the model inputs to check the actual character and director/archive context. Native microphone WAV is intentionally omitted from the inspector and replaced with its byte count/format.

The first experiment keeps the **same-turn director** on both engines. Archive retrieval, image-link resolution and director time therefore remain in the response path. This isolates the effect of combined native actor/speech streaming; it does not claim the separate background-director design has already been implemented.

## Voices, timing and completion

Native voices map **Automatic** to the character's Qwen speaker, **Female** to Chelsie and **Male** to Ethan. Uploaded Drama 3 voice references are rejected in native mode. Emotion/delivery is a whole-request prompt instruction, not a guarantee the model follows it. Voices differ between engines; judge consistency separately from timbre.

The first-audio figure is a server estimate of when PCM becomes available, including the endpoint pause, recognition, retrieval and director time. Browser queue timing is reported separately. Neither is a microphone-to-speaker hardware measurement. Full generation time, response length and source references help interpret a faster first chunk.

Native output plays before a whole-reply ASR/coverage check could complete. The adapter validates the pinned SSE protocol, 24 kHz mono PCM16 metadata, sample alignment, limits, text/audio presence and stop/DONE completion. This confirms transport completion, not that every displayed word was spoken. A broken stream is never retried after audio has played. Incomplete, unaligned partial replies are not saved as a fully heard conversation turn. Complete replies enter heard memory only after normal browser playback acknowledgements. The existing Stop reply and interruption controls still cancel the native stream.

## Return to the original setup

After stopping the native stack, use the original launch with the same listening mode:

```bash
bash scripts/run_live_stack.sh --profile quality-3gpu --listening baseline
```

Or submit `sbatch scripts/run_live_slurm.sbatch --listening baseline`. Keep the page open and choose **Current · Qwen + Drama 3** when ready. The two stacks run sequentially on three GPUs; the selector does not load or move models between GPUs automatically.

## Verification boundary

CPU tests exercise source-shaped SSE streams, native WAV input, shared character/director/archive context, immediate PCM forwarding, cancellation, partial failures, heard-memory gates, sample-rate playback and comparison state. Runtime tests cover pinned identity, cached files, GPU placement, port refusal, process ownership and batch behavior. Local browser checks use a banner-labelled scripted fixture with no AI models. They are not a GPU latency or character-quality result. The current environment could not resolve the Surrey login hostname, so the actual H200 comparison remains to be run there.

Runtime references: [Qwen3-Omni](https://github.com/QwenLM/Qwen3-Omni), [pinned vLLM-Omni deployment](https://github.com/vllm-project/vllm-omni/blob/798fef99a33fba3ad3c2439d6ec10fe838c0076b/vllm_omni/deploy/qwen3_omni_moe.yaml).
