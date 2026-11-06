"""CPU import checks only; no checkpoints, CUDA context or inference are created."""
import json
import platform
import sys
from importlib.metadata import distribution, version

OMNI_REVISION = '798fef99a33fba3ad3c2439d6ec10fe838c0076b'
EXPECTED = {'vllm': '0.32.0', 'transformers': '5.16.1',
            'torch': '2.13.0+cu130', 'torchaudio': '2.11.0+cu130',
            'torchvision': '0.28.0+cu130'}


def main():
    if sys.version_info[:2] != (3, 12) or platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise SystemExit('Use the isolated Python 3.12 Linux x86_64 environment on Surrey.')
    actual = {name: version(name) for name in EXPECTED}
    if actual != EXPECTED:
        raise SystemExit('Qwen streaming dependencies differ from the pinned set. Rerun experiments/qwen_stream/setup.sh.')
    direct = json.loads(distribution('vllm-omni').read_text('direct_url.json') or '{}')
    if direct.get('vcs_info', {}).get('commit_id') != OMNI_REVISION:
        raise SystemExit('The Qwen streaming vLLM-Omni source revision does not match the pinned experiment.')
    import torch
    import torchaudio  # noqa: F401: verifies the wheel's native extension.
    import torchvision  # noqa: F401
    if torch.version.cuda != '13.0':
        raise SystemExit('The isolated PyTorch runtime must be CUDA 13.0.')
    from vllm.benchmarks.lib.endpoint_request_func import async_request_profile
    from vllm_omni.entrypoints.cli.main import main as cli_main
    if not callable(cli_main) or not callable(async_request_profile):
        raise SystemExit('Serving imports did not expose the expected entrypoints.')
    print('Pinned Qwen streaming imports OK. No model was loaded.', flush=True)


if __name__ == '__main__':
    main()
