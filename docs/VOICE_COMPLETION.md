# Whole-reply completion and consistent emotion

Updated 6 November 2026. The live app now defaults to immediate PCM playback (`drama-stream-v1`); the policy below describes the retained `LIVE_VOICE_PLAYBACK=checked` mode. See [streaming behaviour and comparison](DRAMA3_LATENCY.md#live-playback-mode). The current policy is one continuous voice take per complete reply, with one fixed emotion and reference. The earlier sentence-level natural-delivery retry was reported to create unwanted mood changes and has been removed. [Voice failure diagnosis](VOICE_FAILURES.md) describes word checks, conservative quiet-tail trimming and separate generation/checking timings. The real-model comparisons below describe the earlier cue-format tests, not validation of the current recovery rules or sampler.

The later [sampling correction](DRAMA3_SAMPLING.md) separates main/acoustic top-k and masks residual codes outside the decoder's 1,024-entry vocabulary. Historical listening results below used the previous sampler; they do not validate the new policy.

## What failed

The app inserted two separate cues after the first two spoken words: an emotion label and a longer direction. On the reported line, that produced `It’s hard, [sad] [subdued, fragile voice with hesitant pauses] you know?...`. In direct tests, the same pattern generated only “I called” or “I love” for longer sentences and continued to the generation-token limit. Increasing that limit alone cannot restore the missing words.

The old expressive chunker also merged short sentences into longer synthesis requests. Streaming ASCII ellipses could fragment into a stray `..` at the start of another request. Neither issue should be treated as a change in the character's intelligence.

## Current behaviour

- The director's selected delivery is snapshotted before actor generation. Operator changes apply to the next reply.
- Actor text still streams to the transcript, but the app waits for the final text before calling TTS. The whole reply goes through one synthesis request with one speaker-prefixed descriptive cue. It is not split into independent sentence takes, which can vary their performance even with the same cue.
- Paired Markdown emphasis and inline backtick wrappers are cleaned from the final spoken text without removing any words. The transcript, voice request and saved retry use the same final wording. Voice-generation and speech-checking stages have separate status labels and wall-time counters, including failures and retries.
- The app collects the full take's SSE audio and terminal token-usage event before sending PCM. It validates 44.1 kHz mono PCM, alignment, size, complete events and clean end-of-stream. Uncapped takes receive the ordinary word-coverage check. A limited take can qualify only after exact normalized speech recognition and a strict quiet-tail check on the retained waveform; otherwise it stays blocked.
- A rejected take is retried at most once with the **identical full text, cue, emotion and voice reference**. A larger token allowance is used only when ongoing speech and a clean recognized prefix support it. Long silent stalls do not receive extra time. This applies to plain replies too. No retry drops emotional instructions, changes voice, or substitutes natural delivery. If both attempts fail, the interface reports failure without playing either rejected take.
- Temporary transport failures and HTTP 408/429/502/503/504 share that same one-retry budget, with a 250 ms pause before a transient-service retry. Configuration/format errors, other HTTP statuses and cancellation are not automatically retried. A mixed incomplete/service failure still gets at most two attempts.
- After a voice failure before any audio is released, **Retry voice** regenerates the saved full reply using its original cue and reference. It keeps the same turn and does not call the actor or director, duplicate text or add a participant utterance. New turns, interruption, a changed story revision or call closure invalidate the old retry. Concurrent retry clicks cannot launch two generations. A success still waits for playback acknowledgement before storing assistant speech.
- The message “Voice generation did not finish. Retrying the whole reply with the same emotion and voice.” is a retry notice, not a final failure. Temporary notices clear after successful voice generation, the next turn or disconnect; final and connection errors remain visible.
- Existing `phrase_start`, `phrase_done` and binary sequence IDs remain compatible, but sequence `0` now denotes the entire reply. Playback acknowledgement and heard-memory commitment wait until all its audio finishes. An interrupted reply is conservatively omitted as a whole; the system does not infer which words were heard.

**Latency tradeoff:** audio waits for complete actor text and a validated full TTS take. This adds startup delay compared with sentence streaming, while avoiding audible sentence-to-sentence take resets or fallback mood changes. Once ready, PCM is transported in bounded chunks and the browser preserves its native playback rate. Model prosody can still vary within one take; this change removes application-level emotion switches rather than guaranteeing a particular acted performance.

## Earlier diagnostic evidence and current limits

The real H200 comparison used five cue formats on three lines, followed by twelve speaker-prefixed descriptive takes covering all four reported sentences with male and female references. All twelve ended below their token budgets. Local Whisper recovered the full clauses in all twelve, with ten exact normalized word matches and two `called`/`call` transcription differences; `mum`/`mom` was normalized. Eight additional emotion/expression takes retained the full diagnostic line. These small tests do not establish general word accuracy or subjective emotional quality.

The preceding sentence-level controller was exercised against the running Drama 3 service and emitted four completed sentences with final playback acknowledgement accepted; local ASR recovered their full wording apart from mum/mom spelling. The second reported six-short-sentence reply also retained all six sentences with a male reference; automatic voice had a Called/Hold recognition difference. These are evidence for the earlier cue fix, **not validation of the current whole-reply runtime**. Generated WAVs and transcripts are held only in ignored `outputs/voice-cutoff-diagnosis/`, not committed. No model weights, dependency pins or GPU placement changed.

Protocol completion does not prove that every word was spoken: early EOS can still omit or mispronounce words, and references influence prosody. The current live app therefore uses its existing recognizer before release; earlier real-model comparisons used ASR only diagnostically. Normal word coverage is conservative, while limited-take recovery requires exact normalized words and a quiet ending. These checks can be affected by ASR errors and do not establish pronunciation or emotional quality. Unit checks cover full-reply requests, fixed words/delivery across retry, bounded budget growth, final PCM, cancellation, failure and whole-playback memory commitment. A live listening check remains necessary.

## Deploy and verify

End the call, press Ctrl+C in the GPU launch terminal, and wait for the prompt. Pull the new commit and restart the existing profile:

```bash
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_ed25519_github -o IdentitiesOnly=yes' git pull --ff-only &&
bash scripts/run_live_stack.sh --profile quality-3gpu
```

If using experimental listening, append `--listening nemotron-smart-turn` to the restart command. No `--prepare` or model download is needed for this code change. Restart the full stack, including Drama 3, so its imported sampler is replaced. Refresh the browser. The running `/api/status` should include `"voice_pipeline":"voice-checked-v5"` and `"voice_progress":true`; a newer static file alone does not confirm that Python restarted. The TTS startup log must show `acoustic vocabulary 1024`. Test typed playback first, listen to all sentences as one take, then change emotion between replies and check Stop reply. No live listening check of v5 has been performed.

Relevant files: `live_voice/voice.py`, `turns.py`, `providers.py`, `session.py`, `director.py`, `frontend/live.js`, `tests/test_phrase_chunker.py` and `tests/test_voice_generation.py`.
