import argparse
import os

from .app import create_app
from .engine import QwenEngine


def main():
    parser = argparse.ArgumentParser(description="Character Studio Qwen; localhost only.")
    parser.add_argument("--preview", action="store_true", help="Show the UI without loading or downloading a model.")
    parser.add_argument("--model-dir", default=os.environ.get("QWEN_MODEL_DIR"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("QWEN_PORT", "18091")))
    parser.add_argument("--attention", choices=["sdpa", "flash_attention_2"], default=os.environ.get("QWEN_ATTENTION", "sdpa"))
    args = parser.parse_args()
    if not args.preview and not args.model_dir:
        parser.error("Set QWEN_MODEL_DIR on the GPU server, or use --preview on your laptop.")
    import uvicorn
    app = create_app(QwenEngine(args.preview, args.model_dir, args.attention))
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
