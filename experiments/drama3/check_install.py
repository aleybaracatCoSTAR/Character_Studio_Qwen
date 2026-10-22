"""Fail before serving when the two GPU runtime packages cannot import together."""
from importlib.metadata import version


def main():
    actual = version("vllm")
    omni = version("vllm-omni")
    print(f"vLLM {actual}; vLLM-Omni {omni}", flush=True)
    if actual.split("+")[0] != "0.32.0" or not omni.startswith("0.32."):
        raise SystemExit("Incompatible serving environment. Run bash experiments/drama3/setup.sh to install the matched pair.")
    try:
        from vllm.benchmarks.lib.endpoint_request_func import async_request_profile
        from vllm_omni.entrypoints.cli.main import main as cli_main
        assert callable(async_request_profile) and callable(cli_main)
    except ImportError as exc:
        raise SystemExit(f"Serving imports failed: {exc}. Rerun setup.sh and keep the complete installer output.") from exc
    print("Serving imports OK. No model was loaded.", flush=True)


if __name__ == "__main__": main()
