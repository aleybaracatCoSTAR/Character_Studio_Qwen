"""Explicit download of the small text model and CPU ASR checkpoint. No inference."""
import json
import os
from pathlib import Path

MODELS = {
    "llm": ("Qwen/Qwen3-4B-Instruct-2507", "cdbee75f17c01a7cc42f958dc650907174af0554"),
    "stt": ("Systran/faster-whisper-base.en", "3d3d5dee26484f91867d81cb899cfcf72b96be6c"),
}


def main():
    from huggingface_hub import snapshot_download
    store = Path(os.environ.get("LIVE_VOICE_STORE", f"/scratch4weeks/{os.environ.get('USER','YOUR_USER')}/live-voice-store")).expanduser()
    store.mkdir(parents=True, exist_ok=True)
    for directory, (model, revision) in MODELS.items():
        print(f"Downloading {model} to {store / directory}", flush=True)
        snapshot_download(model, revision=revision, local_dir=store/directory, token=False,
                          allow_patterns=["*.json","*.safetensors","*.bin","*.txt","*.model","*.tiktoken","*.jinja","README.md","LICENSE*"])
    (store/"checkpoints.json").write_text(json.dumps(MODELS, indent=2) + "\n")
    print("Text and recognition models downloaded. Drama 3 uses its existing separate store.")


if __name__ == "__main__": main()
