# Test Drama 3 latency first

The current checked conversation path receives streaming audio from Drama 3, but collects the entire take and checks spoken-word coverage before sending playable PCM to the browser. Its first audible response therefore includes synthesis completion, checking, and any permitted retry, as well as recognition, archive retrieval and director/actor work before TTS starts. `stream: true` at the model endpoint alone does not make this client path low-latency.

This direct benchmark bypasses recognition, archives, director and actor. It measures the first raw PCM packet and transport completion. It does not measure physical speaker onset or verify that every word was spoken. The existing checked conversation mode is unchanged.

## Start the existing Fish stack

End the portal call and save comparison results before stopping a model job. On the Surrey login node, inspect `squeue -u "$USER"`; cancel the actual old `character-omni` job before submitting the Drama 3 stack. Use the batch launcher once:

```bash
cd "/scratch4weeks/$USER/Character_Studio_Qwen"
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_ed25519_github -o IdentitiesOnly=yes' git pull --ff-only
LIVE_DEFAULT_ENGINE=baseline sbatch scripts/run_live_slurm.sbatch --listening baseline
```

This reuses installed models/environments and starts Drama 3, actor and director on the three allocated H200s. Wait for the new job to run and for all services to become ready. If you already have that stack running, reuse it; do not start a second TTS server on the same port/GPU. Match listening modes when later comparing whole conversation pipelines.

Forward the model's test port from another Mac terminal, replacing `costar07` with the newly allocated node if necessary:

```bash
ssh -N -i ~/.ssh/id_ed25519_costar \
  -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -L 127.0.0.1:18093:127.0.0.1:18093 \
  -J jq575gb@costar-login01.surrey.ac.uk \
  jq575gb@costar07.costar-cluster.lan
```

The portal's separate port-18091 tunnel can remain open. Keep the portal call ended during the benchmark so two requests do not compete for the voice service.

## Run on the Mac

From the local repository directory:

```bash
curl --fail http://127.0.0.1:18093/health
python3 experiments/drama3/benchmark_latency.py --voice female --warmup 1 --runs 5
open outputs/drama3-latency/listen.html
```

The client uses only Python's standard library. `--voice male` uses the other existing reference; `--voice none` omits reference encoding for a separate diagnostic control, without guaranteeing voice identity. Keep that control in a different output directory:

```bash
python3 experiments/drama3/benchmark_latency.py --voice none --warmup 1 --runs 5 --output outputs/drama3-latency-no-reference
```

`results.json` retains measured samples, median/min/max first-packet latency, median completion and real-time factor. Warmups are excluded. Every measured take has a WAV for manual listening. No reference-audio base64 or connection credentials are written into the report. A failed run stops the test and leaves earlier completed results available. Do not treat a partial set as the requested number of successful runs.

Listen for missing opening/closing words, changes in voice, and long silent tails. A transport that ends successfully is not proof the requested text was fully spoken. Keep the same short text and voice for fair repeated measurements. The neutral default avoids leading filler/event tags, a documented failure trigger in the pinned serving recipe.

## Interpret the result

* Fast first PCM but slow portal playback: complete-take buffering, speech verification/retry, and upstream director/retrieval work can account for the gap. Turning verification off alone does not remove the current complete-take buffer.
* Slow direct first PCM: investigate warmup, reference encoding, queueing, serving configuration and GPU load before changing the character LLM.
* Fast first PCM but long completion/silence: inspect the audio and generation ending. A higher token cap can extend silent tails; it is not a general latency fix.

For a lower-latency conversational mode, the next architectural change is forwarding PCM as it arrives. It can keep one fixed voice/emotion per reply, but cannot promise full-reply word verification before the listener hears it, and must not retry a whole reply after partial playback. Keep that as an explicit alternative to the checked mode rather than silently changing its completion behaviour. Incremental sentence synthesis additionally needs a separate continuity test because earlier experiments changed delivery or skipped words between chunks.

## Published numbers and scope

The [official vLLM recipe](https://recipes.vllm.ai/fishaudio/drama-3) reports 126 ms median first audio on a warm H100 at concurrency one. The [official SGLang cookbook](https://sgl-project.github.io/sglang-omni/cookbook/fishaudio_drama3.html) and [Fish model card](https://huggingface.co/fishaudio/drama-3) report 88.9 ms median first audio on an H200 using a pre-encoded voice reference. These are model-serving measurements under different conditions, not microphone-to-speaker figures or measurements of this portal. Our vLLM client currently sends WAV reference audio rather than those pre-encoded SGLang reference codes.

The pasted LiveKit discussion concerns Fish through LiveKit Inference, with mixed Drama 3 and S2.1 cases; it does not demonstrate a shared cause with a local Surrey deployment. No monitoring/privacy settings need changing to perform this test.

CPU tests verify benchmark accounting, reference reuse, safe reports and partial-result preservation. No new real-GPU latency result is claimed by adding this client. Do not replace the working CUDA wheels or update serving packages just to run it. Any SGLang comparison should use a separate environment and measured results on the same hardware/text/voice, preserving the current vLLM installation.
