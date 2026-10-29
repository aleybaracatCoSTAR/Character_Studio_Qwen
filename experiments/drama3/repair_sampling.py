"""Inspect or reversibly repair only the pinned Drama 3 sampler; no model loads."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from live_voice.drama_sampling import (
    SamplingRepairError,
    check_live_sampling,
    ensure_live_sampling,
    restore_live_sampling,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Inspect only (the default).")
    mode.add_argument("--apply", action="store_true", help="Apply the verified acoustic-head top-k repair.")
    mode.add_argument("--restore", action="store_true", help="Restore the verified upstream source.")
    args = parser.parse_args(argv)
    action = ensure_live_sampling if args.apply else restore_live_sampling if args.restore else check_live_sampling
    try:
        result = action()
    except (SamplingRepairError, OSError) as exc:
        parser.exit(1, f"Sampling repair stopped: {exc}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
