# Drama 3 sampling correction

29 October 2026. This update addresses a concrete generation setting in the pinned vLLM-Omni runtime. It has CPU/source checks, but no new Surrey synthesis or listening result.

## Why the repeated token-limit error matters

The reported take reached `600/600` audio tokens, generated 27.9 seconds of waveform, and matched only 3 of 21 requested words. That establishes a substantial spoken omission. It does not mean that 27.9 seconds of useful speech were produced, or that a larger budget would recover the remaining words.

The [official SGLang-Omni Drama 3 cookbook](https://sgl-project.github.io/sglang-omni/cookbook/fishaudio_drama3.html#defaults-parameters) explicitly warns that capping the **main token** at `top_k=30` can make generation fall silent after its first phrase. Its default leaves main-token sampling uncapped while keeping acoustic/codebook heads capped at 30. This is a strong match to the observed failure pattern, not proof that every failed request has that cause.

Our pinned vLLM-Omni revision `798fef99a33fba3ad3c2439d6ec10fe838c0076b` has three relevant behaviours:

- Its [deployment defaults](https://github.com/vllm-project/vllm-omni/blob/798fef99a33fba3ad3c2439d6ec10fe838c0076b/vllm_omni/deploy/fish_omni.yaml) use `top_k=30` for stage 0.
- Its [model state](https://github.com/vllm-project/vllm-omni/blob/798fef99a33fba3ad3c2439d6ec10fe838c0076b/vllm_omni/model_executor/models/fish_omni/model_state.py) bounds an uncapped request by `model.max_top_k`, which defaults to 64. Sending `top_k=0` alone is insufficient.
- Its [frame sampler](https://github.com/vllm-project/vllm-omni/blob/798fef99a33fba3ad3c2439d6ec10fe838c0076b/vllm_omni/model_executor/models/fish_omni/sampling.py) shares the request's top-k between the main token and acoustic heads. Simply widening both does not reproduce the cookbook's separation.

## What the live launcher changes

The existing live TTS launcher now applies one exact source replacement before importing vLLM-Omni: `FusedFrameSampler.code()` passes `top_k.clamp(max=30)` to its acoustic-head draw. Main-token sampling and the existing repetition-aware second draw are untouched.

Its stage-0 overlay supplies `hf_overrides: {fish_omni_max_top_k: 4096}` so the main draw has capacity for all 4,096 supported candidates. Stage-0 defaults and each actual speech request explicitly select `temperature=1.0`, `top_p=0.9`, and `top_k=0`. The request overrides use the pinned speech endpoint's supported nested `extra_params` field, so the inspector/download shows what was actually submitted. The overlay is JSON, which is valid YAML for the deployment loader.

This is a limited correction in the existing vLLM backend. It does **not** implement SGLang's optional reference-sampling algorithm, epsilon cutoff, or its different main-token top-p crossing rule. It does not establish equivalent output quality between backends.

One whole reply still uses the same words, voice reference and emotion across attempts. Completion, spoken-word verification and the shared two-attempt maximum remain active. Actor/director sampling, model checkpoints, GPU placement and the matched CUDA 13.0 dependencies are unchanged. Increasing candidate capacity can change sampler cost; H200 memory use and latency need measurement after restart.

The plain `experiments/drama3/run.sh` standalone launcher retains its upstream deployment defaults. The correction and wider main-token capacity are installed together by `scripts/run_live_tts.py`, through the live stack. Do not use an unmodified standalone server to assess this live policy.

## Guarded installation and rollback

`live_voice/drama_sampling.py` requires this checkout's `.venv-drama3` interpreter and installed source inside that environment. It checks pinned Git provenance (or its identifying installed version when provenance metadata is absent), rejects editable/unknown installations, and verifies the complete source bytes. It loads no model and installs no packages.

| Source | SHA-256 |
|---|---|
| Verified original | `33db22e1338d4214a1e0ed0be919cdfba6b1865c095d0a17fa54dc222e332e03` |
| Corrected sampler | `038b2c5eea5a8a8ed48c435429c0a7b3bb2e2aed0cd265e7a783f7aad829c4d7` |

The helper retains an original-byte backup and a JSON marker beside the installed sampler. File replacement is atomic, so an installation hardlinked to uv's cache does not modify that cache. Unrecognised source or conflicting sidecars stops startup instead of guessing or reinstalling. Apply and restore are idempotent; the inspection command does not create files.

Stop the existing live job before changing its installed sampler. From the repository, these maintenance commands use the correct interpreter:

```bash
.venv-drama3/bin/python experiments/drama3/repair_sampling.py --check
.venv-drama3/bin/python experiments/drama3/repair_sampling.py --restore
```

Restoring the source alone is for inspection/maintenance: the next normal live launch deliberately reapplies the correction. To run the old live policy, roll back the application code and restore the installed source while stopped. Do not combine the widened main-token overlay with an uncorrected acoustic sampler.

## Updating the current Surrey deployment

From `login01`, stop the current batch job **70056** if it is still the job running this stack:

```bash
scancel 70056
squeue -j 70056
```

After that job has left the queue, pull and submit one new job from the checkout:

```bash
cd "/scratch4weeks/$USER/Character_Studio_Qwen"
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_ed25519_github -o IdentitiesOnly=yes' git pull --ff-only &&
sbatch scripts/run_live_slurm.sbatch
```

Use the new job ID to inspect `slurm-character-JOB_ID.log` and the assigned node. The TTS log must contain `Drama 3 sampling repair: patched; main candidates 4096; acoustic top-k 30` before model startup. Existing model downloads are reused; no setup command is needed. Reconnect the Mac tunnel to the actual assigned node, refresh the portal and start a fresh call.

The first live check should use a short plain reply and then the same selected voice/emotional delivery. Compare requested words with what is heard and record any safe coverage/finish diagnostics. A passing CPU fixture or successful server startup is not a successful listening test.

## SGLang as a separate backend

The linked cookbook is a useful alternative runtime. At inspected SGLang-Omni commit `b1097644c09fd3dbe00c26e46d114dbec87f923d`, its installation uses a different Torch/SGLang/FlashInfer stack and manual UCX/Flash Attention prerequisites. Its SSE PCM metadata and request validation also need an adapter. A migration should use a separate environment and retain whole-reply word checks; it should not overwrite the working `.venv-drama3` or be described as a tested Surrey installation.
