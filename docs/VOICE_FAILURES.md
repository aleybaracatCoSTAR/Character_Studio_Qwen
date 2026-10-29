# Diagnosing an unfinished Drama 3 voice take

**29 October sampling update:** the reported `600/600 tokens, 3/21 words matched` failure is consistent with a main-token top-k limitation explicitly documented in the SGLang-Omni cookbook. The live launcher now separates uncapped main-token sampling from top-30 acoustic heads in the existing pinned vLLM runtime. See the [source evidence, guarded correction and restart procedure](DRAMA3_SAMPLING.md). This targets generation itself; it does not weaken the completion checks or establish a successful real-model result.

`VoiceTakeIncomplete` means the app could not validate a complete spoken response. It does not identify the cause by itself, and it does not mean speech recognition or the character's text generation failed. The app buffers a whole reply while validating it, so a rejected take is not played as cut-off audio.

## Find the reason

After updating and restarting, the portal message includes a fixed reason such as `[token_limit]` or `[upstream_error]`. The app terminal also writes a `Voice take failed:` record for each attempt, with the safe counters available: requested/output tokens, received audio seconds/chunks, completion status, elapsed time and allowlisted service status. The final call error includes the same structured `voice_diagnostic`. Submitted text, reference transcripts, audio, URLs and arbitrary upstream error messages are excluded from these records.

| Reason | What it means |
|---|---|
| `token_limit` | Generation reached its budget or reported a length stop, and the stricter capped-take recovery checks did not establish a complete reply with a quiet ending. |
| `audio_limit` | Audio exceeded the bounded amount allowed for this request. |
| `upstream_error` | Drama 3 explicitly sent an error; the TTS log is needed for its underlying engine cause. |
| `missing_done` / `unfinished_event` | The stream ended without a complete final event. |
| `empty_audio` / `malformed_audio` | No usable PCM take was returned. |
| `missing_usage` | The server did not provide the token-usage confirmation expected by this client. |
| `transport_timeout` / `transport_network` | The voice request timed out or its connection failed. |
| `http_status` | The voice service rejected the request; inspect its TTS log and status code. |
| `pcm_format` / `content_type` | The service did not return the required PCM/completion-event protocol. |
| `speech_mismatch` | Recognition of the completed take did not cover enough of the requested words; a substantial omission or early stop was detected. |
| `verification_unavailable` | The audio was generated but its recognition check failed or timed out; unchecked audio was withheld. |

In the allocated Surrey GPU terminal, capture the service log immediately after a failed reply:

```bash
tail -n 80 /scratch4weeks/$USER/live-voice-store/logs/tts.log
```

Keep the matching `Voice take failed:` line from the app terminal as well. These two records distinguish a local validation rejection from a voice-server failure. An earlier log containing only `Live turn failed (VoiceTakeIncomplete)` cannot establish which cause occurred.

An HTTP 200 response for a streaming request only confirms that the request was accepted. A later error or incomplete spoken take can still follow. A service log saying `cancelled by client` establishes cancellation, not a GPU-memory failure: an explicit Stop reply, a new turn, disconnect or a local rejection can close the request. Match the request time to the app's reason code before assigning a cause.

The [official vLLM Drama 3 recipe](https://recipes.vllm.ai/fishaudio/drama-3) also describes a model limitation where leading event/delivery tags or fillers can produce only an initial word/event followed by silence. This is a plausible contributor to early-word failures, not proof of an individual request's cause. The app does not silently remove the selected emotion or move it between sentences as a recovery strategy.

## Reaching the limit does not reveal what was spoken

A report such as `488/488 tokens, 22.7 s generated audio` means the codec used its full frame allowance. Those are audio-generation frames, not a count of spoken words. The waveform may contain ongoing speech, all requested words followed by silence, or only an initial fragment followed by silence. Increasing every request's budget would make some stalled takes longer without restoring missing words.

The client now consumes and validates the entire event stream before considering recovery: one terminal completion event, valid usage and audio format, aligned bounded PCM, no later error or extra event, and clean end-of-stream. Malformed or aborted streams are never repaired by a silence check. Raw candidate audio remains local to the current attempt and is not added to errors, logs, the inspector or conversation memory.

For a valid but limited take, the client can accept a **strictly verified full reply with a quiet ending**. Its existing recognizer must recover exactly the requested normalized lexical word sequence, with no missing, extra or repeated normalized words. There must also be at least 800 ms of consecutive very quiet audio after earlier varying speech energy. Only the quiet suffix is removed, retaining 400 ms after the last nonquiet block. Recognition runs on the audio that would actually be released, so a quiet final word lost by trimming cannot qualify through recognition of the untrimmed waveform. If verification is disabled or unavailable, this recovery is not allowed.

If speech is still active at the cap and recognition establishes an ordered prefix of the requested words without additions or repetitions, the single automatic retry can use a larger frame allowance, bounded by 1,600 tokens. Its text, emotion, cue and reference stay fixed. A fragment followed by a long silent tail does not receive an inflated budget, because more silence is not a useful recovery. There are still at most two attempts in total, shared with other voice failures.

Tail analysis uses conservative fixed energy thresholds rather than relative loudness: very quiet speech, whispered endings, names and ASR errors can prevent recovery. Exact normalized matching ignores punctuation, case, configured fillers and common contraction/number spelling differences; it is not proof of acoustic or semantic perfection. Rejected takes remain blocked. No new real-model recovery success rate has been measured.

## What this update fixes

The old fixed 4 MiB PCM bound was about 47.6 seconds at 44.1 kHz mono PCM16, while the existing 1,600-token request ceiling could allow a longer take. The byte bound now scales with the request's codec-frame budget, using the pinned Fish model configuration's approximately 21.5 frames/second and a small margin. It retains a 4 MiB floor and an 8 MiB hard bound. A complete 49-second synthetic transport fixture is accepted. Limited takes require the stricter recovery above; malformed or unconfirmed streams remain blocked. This does not establish that a real model says every requested word correctly.

The whole reply keeps one emotion and one voice reference. An incomplete take or transient service failure still receives at most one automatic retry with the same words and delivery. Only a supported active-speech case can increase its generation allowance. **Retry voice** regenerates the saved reply without asking the actor or director for different words. The app does not strip emotional direction to conceal a failed take.

The live app now also uses its existing English speech recognizer to compare a completed take with the requested spoken text before releasing it. This catches substantial omissions such as a multi-sentence reply rendered as only its opening sentence, even if the protocol reports completion. A rejected take shares the same two-attempt limit; its text, emotion and reference stay fixed. Recognition runs locally, and its temporary transcript is not logged or stored. This is a conservative word-coverage check, not a proof of pronunciation or semantic accuracy: accents, names, multilingual speech and recognition errors can affect it, and minor skipped words can still pass. It adds a recognition step to voice preparation and does not repair an unreliable TTS model.

Voice checking is enabled by default in the live app. `LIVE_VOICE_VERIFY_TIMEOUT` bounds its recognition wait (default 20 seconds); `LIVE_VERIFY_VOICE=0` disables that content check for a deliberate diagnostic comparison, leaving protocol checks active. This setting does not change the selected voice or emotional delivery. Direct low-level Drama 3 scripts still need their own listening check; the live app's validator is not a claim about every speech endpoint.

Automatic voice anchoring accepts only a whole take lasting 1–30 seconds, matching the pinned upstream reference-duration rule. Shorter or longer audio is not cropped with a now-incorrect transcript and is not promoted as the next request's reference. The app waits for a suitable later take; built-in Female/Male and validated uploaded references retain their selected conditioning.

No live GPU synthesis was performed for these updates. The later sampling diagnosis identifies a concrete runtime setting consistent with the reported first-phrase stall, but it still needs a real-model listening check. The corrected duration bound is not evidence that every failure came from that bound.
