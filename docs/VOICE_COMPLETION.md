# Whole-reply completion and consistent emotion

Updated 26 October 2026. The current policy is one continuous voice take per complete reply, with one fixed emotion and reference. The earlier sentence-level natural-delivery retry was reported to create unwanted mood changes and has been removed.

## What failed

The app inserted two separate cues after the first two spoken words: an emotion label and a longer direction. On the reported line, that produced `It’s hard, [sad] [subdued, fragile voice with hesitant pauses] you know?...`. In direct tests, the same pattern generated only “I called” or “I love” for longer sentences and continued to the generation-token limit. Increasing that limit alone cannot restore the missing words.

The old expressive chunker also merged short sentences into longer synthesis requests. Streaming ASCII ellipses could fragment into a stray `..` at the start of another request. Neither issue should be treated as a change in the character's intelligence.

## Current behaviour

- The director's selected delivery is snapshotted before actor generation. Operator changes apply to the next reply.
- Actor text still streams to the transcript, but the app waits for the final text before calling TTS. The whole reply goes through one synthesis request with one speaker-prefixed descriptive cue. It is not split into independent sentence takes, which can vary their performance even with the same cue.
- The app collects the full take's SSE audio and terminal token-usage event before sending PCM. It validates 44.1 kHz mono PCM, alignment, size, complete events and an output-token count below the requested maximum.
- An incomplete/capped take is discarded and retried once with the **identical full text, cue, emotion and voice reference**. This applies to plain replies too. No retry drops emotional instructions, changes voice, or substitutes natural delivery. If both attempts fail, the interface reports failure without playing either incomplete take.
- Existing `phrase_start`, `phrase_done` and binary sequence IDs remain compatible, but sequence `0` now denotes the entire reply. Playback acknowledgement and heard-memory commitment wait until all its audio finishes. An interrupted reply is conservatively omitted as a whole; the system does not infer which words were heard.

**Latency tradeoff:** audio waits for complete actor text and a validated full TTS take. This adds startup delay compared with sentence streaming, while avoiding audible sentence-to-sentence take resets or fallback mood changes. Once ready, PCM is transported in bounded chunks and the browser preserves its native playback rate. Model prosody can still vary within one take; this change removes application-level emotion switches rather than guaranteeing a particular acted performance.

## Earlier diagnostic evidence and current limits

The real H200 comparison used five cue formats on three lines, followed by twelve speaker-prefixed descriptive takes covering all four reported sentences with male and female references. All twelve ended below their token budgets. Local Whisper recovered the full clauses in all twelve, with ten exact normalized word matches and two `called`/`call` transcription differences; `mum`/`mom` was normalized. Eight additional emotion/expression takes retained the full diagnostic line. These small tests do not establish general word accuracy or subjective emotional quality.

The preceding sentence-level controller was exercised against the running Drama 3 service and emitted four completed sentences with final playback acknowledgement accepted; local ASR recovered their full wording apart from mum/mom spelling. The second reported six-short-sentence reply also retained all six sentences with a male reference; automatic voice had a Called/Hold recognition difference. These are evidence for the earlier cue fix, **not validation of the current whole-reply runtime**. Generated WAVs and transcripts are held only in ignored `outputs/voice-cutoff-diagnosis/`, not committed. No model weights, dependency pins or GPU placement changed.

Protocol completion does not prove that every word was spoken: early EOS can still omit or mispronounce words, and references influence prosody. No live ASR gate is added to generated speech; ASR was used only for the diagnostic comparisons. Current unit checks cover a single full-reply TTS call, identical retry payloads, final PCM, per-reply delivery snapshots, cancellation, failure and whole-playback memory commitment. A live listening check remains necessary for subjective emotional consistency.

## Deploy and verify

End the call, press Ctrl+C in the GPU launch terminal, and wait for the prompt. Pull the new commit and restart the existing profile:

```bash
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_ed25519_github -o IdentitiesOnly=yes' git pull --ff-only &&
bash scripts/run_live_stack.sh --profile quality-3gpu
```

No `--prepare` or model download is needed for this code change. Refresh the browser. The running `/api/status` should include `"voice_pipeline":"reply-locked-v3"`; a newer static file alone does not confirm that the Python process was restarted. Test typed playback first, listen to all sentences as one take, then change emotion between replies and check Stop reply.

Relevant files: `live_voice/voice.py`, `turns.py`, `providers.py`, `session.py`, `director.py`, `frontend/live.js`, `tests/test_phrase_chunker.py` and `tests/test_voice_generation.py`.
