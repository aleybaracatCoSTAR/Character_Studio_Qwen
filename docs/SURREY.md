# Run on Surrey, step by step

This is a new application and environment. Keep the existing CharacterPlex checkout and store intact. Commands below run on **Surrey**, except the SSH tunnel explicitly marked **Mac**. Use the hostname of your actual allocation; `costar07` is only a previous example.

## 1. Request a GPU from the login terminal

```bash
srun --partition=proto --gres=gpu:nvidia_h200:1 --cpus-per-task=8 --mem=96G --time=04:00:00 --pty bash
hostname
echo "Job: $SLURM_JOB_ID; allocated GPU: $CUDA_VISIBLE_DEVICES"
nvidia-smi
```

Wait until the prompt changes to a compute node. Do not run model inference on `login01`. This recipe is intended for an H200; it has not yet been measured on that GPU.

## 2. Clone the new private repository

The SSH key already added to your GitHub account is named `id_ed25519_github` on Surrey.

```bash
cd "/scratch4weeks/$USER"
GIT_SSH_COMMAND='ssh -i ~/.ssh/id_ed25519_github -o IdentitiesOnly=yes' git clone git@github.com:aleybaracatCoSTAR/Character_Studio_Qwen.git
cd Character_Studio_Qwen
```

Enter the key's passphrase if asked. If this folder already exists, enter it and inspect `git status` before updating it; do not clone over it.

## 3. Set up this project's environment

```bash
export QWEN_STORE="/scratch4weeks/$USER/qwen-character-store"
export QWEN_MODEL_DIR="$QWEN_STORE/model"
export HF_HOME="$QWEN_STORE/huggingface"
export UV_CACHE_DIR="$QWEN_STORE/uv-cache"
export QWEN_PORT=18091
mkdir -p "$QWEN_STORE"
bash scripts/setup.sh gpu
```

This creates `.venv` inside this checkout. It uses the `uv` installation you already set up and installs PyTorch 2.10.0 / torchvision 0.25.0 with CUDA 12.8 wheels, plus Transformers 5.2.0. Your host driver can report a newer CUDA version; the wheels provide their own runtime. See [PyTorch's matching version commands](https://pytorch.org/get-started/previous-versions/#v2-10-0).

The `.env.example` file is a reference only; the app does not automatically read `.env` files. Repeat the exports in a new shell.

## 4. Download once, explicitly

```bash
.venv/bin/python scripts/download_model.py
```

The public checkpoint is about 71 GB. The script checks remaining download space plus a 20 GiB reserve, pins a model revision and resumes existing downloads. It does not access or display your saved Hugging Face token. No API key field is needed in the browser. Keep model files outside Git. Scratch storage is temporary; follow the cluster retention policy and back up character definitions separately.

## 5. Start the studio

```bash
ss -ltn '( sport = :18091 )'
bash scripts/run.sh
```

If the port is already occupied, choose another unused port and use it in both the run command and tunnel. The UI reports loading, ready or an error. Loading the model can take several minutes. The default attention backend is SDPA, avoiding an extra FlashAttention build. No CPU fallback or automatic quantisation is enabled.

## 6. Open a tunnel — on your Mac, in another terminal

Replace `YOUR_COMPUTE_NODE` with the short hostname printed in step 1.

```bash
ssh -N -i ~/.ssh/id_ed25519_costar -o IdentitiesOnly=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -L 127.0.0.1:18091:127.0.0.1:18091 \
  -J jq575gb@costar-login01.surrey.ac.uk \
  jq575gb@YOUR_COMPUTE_NODE.costar-cluster.lan
```

Leave this terminal open. Open **http://127.0.0.1:18091/** on the Mac. Allow microphone access only when you choose to record. Headphones make it easier to hear and avoid recording the reply again. The model still needs a separate check for character consistency.

## 7. First real model check

Start with Anna and typed input. Keep **Spoken reply** selected, then repeat with a recording.

1. Ask: “Who are you, and where are you?” Verify washing machine, wealthy home, utility room.
2. Ask: “Why don't you walk upstairs and ask the family?” Check that she stays consistent with her physical limitations.
3. Ask: “The housekeeper is away. Help me wash a jumper.” Check that she gives one manageable step and does not claim to have loaded it herself.
4. Ask a follow-up referring to your previous answer. Check continuity, playback and the terminal/GPU memory.
5. Try text-only output by clearing **Spoken reply**. Start a new conversation after changing her character details.

Record contradictions and timing before judging this model against CharacterPlex. Prompt instructions do not guarantee consistency.

## 8. Stop and release the GPU

Press **Ctrl+C** in the server terminal to stop the application. That alone does not necessarily end the GPU allocation.

```bash
squeue -u "$USER"
```

Cancel **your Qwen job's actual ID**, then check the queue again:

```bash
scancel YOUR_QWEN_JOB_ID
squeue -u "$USER"
```

Do not reuse an old job ID from earlier examples. Closing the Mac tunnel does not stop the remote model or release the GPU.

## Optional: a batch job after the first interactive test

After dependencies and model files are installed, from the repository root:

```bash
mkdir -p logs
sbatch slurm/qwen-studio.sbatch
squeue -u "$USER"
```

Read `logs/qwen-JOB_ID.log` for the allocated hostname. The job launches the existing environment and model; it does not install dependencies or download weights. Keep the port forwarded to that node. The four-hour limit applies even if a browser is still open.
