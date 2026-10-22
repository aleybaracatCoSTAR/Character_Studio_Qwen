"""Explicit, resumable checkpoint download. Never invoked by the app or preview."""
import argparse
import json
import os
from pathlib import Path
import shutil

MODEL_ID = "Qwen/Qwen3-Omni-30B-A3B-Instruct"
REVISION = "26291f793822fb6be9555850f06dfe95f2d7e695"
PATTERNS = ["*.json", "*.safetensors", "*.txt", "*.tiktoken", "*.model", "README.md", "LICENSE*"]


def main():
    parser = argparse.ArgumentParser(description="Download the pinned Qwen checkpoint (~71 GB). Run on the GPU host.")
    parser.add_argument("--directory", default=os.environ.get("QWEN_MODEL_DIR"))
    args = parser.parse_args()
    if not args.directory:
        parser.error("Set QWEN_MODEL_DIR to a scratch directory outside the repository.")
    import torch
    if not torch.cuda.is_available():
        parser.error("No CUDA GPU is visible. Use a GPU allocation for this setup.")
    from fnmatch import fnmatch
    from huggingface_hub import HfApi, snapshot_download
    target = Path(args.directory).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    info = HfApi(token=False).model_info(MODEL_ID, revision=REVISION, files_metadata=True)
    needed = 0
    for item in info.siblings:
        if any(fnmatch(item.rfilename, pattern) for pattern in PATTERNS):
            local = target / item.rfilename
            if not local.is_file() or local.stat().st_size != item.size:
                needed += item.size or 0
    free = shutil.disk_usage(target).free
    reserve = 20 * 1024**3
    if free < needed + reserve:
        parser.error(f"Need {needed / 1024**3:.1f} GiB of downloads plus 20 GiB free reserve; only {free / 1024**3:.1f} GiB available.")
    print(f"Checkpoint: {MODEL_ID}@{REVISION}\nDestination: {target}\nRemaining download: {needed / 1024**3:.1f} GiB")
    snapshot_download(MODEL_ID, revision=REVISION, local_dir=target, allow_patterns=PATTERNS, max_workers=4, token=False)
    (target / "studio-checkpoint.json").write_text(json.dumps({"model": MODEL_ID, "revision": REVISION}, indent=2) + "\n")
    print("Download complete. Next: bash scripts/run.sh")


if __name__ == "__main__":
    main()
