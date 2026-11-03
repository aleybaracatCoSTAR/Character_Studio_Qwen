# Try Nemotron streaming recognition + Smart Turn

This optional experiment implements the listening part of Mark Boas's suggestions. Nemotron transcribes during speech; Smart Turn judges whether a pause ends the turn. The character actor, narrative director, archive, voice reference, one emotion per complete reply and spoken-word verification are preserved.

The director still runs before the actor reply in this first comparison. Moving it into the background is a separate experiment. Listening changes cannot remove time spent directing, synthesizing and verifying a whole voice take.

## Install once on Surrey

End the portal call. Stop the old stack with Ctrl+C in its foreground terminal, or `scancel JOB_ID` for your actual batch job, and wait for cleanup. Closing a batch log viewer does not stop the job. Do not run two stacks together.

Pull and prepare on Surrey; these installation/download commands do not need a GPU allocation or start inference:

```bash
cd "/scratch4weeks/$USER/Character_Studio_Qwen"
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_ed25519_github -o IdentitiesOnly=yes' git pull --ff-only
export LIVE_VOICE_STORE="/scratch4weeks/$USER/live-voice-store"
bash scripts/setup_listening.sh
.venv-listening/bin/python scripts/download_listening_models.py --download
```

This creates **`.venv-listening`** and stores pinned public checkpoints in **`$LIVE_VOICE_STORE/listening`**. It does not upgrade `.venv-live` or `.venv-drama3`. Setup targets Linux x86-64/Python 3.12. [Exact dependencies, revisions, file hashes and model terms](../experiments/listening/README.md).

## Start one experiment

From `login01`, after the old job has ended:

```bash
sbatch scripts/run_live_slurm.sbatch --listening nemotron-smart-turn
```

The batch script requests the existing three-H200 profile, 24 CPUs, 256 GiB host memory and four-hour limit. Use the returned job number:

```bash
squeue -u "$USER" -o "%.18i %.20j %.2t %.10L %R"
tail -n 60 slurm-character-JOB_ID.log
```

Replace `JOB_ID` with that number. Successful startup finishes with `Uvicorn running on http://127.0.0.1:18091`. The listener must become ready before the other models start; failed startup cleans up this launch's owned workers.

Alternatively, in an **already allocated three-H200 terminal**, use this instead of the batch command:

```bash
bash scripts/run_live_stack.sh --profile quality-3gpu --listening nemotron-smart-turn
```

Launch never downloads missing listening checkpoints. Installation/download remains explicit.

## Open and compare

Open **http://127.0.0.1:18091/** through the Mac tunnel to this job's actual allocated node. Keep the existing tunnel if it targets that node; otherwise [update its node](LIVE_SETUP.md#4-open-it-from-your-mac). Port 18095 is internal and needs no extra forwarding.

The page shows **Experimental listening · Nemotron + Smart Turn** and a temporary transcript while speaking. Only finalized words enter conversation history, archive queries and model prompts. Cancellation discards interim words. The microphone still waits during replies; **Stop reply** handles deliberate interruption.

Use the same character and microphone arrangement in both modes. Try a short question, a thought with an internal hesitation, a self-correction, a sentence with a distinctive last word, and two turns in a row. Also try muting or typing while recognition is unfinished. This initial checkpoint is **English-only**.

Compare missed words, premature replies, unnecessary waiting and the displayed stage timings. Recognition timing is separate from director, actor, complete voice synthesis and verification. A shorter final recognition wait alone does not prove a faster complete conversation.

## Behavior and placement

| Part | Baseline | Experiment |
|---|---|---|
| Input recognition | Faster-Whisper base.en, CPU | Nemotron Speech Streaming EN, 0.6B parameters, GPU |
| Recognition work | Provisional completed-window recognition | One cache-aware RNNT stream fed during speech |
| End of turn | 400 ms VAD silence | Smart Turn v3.2 CPU waveform decision after at least 300 ms silence |
| Uncertain pause | Fixed silence endpoint | Check every 200 ms; explicit 1,200 ms pause fallback |
| Utterance bound | 15 seconds | 15 seconds |
| TTS word verification | Separate Faster-Whisper recognition | Preserved; never shares the participant's live ASR stream |

Nemotron uses six lookahead tokens, a **560 ms model chunk**, with small microphone transport packets. The final short window is padded so it is processed. Chunk duration is a configuration, not a measurement of conversational delay. Smart Turn sees the last eight seconds of waveform. Low-confidence pauses continue until speech resumes or the explicit pause limit is reached. Errors are visible; there is no silent Whisper fallback.

Metrics distinguish `smart_turn`, `pause_limit`, `endpoint_timeout` and `max_duration`, and include final recognition wait. Interim text is a cumulative hypothesis and can change.

The experiment requires **`quality-3gpu`**. Drama 3 stays on the first allocated GPU, the actor on the second, and Nemotron shares the third with the director. Only the experimental director budget changes from 85% to 75%. Smart Turn runs on CPU. Actual Slurm device tokens are respected; no fourth GPU is requested. H200 fit and speed still need a real run.

| File | Purpose |
|---|---|
| `scripts/setup_listening.sh`, `experiments/listening/requirements.txt` | Isolated package setup |
| `scripts/download_listening_models.py` | Explicit download and offline hash checks |
| `listening_runtime/nemotron.py` | Transformers 5.13 streaming RNNT and bounded audio |
| `listening_runtime/turn.py` | Smart Turn 3.2 CPU ONNX inference |
| `listening_runtime/app.py` | Loopback service on 18095 |
| `live_voice/listening.py` | Portal client, VAD buffering, endpointing and cancellation |
| `live_voice/listening_config.py` | Opt-in mode and GPU budget |
| `frontend/live-listening.mjs` | Temporary transcript and listening state |

The listener accepts one utterance at a time, retains bounded active audio/cache and does not save transcripts or recordings to logs. Inference loads local files only. Existing prompt inspection/download shows finalized model inputs.

## Return to the baseline

Stop the experiment, wait for cleanup, then use:

```bash
sbatch scripts/run_live_slurm.sbatch --listening baseline
```

Or in an allocated interactive terminal:

```bash
bash scripts/run_live_stack.sh --profile quality-3gpu --listening baseline
```

Baseline remains the default without a listening flag/environment setting. Existing character and archive data are reused.

## Diagnose startup

```bash
tail -n 80 /scratch4weeks/$USER/live-voice-store/logs/listening.log
.venv-listening/bin/python scripts/download_listening_models.py --check
```

Use the same `LIVE_VOICE_STORE`/`LISTENING_STORE` override for download, validation and launch. Missing or changed files fail before model startup. Keep first GPU failure output rather than changing packages in the working voice environment.

**Validation boundary:** local tests use synthetic PCM, fake model implementations and temporary loopback services. They check streaming, concurrent turn decisions, final-window preservation, interim versus finalized text, cancellation, readiness, launch arguments and baseline compatibility. No new checkpoint was installed or run on a GPU during implementation; no latency gain is claimed yet.
