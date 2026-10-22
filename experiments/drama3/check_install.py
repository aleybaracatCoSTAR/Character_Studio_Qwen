"""Check the pinned serving and CUDA wheel combination without loading a model."""
from importlib.metadata import version


def main():
    actual = version("vllm")
    omni = version("vllm-omni")
    print(f"vLLM {actual}; vLLM-Omni {omni}", flush=True)
    if actual.split("+")[0] != "0.32.0" or not omni.startswith("0.32."):
        raise SystemExit("Incompatible serving environment. Run bash experiments/drama3/setup.sh to install the matched pair.")
    expected = {"torch": "2.13.0+cu130", "torchaudio": "2.11.0+cu130", "torchvision": "0.28.0+cu130"}
    installed = {name: version(name) for name in expected}
    print("; ".join(f"{name} {value}" for name, value in installed.items()), flush=True)
    if installed != expected:
        raise SystemExit("CUDA wheel versions do not match the supported cu130 combination. Run bash experiments/drama3/setup.sh; existing model downloads are kept.")
    try:
        import torch
        import torchaudio
        import torchvision
        if torch.version.cuda != "13.0":
            raise RuntimeError(f"Expected PyTorch CUDA 13.0, found {torch.version.cuda}")
        from vllm.benchmarks.lib.endpoint_request_func import async_request_profile
        from vllm_omni.entrypoints.cli.main import main as cli_main
        assert callable(async_request_profile) and callable(cli_main)
    except (ImportError, OSError, RuntimeError) as exc:
        raise SystemExit(f"Serving imports failed: {exc}. Rerun setup.sh and keep the complete installer output.") from exc
    print("Serving imports OK. No model was loaded.", flush=True)


if __name__ == "__main__": main()
