# Drama 3 sampling correction

**5 November 2026: repair v2.** The live launcher now restricts residual acoustic tokens to the DAC codec's valid vocabulary, in addition to the earlier main/acoustic top-k separation. The source defect and guarded upgrade have CPU regression tests. No live GPU synthesis or listening test was performed for this update; improved sound, latency and reliability remain unmeasured.

## Why the repeated token-limit error matters

The reported take reached `600/600` audio tokens, generated 27.9 seconds of waveform, and matched only 3 of 21 requested words. That establishes failed word coverage, consistent with substantial missing speech; recognition alone cannot establish exactly what was audible. It does not mean that 27.9 seconds of useful speech were produced, or that a larger budget would recover the remaining words.

The [official SGLang-Omni Drama 3 cookbook](https://sgl-project.github.io/sglang-omni/cookbook/fishaudio_drama3.html#defaults-parameters) explicitly warns that capping the **main token** at `top_k=30` can make generation fall silent after its first phrase. Its default leaves main-token sampling uncapped while keeping acoustic/codebook heads capped at 30. This is a strong match to the observed failure pattern, not proof that every failed request has that cause.

Our pinned vLLM-Omni revision `798fef99a33fba3ad3c2439d6ec10fe838c0076b` has three relevant behaviours:

- Its [deployment defaults](https://github.com/vllm-project/vllm-omni/blob/798fef99a33fba3ad3c2439d6ec10fe838c0076b/vllm_omni/deploy/fish_omni.yaml) use `top_k=30` for stage 0.
- Its [model state](https://github.com/vllm-project/vllm-omni/blob/798fef99a33fba3ad3c2439d6ec10fe838c0076b/vllm_omni/model_executor/models/fish_omni/model_state.py) bounds an uncapped request by `model.max_top_k`, which defaults to 64. Sending `top_k=0` alone is insufficient.
- Its [frame sampler](https://github.com/vllm-project/vllm-omni/blob/798fef99a33fba3ad3c2439d6ec10fe838c0076b/vllm_omni/model_executor/models/fish_omni/sampling.py) shares the request's top-k between the main token and acoustic heads. Simply widening both does not reproduce the cookbook's separation.

## The residual vocabulary defect

The pinned [audio-head configuration](https://github.com/vllm-project/vllm-omni/blob/798fef99a33fba3ad3c2439d6ec10fe838c0076b/vllm_omni/model_executor/models/fish_omni/configuration_fish_omni.py) gives the heads a 4,096-wide vocabulary. The [actual DAC construction](https://github.com/vllm-project/vllm-omni/blob/798fef99a33fba3ad3c2439d6ec10fe838c0076b/vllm_omni/model_executor/models/fish_speech/dac_utils.py) defines one semantic codebook with 4,096 entries and nine residual codebooks with **1,024 entries each**. Valid residual IDs are therefore **0–1023**.

The original `FusedFrameSampler.code()` draws from the full residual logits without masking IDs 1024–4095. Repair v1 added acoustic top-k 30 but still did not restrict that vocabulary. Top-k limits the number of candidates considered; it does not make an out-of-range ID valid. An adversarial CPU regression executing the original and v1 methods confirms that a high-logit invalid ID can win. This is a confirmed source-level defect, separate from whether it caused any particular failed recording.

## What the live launcher changes

Before importing vLLM-Omni, the live TTS launcher applies one exact source replacement in `FusedFrameSampler.code()`. Repair v2 clones the residual logits, sets `residual_logits[..., 1024:]` to negative infinity, and passes `top_k.clamp(max=30)` to the acoustic draw. It preserves the **4,096-wide logits and candidate/noise shapes** required by the captured sampler; it does not shrink the tensor to 1,024 columns. The original input tensor is not modified. Main-token sampling and its existing repetition-aware second draw are untouched.

Its stage-0 overlay supplies `hf_overrides: {fish_omni_max_top_k: 4096}` so the main draw has capacity for all 4,096 supported candidates. Stage-0 defaults and each actual speech request explicitly select `temperature=1.0`, `top_p=0.9`, and `top_k=0`. The request overrides use the pinned speech endpoint's supported nested `extra_params` field, so the inspector/download shows what was actually submitted. The overlay is JSON, which is valid YAML for the deployment loader.

This is a limited correction in the existing vLLM backend. It does **not** implement SGLang's optional reference-sampling algorithm, epsilon cutoff, or its different main-token top-p crossing rule. It does not establish equivalent output quality between backends or prove that the reported first-phrase stalls are resolved.

One whole reply still uses the same words, voice reference and emotion across attempts. Completion, spoken-word verification and the shared two-attempt maximum remain active. Actor/director sampling, model checkpoints, GPU placement and the matched CUDA 13.0 dependencies are unchanged. Increasing candidate capacity can change sampler cost; H200 memory use and latency need measurement after restart.

The plain `experiments/drama3/run.sh` standalone launcher retains its upstream deployment defaults. The repair and wider main-token capacity are installed together by `scripts/run_live_tts.py`, through the live stack. An unmodified standalone server does not exercise this live policy.

## Guarded installation and rollback

`live_voice/drama_sampling.py` requires this checkout's `.venv-drama3` interpreter and installed source inside that environment. It checks pinned Git provenance (or its identifying installed version when provenance metadata is absent), rejects editable/unknown installations, and verifies the complete source bytes. It loads no model and installs no packages.

| Source | SHA-256 |
|---|---|
| Verified original | `33db22e1338d4214a1e0ed0be919cdfba6b1865c095d0a17fa54dc222e332e03` |
| Repair v1: acoustic top-k only | `038b2c5eea5a8a8ed48c435429c0a7b3bb2e2aed0cd265e7a783f7aad829c4d7` |
| Repair v2: valid residual IDs plus acoustic top-k | `f228a123b01acac452c35092fe170f2a1ca7bb6e3195edadb086b702f9cabf94` |

The helper accepts only these complete, verified source versions. A known original is upgraded directly to v2. A known v1 is upgraded to v2 while preserving its verified original backup and replacing the recognized schema-1 marker with schema 2. If the exact v1 source has no sidecars, the helper can reconstruct and hash-check the original before creating its backup. Existing backup bytes must always match the verified original. Unknown source, unrecognized markers, conflicting backups and symbolic-link sidecars stop startup instead of guessing or reinstalling.

The original-byte backup and JSON marker sit beside the installed sampler. Replacements are atomic directory-entry changes, so an installation hardlinked to uv's cache does not modify that cache. The upgrade can safely resume after an interrupted source replacement. Apply and restore are idempotent; `--check` creates no lock, backup, marker or modified source. Inspection distinguishes `original`, `patched-v1` and `patched`; the current repair reports `repair_version: 2`.

Stop the existing live job before changing its installed sampler. From the repository, these maintenance commands use the correct interpreter:

```bash
.venv-drama3/bin/python experiments/drama3/repair_sampling.py --check
.venv-drama3/bin/python experiments/drama3/repair_sampling.py --apply
.venv-drama3/bin/python experiments/drama3/repair_sampling.py --restore
```

Normal startup applies or upgrades the repair automatically; manual `--apply` is optional maintenance while stopped. Restore supports either recognized v1 or v2 and requires a verified original backup and recognized marker. A sampler already imported into a process must be restarted before applying an upgrade or restoring it.

Restoring the source alone is for inspection/maintenance: the next normal live launch deliberately reapplies v2. To run an old live policy, roll back the application code and restore the installed source while stopped. Do not combine the widened main-token overlay with an uncorrected acoustic sampler.

## Updating the current Surrey deployment

End the portal call and stop the current stack. For an interactive launch, press Ctrl+C in its Surrey terminal and wait for cleanup. For a batch launch, identify its actual job number from `login01`:

```bash
squeue -u "$USER" -o "%.18i %.20j %.2t %.10L %R"
```

Replace `JOB_ID` below with the number of the Character Studio job being restarted:

```bash
scancel JOB_ID
squeue -j JOB_ID
```

After that job has left the queue, pull and submit one new job from the checkout. The existing model downloads and matched environments are reused; no setup, dependency upgrade or checkpoint download is required for this repair.

```bash
cd "/scratch4weeks/$USER/Character_Studio_Qwen"
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_ed25519_github -o IdentitiesOnly=yes' git pull --ff-only &&
sbatch scripts/run_live_slurm.sbatch
```

If continuing an **already prepared** Nemotron + Smart Turn experiment, use `sbatch scripts/run_live_slurm.sbatch --listening nemotron-smart-turn` instead. This repair does not install the optional listening environment. In an existing three-H200 interactive allocation, restart with `bash scripts/run_live_stack.sh --profile quality-3gpu`, adding the same optional listening flag when applicable.

Use the returned job ID to inspect `slurm-character-JOB_ID.log` and the assigned node. Before model startup, the TTS log must include:

```text
Drama 3 sampling repair: patched; main candidates 4096; acoustic vocabulary 1024; acoustic top-k 30
```

The full log line also prints temperature, top-p and the current v2 source hash from the table above. Reconnect the Mac tunnel to the actual assigned node, refresh the portal and start a fresh call.

The first live check should use a short plain reply and then the same selected voice/emotional delivery. Compare requested words with what is heard and record any safe coverage/finish diagnostics. A passing CPU fixture or successful server startup is not a successful listening test.

## What has been verified

The [sampling regression tests](../tests/test_drama_sampling.py) check exact original/v1/v2 hashes; original and v1 upgrades; preservation of the original backup; idempotence, rollback and interrupted-upgrade recovery; rejection of unknown or imported source; hardlink protection; and unchanged main-token sampling. The residual-draw test makes every invalid ID more probable than every valid ID. Original and v1 select invalid ID 4095; v2 selects valid ID 1023, preserves the 4,096-wide captured shapes and leaves the original logits unchanged.

These checks establish the intended source transformation and candidate bounds without loading a model. They do not measure H200 compilation, acoustic fidelity, emotional consistency, spoken-word completion or conversation latency. No live GPU test was performed for repair v2.

## SGLang as a separate backend

The linked cookbook is a useful alternative runtime. At inspected SGLang-Omni commit `b1097644c09fd3dbe00c26e46d114dbec87f923d`, its installation uses a different Torch/SGLang/FlashInfer stack and manual UCX/Flash Attention prerequisites. Its SSE PCM metadata and request validation also need an adapter. A migration should use a separate environment and retain whole-reply word checks; it should not overwrite the working `.venv-drama3` or be described as a tested Surrey installation.
