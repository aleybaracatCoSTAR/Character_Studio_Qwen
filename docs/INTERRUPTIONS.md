# Speaking over a reply

**Allow interruptions** lets a participant begin speaking while the character is preparing or playing its reply. New live microphone calls enable it by default. It can be switched off during a call; **Stop reply** remains available in either mode. Text tests do not open or monitor a microphone.

The browser keeps a short microphone onset locally rather than sending all speaker output back into recognition. Sustained activity above a noise floor proposes an interruption. Browser echo cancellation and a comparison with recently audible character PCM reject strongly matching playback. When accepted locally, queued character audio stops, a turn-scoped interruption request is sent, and microphone audio is held until the server confirms that the old turn has stopped. The retained onset and following audio then enter the existing recognizer in order. Automatic interruption bypasses the ordinary 350 ms speaker-decay delay, preserving the first syllable.

The server cancels the old reply and background direction, resets its microphone recognizer, and temporarily reserves the conversational floor. Turn IDs, a lock and one accepted interruption per turn prevent a delayed or duplicate request from cancelling a newer response. A reservation without actual speech expires; it cannot hold narrative progression indefinitely. An interrupted reply is not recorded as fully heard. Existing complete-take TTS checks, per-reply emotion and voice settings remain unchanged.

## Speaker echo and limitations

The browser requests echo cancellation and noise suppression. Where the microphone advertises the `all` echo-cancellation mode, the app requests it, retaining the original setting if unsupported. The [W3C media-capture specification](https://www.w3.org/TR/mediacapture-streams/#dom-mediatrackconstraintset-echocancellation) distinguishes `all` from a plain `true` setting, whose scope the browser chooses. None of these settings establishes reliable separation of every voice and loudspeaker combination.

The playback comparison is a bounded heuristic, not a second neural speech detector or source-separation model. Room echoes, external-speaker delays, quiet voices, mixed speech and noise can defeat it. Headphones provide a useful first comparison. If the character stops without participant speech, turn **Allow interruptions** off and use **Stop reply** while diagnosing the speaker setup. Very short or quiet interjections may not cross the sustained-onset gate.

Playback is not muted or ducked just to test a suspected interruption; doing so would skip audible parts of an otherwise valid reply. A network timeout or rejected interruption discards the pending microphone buffer and shows a manual-stop fallback rather than forwarding it into an unknown turn. Muting, disabling interruptions, changing turns, manual stage actions and disconnecting clear stale candidates.

## Update and check on Surrey

Stop the currently running stack and wait for cleanup. In an active three-H200 terminal:

```bash
cd "/scratch4weeks/$USER/Character_Studio_Qwen"
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_ed25519_github -o IdentitiesOnly=yes' git pull --ff-only &&
bash scripts/run_live_stack.sh --profile quality-3gpu --listening nemotron-smart-turn
```

Omit the listening flag if using the baseline recognizer. For background deployment, use the existing [batch launcher](MULTI_GPU.md#background-job) after stopping the old job. No new model, environment or GPU allocation is required by this feature.

Refresh the browser after restarting Python. `/api/status` should include `"voice_interruptions":true`; the call's `ready` message confirms its enabled setting. Start a microphone call, let the character begin a longer reply, then speak a new sentence. Check that the character stops, the first word is recognized, and one new response follows. Repeat with interruptions off, mute, rapid turns, Stop reply, baseline recognition and optional Nemotron listening.

CPU/transport and browser-preview checks do not establish physical microphone behavior, speaker-echo accuracy or a measured end-to-end interruption delay. A real Surrey call is required for those checks.
