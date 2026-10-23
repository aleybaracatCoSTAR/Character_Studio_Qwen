"""Start isolated inference process groups and stop only the groups owned by this launch."""
import argparse
import os
import signal
import time


def group_exists(group):
    try:
        os.killpg(group, 0)
        return True
    except ProcessLookupError:
        return False


def stop_groups(groups, timeout=20):
    groups = set(groups)
    # Never signal the calling shell's group, PID 1 or an invalid identifier.
    if any(group <= 1 or group == os.getpgrp() for group in groups):
        raise ValueError("Refusing to stop a shared or invalid process group.")
    for group in groups:
        try: os.killpg(group, signal.SIGTERM)
        except ProcessLookupError: pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        groups = {group for group in groups if group_exists(group)}
        if not groups: return
        time.sleep(0.1)
    for group in groups:
        print(f"Inference group {group} did not stop within {timeout:g}s; stopping its remaining workers.", flush=True)
        try: os.killpg(group, signal.SIGKILL)
        except ProcessLookupError: pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    start = commands.add_parser("start")
    start.add_argument("command", nargs=argparse.REMAINDER)
    stop = commands.add_parser("stop")
    stop.add_argument("groups", nargs="*", type=int)
    stop.add_argument("--timeout", type=float, default=20)
    args = parser.parse_args()
    if args.action == "start":
        if not args.command: parser.error("Provide the inference command to start.")
        # The launcher backgrounds this process, so its PID is not the shell's process group.
        os.setsid()
        os.execvp(args.command[0], args.command)
    else:
        stop_groups(args.groups, timeout=args.timeout)


if __name__ == "__main__": main()
