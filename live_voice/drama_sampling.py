"""Reversible, byte-guarded fix for the pinned Drama 3 acoustic-head sampler.

The upstream sampler is Apache-2.0, copyright vLLM-Omni contributors:
https://github.com/vllm-project/vllm-omni/blob/798fef99a33fba3ad3c2439d6ec10fe838c0076b/vllm_omni/model_executor/models/fish_omni/sampling.py

Only acoustic-head top-k is changed here. The live deployment separately gives
the main sampler capacity for all 4096 candidates. This is not SGLang reference
sampling and does not alter top-p, repetition handling, or the CUDA packages.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import stat
import sys
import tempfile


REQUEST_SAMPLING = {"temperature": 1.0, "top_p": 0.9, "top_k": 0}
MAIN_TOP_K = 4096
RESIDUAL_TOP_K = 30
PINNED_COMMIT = "798fef99a33fba3ad3c2439d6ec10fe838c0076b"
_PINNED_VERSION = "0.32.0rc2.dev59+g798fef99a"
_EXPECTED_PREFIX = Path(__file__).resolve().parents[1] / ".venv-drama3"
_SAMPLER_PATH = "vllm_omni/model_executor/models/fish_omni/sampling.py"
_MODULE = "vllm_omni.model_executor.models.fish_omni.sampling"
_ORIGINAL_SHA256 = "33db22e1338d4214a1e0ed0be919cdfba6b1865c095d0a17fa54dc222e332e03"
_PATCHED_SHA256 = "038b2c5eea5a8a8ed48c435429c0a7b3bb2e2aed0cd265e7a783f7aad829c4d7"
_ORIGINAL_FRAGMENT = b"logits, temperature, top_k, top_p, noise[:, self.NUM_DRAWS_EXTRA + head_index], self.max_top_k"
_PATCHED_FRAGMENT = b"logits, temperature, top_k.clamp(max=30), top_p, noise[:, self.NUM_DRAWS_EXTRA + head_index], self.max_top_k"


class SamplingRepairError(RuntimeError):
    """The installed runtime cannot safely receive this narrowly scoped repair."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _inside(path: Path, prefix: Path) -> bool:
    return path.is_relative_to(prefix)


def _installation() -> tuple[Path, Path, str]:
    expected = _EXPECTED_PREFIX
    prefix = Path(sys.prefix).resolve()
    if expected.is_symlink() or prefix != expected.resolve() or not prefix.is_dir():
        raise SamplingRepairError("Use this checkout's .venv-drama3/bin/python for the sampling repair.")
    try:
        distribution = metadata.distribution("vllm-omni")
    except metadata.PackageNotFoundError as exc:
        raise SamplingRepairError("The pinned vLLM-Omni installation is missing from .venv-drama3.") from exc
    try:
        package_root = Path(distribution.locate_file("")).resolve(strict=True)
        source = Path(distribution.locate_file(_SAMPLER_PATH)).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SamplingRepairError("The pinned Drama 3 sampler file is missing.") from exc
    if not _inside(package_root, prefix) or not _inside(source, prefix) or not source.is_file():
        raise SamplingRepairError("The Drama 3 sampler must be installed inside this checkout's .venv-drama3.")
    direct_url = distribution.read_text("direct_url.json")
    if direct_url is not None:
        try:
            info = json.loads(direct_url)
            vcs = info.get("vcs_info", {})
            valid = (
                isinstance(info, dict)
                and not info.get("dir_info", {}).get("editable", False)
                and vcs.get("vcs") == "git"
                and vcs.get("commit_id") == PINNED_COMMIT
                and info.get("url", "").removesuffix(".git")
                == "https://github.com/vllm-project/vllm-omni"
            )
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise SamplingRepairError("vLLM-Omni provenance does not match the pinned, non-editable upstream commit.")
    elif not distribution.version.startswith(_PINNED_VERSION):
        raise SamplingRepairError("vLLM-Omni version does not identify the supported pinned commit.")
    return prefix, source, distribution.version


def _paths(source: Path) -> tuple[Path, Path]:
    return (
        source.with_name(source.name + ".character-studio-original"),
        source.with_name(source.name + ".character-studio-sampling.json"),
    )


def _marker() -> dict:
    return {
        "schema": 1,
        "upstream_commit": PINNED_COMMIT,
        "original_sha256": _ORIGINAL_SHA256,
        "patched_sha256": _PATCHED_SHA256,
        "residual_top_k": RESIDUAL_TOP_K,
    }


def _read_state(source: Path) -> tuple[str, bytes]:
    data = source.read_bytes()
    digest = _sha256(data)
    if digest == _ORIGINAL_SHA256:
        return "original", data
    if digest == _PATCHED_SHA256:
        return "patched", data
    raise SamplingRepairError("Drama 3 sampler source differs from both verified versions; no files were changed.")


def _verify_sidecars(source: Path) -> tuple[bool, bool]:
    backup, marker = _paths(source)
    for path in (backup, marker):
        if path.is_symlink():
            raise SamplingRepairError("A sampling repair sidecar is a symbolic link; no files were changed.")
    has_backup = backup.exists()
    has_marker = marker.exists()
    if has_backup and (not backup.is_file() or _sha256(backup.read_bytes()) != _ORIGINAL_SHA256):
        raise SamplingRepairError("The existing sampling backup is not the verified original; no files were changed.")
    if has_marker:
        try:
            valid = marker.is_file() and json.loads(marker.read_text()) == _marker()
        except (OSError, ValueError):
            valid = False
        if not valid:
            raise SamplingRepairError("The existing sampling repair marker is unrecognized; no files were changed.")
    return has_backup, has_marker


def _result(status: str, data: bytes, version: str, changed: bool = False) -> dict:
    return {"status": status, "changed": changed, "source_sha256": _sha256(data), "upstream_commit": PINNED_COMMIT, "version": version}


def check_live_sampling() -> dict:
    """Inspect only; never create a lock, backup, marker, or modified source."""
    _, source, version = _installation()
    status, data = _read_state(source)
    has_backup, has_marker = _verify_sidecars(source)
    result = _result(status, data, version)
    result["backup_verified"] = has_backup
    result["marker_verified"] = has_marker
    return result


@contextmanager
def _repair_lock(prefix: Path):
    import fcntl

    path = prefix / ".character-studio-drama-sampling.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _write_atomic(path: Path, data: bytes, mode: int, *, replace: bool) -> None:
    """Replace directory entries, never modify uv cache hardlinks in place."""
    fd, temporary = tempfile.mkstemp(prefix=".character-studio-sampling-", dir=path.parent)
    tmp = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
        if replace:
            os.replace(tmp, path)
        else:
            # An unrelated pre-existing backup/marker is never overwritten.
            os.link(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def ensure_live_sampling() -> dict:
    """Apply the exact repair before importing the runtime, or verify it is present."""
    prefix, source, version = _installation()
    with _repair_lock(prefix):
        status, data = _read_state(source)
        has_backup, has_marker = _verify_sidecars(source)
        original = data if status == "original" else data.replace(_PATCHED_FRAGMENT, _ORIGINAL_FRAGMENT)
        if _sha256(original) != _ORIGINAL_SHA256 or original.count(_ORIGINAL_FRAGMENT) != 1:
            raise SamplingRepairError("The verified sampling repair cannot be reconstructed safely.")
        patched = original.replace(_ORIGINAL_FRAGMENT, _PATCHED_FRAGMENT)
        if _sha256(patched) != _PATCHED_SHA256:
            raise SamplingRepairError("The sampling repair hash does not match the verified patch.")
        if status == "original" and _MODULE in sys.modules:
            raise SamplingRepairError("Restart before applying the sampling repair; its module is already imported.")
        backup, marker = _paths(source)
        if not has_backup:
            _write_atomic(backup, original, 0o600, replace=False)
        if not has_marker:
            _write_atomic(marker, (json.dumps(_marker(), sort_keys=True) + "\n").encode(), 0o600, replace=False)
        if status == "original":
            _write_atomic(source, patched, stat.S_IMODE(source.stat().st_mode), replace=True)
        if _sha256(source.read_bytes()) != _PATCHED_SHA256:
            raise SamplingRepairError("Sampling repair verification failed after replacement.")
        return _result("patched", patched, version, changed=status == "original")


def restore_live_sampling() -> dict:
    """Restore only a recognized patched file from its verified original backup."""
    prefix, source, version = _installation()
    with _repair_lock(prefix):
        status, data = _read_state(source)
        has_backup, has_marker = _verify_sidecars(source)
        if status == "original":
            return _result(status, data, version)
        if not has_backup or not has_marker:
            raise SamplingRepairError("A verified sampling backup and marker are required to restore the original.")
        if _MODULE in sys.modules:
            raise SamplingRepairError("Restart before restoring the sampler; its module is already imported.")
        backup, _ = _paths(source)
        original = backup.read_bytes()
        _write_atomic(source, original, stat.S_IMODE(source.stat().st_mode), replace=True)
        return _result("original", original, version, changed=True)
