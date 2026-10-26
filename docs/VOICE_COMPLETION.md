# Sentence completion and consistent emotion

Updated 26 October 2026. The previous playback/token-budget patch was confirmed in the served frontend, but the user still heard missing sentence endings and words. Direct requests to the running Drama 3 server reproduced failures before browser playback.

## What failed

The app inserted two separate cues after the first two spoken words: an emotion label and a longer direction. On the reported line, that produced `It’s hard, [sad] [subdued, fragile voice with hesitant pauses] you know?...`. In direct tests, the same pattern generated only “I called” or “I love” for longer sentences and continued to the generation-token limit. Increasing that limit alone cannot restore the missing words.

The old expressive chunker also merged short sentences into longer synthesis requests. Streaming ASCII ellipses could fragment into a stray `..` at the start of another request. Neither issue should be treated as a change in the character's intelligence.

## Current behaviour

- One descriptive cue precedes the spoken sentence, after the documented `<|speaker:0|>` speaker marker. There are no delivery cues inserted after the first two words and no stacked emotion-label/direction tags.
- The director chooses one steady delivery for a reply. Live delivery controls apply to the next reply. Short complete sentences are kept separate; punctuation waits for lookahead, and ellipses remain pauses. Unusually long sentences retain a bounded clause/word fallback.
- The app collects one sentence's SSE audio and its terminal token-usage event before sending PCM to the browser. It validates 44.1 kHz mono PCM, alignment, size, a complete event stream and an output-token count below the requested maximum.
- An incomplete or capped expressive take is discarded before playback and retried once without the performance cue, using the same voice reference. The interface explains the retry and natural delivery. A failed plain retry is reported, rather than silently claiming the sentence was spoken.
- If a later sentence fails, previously completed audio finishes playing. Only that completed prefix can enter heard conversation history; failed text is removed from the displayed reply.

This adds a sentence's synthesis time before playback. Later sentences can be prepared while earlier ones play; no full-reply gate is added.

## Validation and limits

The real H200 comparison used five cue formats on three lines, followed by twelve speaker-prefixed descriptive takes covering all four reported sentences with male and female references. All twelve ended below their token budgets. Local Whisper recovered the full clauses in all twelve, with ten exact normalized word matches and two `called`/`call` transcription differences; `mum`/`mom` was normalized. Eight additional emotion/expression takes retained the full diagnostic line. These small tests do not establish general word accuracy or subjective emotional quality.

The updated local sentence controller was also exercised against the running Drama 3 service and emitted four completed sentences with final playback acknowledgement accepted; local ASR recovered their full wording apart from mum/mom spelling. The second reported reply, “Just… sitting here. Called my mom. She asked if I’d met anyone. I said no. I couldn’t say the truth. Not again.”, was then synthesized as six separate sentences. The male-reference take retained all six in the local transcription; automatic voice had a `Called`/`Hold` recognition difference in one sentence. Retest with a fixed reference to compare delivery consistently. Generated WAVs and transcripts are held only in ignored `outputs/voice-cutoff-diagnosis/`, not committed. No model weights, dependency pins or GPU placement changed. The running browser application still needs a pull/restart before it uses this controller.

Protocol completion does not prove that every word was spoken: early EOS can still omit or mispronounce words, and references influence prosody. No per-sentence ASR gate is added to the live call; ASR was used only for the local diagnostic comparison. The natural retry prioritizes a complete sentence over preserving its expressive cue.

## Deploy and verify

End the call, press Ctrl+C in the GPU launch terminal, and wait for the prompt. Pull the new commit and restart the existing profile:

```bash
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_ed25519_github -o IdentitiesOnly=yes' git pull --ff-only &&
bash scripts/run_live_stack.sh --profile quality-3gpu
```

No `--prepare` or model download is needed for this code change. Refresh the browser. The running `/api/status` should include `"voice_pipeline":"sentence-checked-v2"`; a newer static file alone does not confirm that the Python process was restarted. Test typed playback before the microphone, listen to the complete last sentence, then change emotion between replies and check Stop reply.

Relevant files: `live_voice/voice.py`, `turns.py`, `providers.py`, `session.py`, `director.py`, `frontend/live.js`, `tests/test_phrase_chunker.py` and `tests/test_voice_generation.py`.
