# Character Studio · Qwen

A small rehearsal space for creating a character and talking to it using **Qwen3-Omni-30B-A3B-Instruct**. Edit the character on the left, then type or record a question on the right. Anna, an underappreciated washing machine in a wealthy household, is the included fictional example.

**Find and show artworks by voice or text:** archive search now prioritizes artist, title and artwork date, with limited spelling tolerance for examples such as “Agnolo Gadi” and “Madonne and Child Entrhoned with Twelve Angels.” “Thirteen eighty-seven” searches the year 1387. Say **Show me the image** to put the current artwork candidates directly in chat; **Open image** opens a separate view. Original catalogue spellings, source links and ambiguity are preserved. [Examples, context and search limits](docs/ARTWORK_SEARCH.md).

**More museum collections for Robin:** **Archive → + Add museum collection** now imports artist/title/subject selections from the Met, Art Institute of Chicago, Cleveland Museum of Art, Rijksmuseum and Getty, alongside NGA. Choose 25–200 records in the portal; the imported collection stays selected with your existing sources and opens its matching records first. Retrieved artwork images, catalogue facts, rights and museum links use the existing chat cards. This is a bounded selection from each museum, not a full-catalogue mirror or visual-analysis model. [Museum setup, source details and operator import commands](docs/MUSEUM_COLLECTIONS.md).

**Create more characters in the live portal:** choose **New character** beside the character picker, enter a name and optional role, then fill their details, memories, environment, voice and director script. Each profile saves separately in this browser and appears in **Your characters**. Existing characters are preserved; end the current call before creating or switching. On `/archive/`, new characters start with access to the whole local archive. Pull and refresh the page to use this frontend update; it works with baseline and experimental listening.

**Karim is now a built-in choice:** the Egyptian queer singer/songwriter, out in London but not to his parents and navigating faith and family, is available in **Your characters**. This starter reconstructs Aley's brief; it does not replace or claim to recover the original profile. It has a male reference voice, free conversation and three optional manual beats. The main library also preserves distinct surviving legacy character records before replacing old storage keys. [Karim and recovery limits](docs/KARIM.md). Pull, restart the stack and refresh to load the new preset.

**Speak to interrupt:** new microphone calls enable **Allow interruptions**. A sustained microphone onset stops the old reply and preserves the beginning of the participant's speech through a server-confirmed handover. Browser echo cancellation and recent-playback comparison reduce likely speaker echoes; they are not a guarantee for every speaker setup. The switch can be turned off live, and **Stop reply** remains available. [Behavior, limits and restart](docs/INTERRUPTIONS.md). No extra model or GPU is required; a real microphone comparison is still needed.

**Art character and multiple collections:** select **Robin**, the new fictional gallery guide, to consult the **National Gallery of Art** catalogue. Open **Archive** to choose collections or **+ Add collection** to import another JSON/JSONL/CSV database export. Retrieved images, artwork details and source links appear beside chat. Full local validation indexed **146,454 artworks**, including **63,884 open-access thumbnails**; run the importer on Surrey before that server has the data. This is SQLite/FTS lexical RAG with grounded text, not image analysis or model training. QTM storage stays separate. [Import, UI, format and limits](docs/RAG_COLLECTIONS.md).

**New optional listening experiment:** [Try Nemotron GPU streaming ASR + Smart Turn v3.2](docs/LISTENING_EXPERIMENT.md), following Mark Boas's suggestions. It transcribes during speech and judges candidate pauses, with a temporary live transcript. A separate environment preserves the working voice dependencies. The three-GPU launcher shares the director GPU and accepts `--listening nemotron-smart-turn`; `baseline` remains the default. The director and complete one-emotion voice validation stay in place for this first comparison. CPU/protocol checks do not establish GPU fit or a latency improvement; the guide includes Surrey install/start and comparison commands.

**Live voice experiment added:** [Follow the live setup guide](docs/LIVE_SETUP.md) for continuous microphone input, automatic pause detection, interruption and streamed Drama 3 speech. That mode uses English Faster-Whisper + a small Qwen3-4B text model + Drama 3, separately from the original Qwen-Omni 30B baseline described below. [View the live UI preview](docs/screenshots/live-preview.jpg).

**The live mode now includes [narrative beats and performer controls](docs/NARRATIVE.md), plus [two-/three-GPU quality profiles](docs/MULTI_GPU.md).** The [director orchestrator](docs/DIRECTOR.md) works as follows: a backstage LLM plans each reply’s intention and vocal delivery, Qwen acts the character, and Drama 3 speaks. The director defaults to a separate call to the existing Qwen server; a different running LLM endpoint can be configured. The sidebar includes Female/Male reference voices, custom WAV upload, vocal emotion and an editable emotional trigger. Anna's example is **Press reset → Anxious → fear of losing memories**. With **Allow interruptions**, speak to take over; with it off, the microphone waits during replies. **Stop reply** works in both modes. [Preview the trigger editor](docs/screenshots/contextual-reaction-preview.jpg). Triggers now use a local-model contextual decision rather than exact wording. Vocal direction is fixed for a complete reply, synthesized as one continuous take without a natural-delivery fallback; the new director and audible performance still need a live listening test. The earlier trigger-only check scored 11/12 on its small diagnostic set; see [verification](docs/DIRECTOR.md#verification).

**Response timing and conversation context:** the live app now waits 400 ms of silence, starts recognition provisionally at 200 ms and reuses it only when the final audio matches exactly. Microphone packets are 40 ms. It sends twelve recent exchanges plus relevant older quotes to the character, with a bounded RAM archive for the current call. Ending the call clears that memory. Whole-reply emotional direction and voice validation are preserved. See [timing and memory details](docs/LIVE_SETUP.md); actual GPU latency improvement has not yet been measured.

**Inspect and download model inputs:** choose **See what the model sees** beside the live conversation title. Your selected request stays still until you choose another or **Show latest**. **Download request (.txt)** saves its readable prompts, context and settings; **Download recent (.json)** saves the recent captured request log. Copying JSON is also available. [How the viewer works](docs/MODEL_INPUTS.md).

**KADEN is ready to rehearse:** select **KADEN** in the live sidebar's Character picker. Existing profiles saved at this browser address stay separate choices with their own voice, director and story settings. KADEN includes a male voice reference and five performer-controlled beats adapted from the Lighthouse run sheet. [Quick test and ending cues](docs/KADEN.md).

**Character + archive:** open `/archive/` for **Mika**, an editable character that consults [Queering the Map](https://www.queeringthemap.com/) stories during voice or text conversation. The archive now supports a local SQLite database, full-text search, pagination and JSON/JSONL/CSV collection import. Choose **Whole local archive** to search all imported stories, or retain a curated selection. The official public snapshot imported locally on 2 November 2026 contains **123,209 stories**. Excerpts and source links appear beside replies; normal Karim/KADEN settings stay separate. [Import the larger collection on Surrey](docs/QTM_DATABASE.md) · [Scope, grounding and attribution](docs/ARCHIVE.md).

**Beat replies and voice recovery:** entering a new narrative beat now prompts one character reply, waiting for participant speech and current playback to finish. Director controls start collapsed behind **Show director controls** so the transcript has more room. Temporary voice-service failures share one automatic retry with incomplete takes; **Retry voice** can then regenerate the existing reply's audio with its original wording and delivery, without rerunning the actor or director.

**Voice completion now checks spoken coverage:** the live app uses its existing recognizer to catch substantial missing speech before playback, including a completed response that contains only the opening sentence. Rejected takes share the same two-attempt limit and keep their emotion/reference. Failures now include a specific safe reason code; automatic voice anchoring excludes invalid-duration and rejected takes. This adds a recognition step and is not a cure for Drama 3's documented generation limitations. [Failure diagnosis and limits](docs/VOICE_FAILURES.md).

**Token-limit recovery:** a take that reaches its frame allowance is no longer rejected solely for that reason. The complete transport must validate, then the recognizer must match every normalized requested word with no extras, and a strict quiet ending must be present. Only the verified quiet suffix is trimmed, preserving a 400 ms pad. Ongoing speech with a clean recognized prefix can give the one retry a larger budget; silent stalls cannot. Words, voice and emotion stay fixed. [Recovery conditions](docs/VOICE_FAILURES.md#reaching-the-limit-does-not-reveal-what-was-spoken).

**Drama 3 sampling correction:** the live launcher separates main and acoustic sampling and now masks residual audio codes outside the decoder's 1,024-entry vocabulary. The correction upgrades the existing pinned environment without changing checkpoints or CUDA dependencies. One emotion, the chosen reference and whole-reply word checks stay in place. It needs a full stack restart and real-model listening test; [evidence, update commands and rollback](docs/DRAMA3_SAMPLING.md).

**Clearer voice timing:** the portal shows **Generating voice**, **Checking speech** and **Retrying voice**, with separate wall times including failed attempts. Clearly quiet tails are trimmed before recognition while preserving a 400 ms pad and the word check; paired presentation markup is cleaned without changing spoken words. These changes remove avoidable work and expose the remaining wait; a GPU speedup or improved acoustic consistency has not yet been measured. [Voice diagnosis and limits](docs/VOICE_FAILURES.md#generation-checking-and-delay).

**Status: starter prototype.** CPU transport tests and browser checks pass. A short standalone Drama 3 synthesis succeeded on Surrey through the Mac SSH tunnel on 23 October 2026; [details](experiments/drama3/README.md#validation). The full live stack, combined H200 memory use, spoken quality and live response time still need validation. No model weights are included. This repository is separate from CharacterPlex and does not modify it.

![Character sheet and conversation preview](docs/screenshots/studio-preview.jpg)

**Surrey background launch:** after installation, submit `sbatch scripts/run_live_slurm.sbatch` from the repository on `login01` to run the three-GPU stack without keeping an allocation terminal open. It requests one four-hour job; the actual browser tunnel must still target its allocated node. [Batch setup and logs](docs/MULTI_GPU.md#background-job).

## What it does

- Editable identity, goal, environment, memories, boundaries, story moment and voice description.
- Text or microphone input; record up to 30 seconds, listen back, then send.
- Generated text and an audio player for Qwen's spoken reply; text-only replies are also available.
- View the exact character instructions; download the character as JSON.
- Local browser saving for the character; recent conversation context in server RAM.

The original baseline uses **one recorded turn, then one reply**. The new `--live` mode streams text, validates one continuous voice take per reply and supports interruption; it is a cascaded voice pipeline, not a native full-duplex model. Neither mode implements model training or the full existing Character Studio feature set.

The initial GPU trial has been reported as too slow for a live conversation. See [the voice behaviour and streaming runtime notes](docs/LIVE_VOICE.md) for the confirmed bottlenecks and the distinction between this baseline and a live call.

## Preview on your laptop

Use Python 3.10+ or `uv` (the setup script uses Python 3.12 with uv).

```bash
git clone git@github.com:aleybaracatCoSTAR/Character_Studio_Qwen.git
cd Character_Studio_Qwen
bash scripts/setup.sh preview
bash scripts/run.sh --preview
```

Open **http://127.0.0.1:18091/**. Preview mode lets you edit and inspect character instructions. It does not download Qwen or invent AI responses.

## Run the real model

Follow **[the Surrey guide](docs/SURREY.md)**. The starting target is one allocated H200, BF16, one generation at a time. There is no paid API key required by this app. Checkpoint storage is roughly 71 GB; allow additional room for dependencies and caches. Actual GPU requirements depend on context and must be measured.

## Where everything is

| File | Purpose |
|---|---|
| `frontend/index.html` | Page structure and character fields |
| `frontend/styles.css` | Appearance and responsive layout |
| `frontend/app.js` | Character editing, recording, chat and playback |
| `frontend/recorder-worklet.js` | Microphone PCM capture |
| `studio/app.py` | Python FastAPI server and HTTP endpoints |
| `studio/characters.py` | Character schema and instructions sent to the model |
| `studio/engine.py` | Qwen loading, generation and recent conversation context |
| `studio/audio.py` | WAV validation, resampling and reply encoding |
| `characters/anna.json` | Example character |
| `live_voice/` | Continuous-mic app, turn detection, local providers and interruption |
| `live_voice/recognition.py`, `live_voice/turns.py` | Safe recognition preparation and bounded active-call conversation memory |
| `live_voice/listening.py`, `listening_runtime/` | Optional streaming recognition and Smart Turn service in its own environment |
| `frontend/live.*`, `frontend/live-audio.mjs` | Live call page, controls and streamed PCM playback |
| `scripts/run_live_stack.sh` | Start the live app and both model servers |
| `scripts/` | Environment setup, model download and launch |
| `slurm/` | Optional batch job for Surrey |

Read [architecture](docs/ARCHITECTURE.md) for the request flow and [handoff notes](docs/HANDOFF.md) for validation, limitations and the next tests.

## Optional voice comparison: Drama 3

[Drama 3 test instructions](experiments/drama3/README.md) prepare a separate TTS server and three Anna voice samples, with streamed first-audio timing and playable WAV output. This tests expressive delivery; Drama 3 is not a speech-to-speech conversational model. It uses its own environment and does not replace the Qwen backend.

## Checks without a GPU

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q studio live_voice scripts experiments/drama3 tests
node --check frontend/app.js
node --check frontend/recorder-worklet.js
node --input-type=module --check < frontend/live.js
node --test tests/*.test.mjs
for script in scripts/*.sh experiments/drama3/*.sh slurm/qwen-studio.sbatch; do bash -n "$script" || exit; done
```

571 Python tests cover streaming listening and Smart Turn, real loopback protocol, interim/final transcript separation, cancellation and baseline compatibility, isolated setup/download manifests, GPU launch/configuration, inherited CPU-mask cleanup, scalable archive import/indexing, guarded sampling repair, spoken coverage, character presets, model-input exports, beat replies and whole-reply delivery. 138 JavaScript tests cover the live controls, temporary listening feedback and independent character creation/persistence. Inference uses explicit test doubles; transport and shutdown checks use real temporary CPU processes and sockets. Browser checks verify creating, editing, switching and refreshing a new character. Separate browser and complete-corpus import/deduplication checks used the public snapshot without calling speech models. These do not demonstrate model quality or real GPU speed.

## Model and implementation sources

The inference adapter targets **Transformers 5.2.0**, following [Qwen's current guidance](https://github.com/QwenLM/Qwen3-Omni#installation). Its generate return values were checked against the [5.2.0 implementation](https://github.com/huggingface/transformers/blob/v5.2.0/src/transformers/models/qwen3_omni_moe/modeling_qwen3_omni_moe.py). The checkpoint is [Qwen/Qwen3-Omni-30B-A3B-Instruct](https://huggingface.co/Qwen/Qwen3-Omni-30B-A3B-Instruct), pinned in `scripts/download_model.py`.

New application code is MIT licensed. Qwen model files have their own [Apache 2.0 license and model card](https://huggingface.co/Qwen/Qwen3-Omni-30B-A3B-Instruct). Models, recordings, credentials and generated media are excluded from Git.
