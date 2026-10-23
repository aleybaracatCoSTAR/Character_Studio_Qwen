# Narrative director and beats

[Preview the narrative editor](screenshots/narrative-beats-preview.jpg).

This iteration brings the pattern of the developer's `ArcDirector` in `orchestrator/studio_voice.py` into the Qwen + Drama 3 pipeline. It is a new controller for these models, not a PersonaPlex checkpoint or a port of its pinned-memory architecture.

## Rehearsal workflow

1. Open **Director script**. Enable **Follow narrative beats** and **Guide each reply**.
2. Load **Anna example**, or edit/add/remove beats. One beat editor opens at a time by default. Edit the title, goal, private entry note, feeling, vocal direction and minimum/maximum seconds. The script supports 1–12 beats; Anna has five.
3. Choose advancement: **Director** (evidence with a time-limit fallback), **Time limits only**, or **Performer only**. Start a call.
4. The main view shows the current beat, progress dots, elapsed time and why it changed. Expand **Beat goal & entry note** to inspect its private context.
5. Use **Hold** to pause automatic progression; **Resume** restores the clock. **Next beat** and **Previous** are performer overrides. During a reply a requested move is queued until playback finishes. On the last beat, Next becomes **Finish story**.
6. Send **Direction for this scene** at any time. It is a private instruction, applied to the next answer even when the LLM director is disabled. It does not replace saved character facts.
7. **Cue character** explicitly asks for an in-world response to the current beat without waiting for a visitor question. Beat changes alone update the situation; they do not interrupt playback or automatically start speech.

Finishing the story marks the arc complete; it does not cancel the call or the Slurm allocation. The character can say farewell naturally. End the call to edit the outline. Script edits are saved in this browser and a fresh call starts from beat one.

## Anna's example

| Beat | Dramatic aim |
|---|---|
| An overlooked machine | Feel noticed without reciting a biography |
| An invitation to help | Offer one manageable washing task |
| The blinking reset light | Explain why memories matter; ask for help |
| Letting someone in | Respond to what the visitor actually offers |
| A small possibility | Acknowledge a real connection, without inventing a successful outcome |

These are directions, not fixed dialogue. The blinking light is an authored event; it does not mean the visitor pressed reset. The existing emotional-trigger control remains separate and can override vocal delivery while its threat is active.

## Compared with the old demo

| Old demo concept | Qwen/Drama 3 implementation |
|---|---|
| Five authored arc beats | Editable 1–12 beats, with a five-beat Anna example |
| Beat-entry private notes | Current beat note, goal and feeling added to the actor's runtime context |
| LLM judges `beat_done` using transcript | Validated director output with `beat_done` and `beat_evidence` |
| Minimum and maximum beat times | Server-enforced bounds; maximum is a pacing fallback, not evidence the goal happened |
| Periodic/utterance checks | Per-utterance planning and between-turn checks no more often than 12 seconds, on new heard material |
| Notes when a beat stalls | Private between-turn notes, capped at two per beat with a 20-second gap |
| Performer advances and sends notes | Previous/Next/Hold/Cue controls and live private direction |
| PersonaPlex speech from pinned memory | Streamed Qwen dialogue → Drama 3 voice; recognition ends each utterance after a pause |

The old demo's embedding-based memory retrieval, speaker recognition, persistent person memories and recorded session archives are not ported in this change. The Qwen app retains its existing bounded conversation history and character memory text.

## Evidence and timing rules

The server only accepts an LLM completion after the minimum time, while not held. `beat_evidence` must contain at least eight characters and match a quotation from this beat's heard conversation or the newest participant utterance. Matching normalises case and whitespace. It does not prove that the LLM judged the dramatic goal correctly; the performer can retain control with manual mode.

Generated but unplayed actor text is not evidence. Completed browser playback acknowledgements enter heard history; private stage cues are excluded from the participant's heard utterances. Evidence from a previous beat is cleared on entry to the next. A director decision arriving after a beat change is discarded. New participant turns cancel pending between-turn checks.

A one-second clock checks timeouts; state updates are sent every five seconds and on transitions. Automatic moves wait while an answer is generating or playing. Hold freezes the clock and blocks model-driven completion, but permits conversation and deliberate performer moves. **Stop reply** remains the explicit way to interrupt audio.

## Files and protocol

- `live_voice/narrative.py`: validation, clock, state, evidence gate, bounded private notes.
- `live_voice/session.py`: turn-boundary transitions, heard evidence, background director checks and cancellation.
- `live_voice/director.py`: structured intention/delivery and beat completion output.
- `characters/anna-story.json`: editable example outline.
- `frontend/live-story.mjs`: small beat editor; `frontend/live.js`: rehearsal controls and progress.

The start message accepts `narrative: {enabled, mode, beats}`. Each beat has `title`, `goal`, `note`, `emotion`, `delivery`, `min_seconds` and `max_seconds`. Runtime commands use `{type:"story", control:{action:"next"|"previous"|"pause"|"resume"|"restart"|"cue"}}`. `story_state` contains current beat, index, revision, elapsed time, completion state and transition reason. `story_decision` explains a rejected/unavailable model decision. The single-call same-origin WebSocket remains the transport; this is not yet a session-addressed REST API for game engines.

## Validation status

CPU tests cover bounds, pause/resume, minimum time, invented and old evidence, completion, stale decisions, cancellation, playback boundaries, manual notes and WebSocket controls. Browser checks cover the editor and script preview. The new arc and the larger model profiles have not been run on Surrey GPUs or validated as a live performance. See [multi-GPU setup](MULTI_GPU.md) for the next test.
