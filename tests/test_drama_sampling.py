"""CPU-only checks for the exact, reversible pinned-runtime repair."""
import ast
import builtins
from contextlib import ExitStack, redirect_stdout
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from experiments.drama3 import repair_sampling
from live_voice import drama_sampling as sampling

# Verbatim Apache-2.0 source, copyright vLLM-Omni contributors, from
# vllm-project/vllm-omni commit 798fef99a33fba3ad3c2439d6ec10fe838c0076b:
# vllm_omni/model_executor/models/fish_omni/sampling.py. No runtime imports.
ORIGINAL = b'''# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM-Omni project
"""Drama 3 frame sampling: top-k / top-p heads and repetition-aware semantic draws.

Every op here is a fixed-shape tensor op without host synchronization, so a
decode step that samples a whole frame can be captured in one CUDA graph.
Randomness comes from caller-owned noise (see ``FusedFrameSampler.noise``).
"""

from __future__ import annotations

import torch

from vllm_omni.model_executor.models.fish_omni.kernels import draw_top_k_top_p

# RAS settings are fixed for Drama 3, not request parameters: when a semantic
# first draw repeats a main token of the last 16 frames, the frame takes a
# second draw with these parameters instead.
RAS_WINDOW_FRAMES = 16
RAS_TEMPERATURE = 1.0
RAS_TOP_K = 30
RAS_TOP_P = 0.95
# Pads the window of a request with fewer frames; it matches no token id.
EMPTY_RAS_SLOT = -1


class FusedFrameSampler:
    """Triton draws with top-k capped at ``max_top_k``; one ``topk`` per head.

    The two semantic draws share their candidates. Randomness is one
    ``torch.rand`` per frame, so CUDA graph replays draw fresh noise.
    """

    NUM_DRAWS_EXTRA = 2  # first and RAS semantic draws

    def __init__(self, *, max_top_k: int, num_codebooks: int, max_rows: int, device: torch.device) -> None:
        self.max_top_k = max_top_k
        self.num_draws = num_codebooks - 1 + self.NUM_DRAWS_EXTRA
        self.ras_temperature = torch.full((max_rows,), RAS_TEMPERATURE, dtype=torch.float32, device=device)
        self.ras_top_k = torch.full((max_rows,), RAS_TOP_K, dtype=torch.int64, device=device)
        self.ras_top_p = torch.full((max_rows,), RAS_TOP_P, dtype=torch.float32, device=device)

    def noise(self, rows: int, device: torch.device) -> torch.Tensor:
        return torch.rand((rows, self.num_draws, self.max_top_k), dtype=torch.float32, device=device)

    def main_tokens(
        self,
        candidate_logits: torch.Tensor,
        candidate_ids: torch.Tensor,
        ras_window: torch.Tensor,
        temperature: torch.Tensor,
        top_k: torch.Tensor,
        top_p: torch.Tensor,
        noise: torch.Tensor,
        semantic_begin_id: int,
        codebook_size: int,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        rows = candidate_logits.shape[0]
        top = torch.topk(candidate_logits, self.max_top_k, dim=-1)
        first = draw_top_k_top_p(
            candidate_logits, temperature, top_k, top_p, noise[:, 0], self.max_top_k, candidate_ids, top
        )
        ras = draw_top_k_top_p(
            candidate_logits,
            self.ras_temperature[:rows],
            self.ras_top_k[:rows],
            self.ras_top_p[:rows],
            noise[:, 1],
            self.max_top_k,
            candidate_ids,
            top,
        )
        is_repeat = (ras_window == first.unsqueeze(-1)).any(dim=-1)
        is_semantic = (first >= semantic_begin_id) & (first < semantic_begin_id + codebook_size)
        main_tokens = torch.where(is_repeat & is_semantic, ras, first)
        code_zero = (main_tokens - semantic_begin_id).clamp(0, codebook_size - 1)
        return main_tokens, code_zero

    def code(
        self,
        logits: torch.Tensor,
        temperature: torch.Tensor,
        top_k: torch.Tensor,
        top_p: torch.Tensor,
        noise: torch.Tensor,
        head_index: int,
    ) -> torch.Tensor:
        return draw_top_k_top_p(
            logits, temperature, top_k, top_p, noise[:, self.NUM_DRAWS_EXTRA + head_index], self.max_top_k
        )


__all__ = ["EMPTY_RAS_SLOT", "RAS_TOP_K", "RAS_WINDOW_FRAMES", "FusedFrameSampler"]
'''


class Installation:
    version = "0.32.0rc2.dev59+g798fef99a"

    def __init__(self, root):
        self.root = root
        self.provenance = json.dumps({
            "url": "https://github.com/vllm-project/vllm-omni.git",
            "vcs_info": {"vcs": "git", "commit_id": sampling.PINNED_COMMIT},
        })

    def locate_file(self, path):
        return self.root / path

    def read_text(self, name):
        assert name == "direct_url.json"
        return self.provenance


class SamplingRepairTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(folder)
        self.prefix = self.root / ".venv-drama3"
        self.site = self.prefix / "lib/python3.12/site-packages"
        self.source = self.site / sampling._SAMPLER_PATH
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(ORIGINAL)
        self.source.chmod(0o644)
        self.distribution = Installation(self.site)
        self.stack.enter_context(patch.object(sampling, "_EXPECTED_PREFIX", self.prefix))
        self.stack.enter_context(patch.object(sys, "prefix", str(self.prefix)))
        self.stack.enter_context(patch.object(sampling.metadata, "distribution", return_value=self.distribution))

    def test_constants_and_fixture_match_exact_upstream_hash(self):
        self.assertEqual(sampling.REQUEST_SAMPLING, {"temperature": 1.0, "top_p": 0.9, "top_k": 0})
        self.assertEqual((sampling.MAIN_TOP_K, sampling.RESIDUAL_TOP_K), (4096, 30))
        self.assertEqual(sampling.RESIDUAL_CODEBOOK_SIZE, 1024)
        self.assertEqual(hashlib.sha256(ORIGINAL).hexdigest(), sampling._ORIGINAL_SHA256)
        v1 = ORIGINAL.replace(sampling._ORIGINAL_FRAGMENT, sampling._V1_FRAGMENT)
        self.assertEqual(hashlib.sha256(v1).hexdigest(), sampling._V1_SHA256)
        patched = ORIGINAL.replace(sampling._ORIGINAL_FRAGMENT, sampling._PATCHED_FRAGMENT)
        self.assertEqual(hashlib.sha256(patched).hexdigest(), sampling._PATCHED_SHA256)
        self.assertEqual(ORIGINAL.count(sampling._ORIGINAL_FRAGMENT), 1)

    def test_check_is_read_only_and_distinguishes_original(self):
        before = sorted(str(p.relative_to(self.prefix)) for p in self.prefix.rglob("*"))
        inode = self.source.stat().st_ino
        result = sampling.check_live_sampling()
        self.assertEqual(result["status"], "original")
        self.assertFalse(result["changed"])
        self.assertFalse(result["backup_verified"])
        self.assertFalse(result["marker_verified"])
        self.assertEqual(inode, self.source.stat().st_ino)
        self.assertEqual(before, sorted(str(p.relative_to(self.prefix)) for p in self.prefix.rglob("*")))

    def test_apply_is_exact_atomic_and_idempotent_with_one_verified_backup(self):
        result = sampling.ensure_live_sampling()
        self.assertEqual(result["status"], "patched")
        self.assertTrue(result["changed"])
        self.assertEqual(self.source.read_bytes(), ORIGINAL.replace(sampling._ORIGINAL_FRAGMENT, sampling._PATCHED_FRAGMENT))
        self.assertEqual(self.source.stat().st_mode & 0o777, 0o644)
        backup, marker = sampling._paths(self.source)
        self.assertEqual(backup.read_bytes(), ORIGINAL)
        self.assertEqual(json.loads(marker.read_text()), sampling._marker())
        first_stats = [p.stat().st_mtime_ns for p in (self.source, backup, marker)]
        result = sampling.ensure_live_sampling()
        self.assertFalse(result["changed"])
        self.assertEqual(first_stats, [p.stat().st_mtime_ns for p in (self.source, backup, marker)])
        self.assertEqual(sampling.check_live_sampling()["status"], "patched")
        self.assertFalse(list(self.source.parent.glob(".character-studio-sampling-*")))

    def test_v1_source_and_marker_upgrade_preserve_the_original_backup(self):
        v1 = ORIGINAL.replace(sampling._ORIGINAL_FRAGMENT, sampling._V1_FRAGMENT)
        self.source.write_bytes(v1)
        backup, marker = sampling._paths(self.source)
        backup.write_bytes(ORIGINAL)
        marker.write_text(json.dumps(sampling._v1_marker()))
        before = backup.stat().st_mtime_ns
        self.assertEqual(sampling.check_live_sampling()['status'], 'patched-v1')
        result = sampling.ensure_live_sampling()
        self.assertTrue(result['changed'])
        self.assertEqual(result['repair_version'], 2)
        self.assertEqual(backup.read_bytes(), ORIGINAL)
        self.assertEqual(backup.stat().st_mtime_ns, before)
        self.assertEqual(json.loads(marker.read_text()), sampling._marker())
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), sampling._PATCHED_SHA256)
        self.assertFalse(sampling.ensure_live_sampling()['changed'])
        sampling.restore_live_sampling()
        self.assertEqual(self.source.read_bytes(), ORIGINAL)

    def test_v1_without_sidecars_can_reconstruct_only_the_verified_original(self):
        self.source.write_bytes(ORIGINAL.replace(sampling._ORIGINAL_FRAGMENT, sampling._V1_FRAGMENT))
        self.assertTrue(sampling.ensure_live_sampling()['changed'])
        backup, marker = sampling._paths(self.source)
        self.assertEqual(backup.read_bytes(), ORIGINAL)
        self.assertEqual(json.loads(marker.read_text()), sampling._marker())

    def test_v1_restore_works_without_applying_v2_first(self):
        self.source.write_bytes(ORIGINAL.replace(sampling._ORIGINAL_FRAGMENT, sampling._V1_FRAGMENT))
        backup, marker = sampling._paths(self.source)
        backup.write_bytes(ORIGINAL)
        marker.write_text(json.dumps(sampling._v1_marker()))
        self.assertTrue(sampling.restore_live_sampling()['changed'])
        self.assertEqual(self.source.read_bytes(), ORIGINAL)

    def test_failed_v1_upgrade_can_resume_without_overwriting_its_backup(self):
        v1 = ORIGINAL.replace(sampling._ORIGINAL_FRAGMENT, sampling._V1_FRAGMENT)
        self.source.write_bytes(v1)
        backup, marker = sampling._paths(self.source)
        backup.write_bytes(ORIGINAL)
        marker.write_text(json.dumps(sampling._v1_marker()))
        real_replace = sampling.os.replace
        def replace(src, dst):
            if Path(dst).resolve() == self.source.resolve(): raise OSError('source replacement unavailable')
            return real_replace(src, dst)
        with patch.object(sampling.os, 'replace', side_effect=replace):
            with self.assertRaises(OSError): sampling.ensure_live_sampling()
        self.assertEqual(self.source.read_bytes(), v1)
        self.assertEqual(backup.read_bytes(), ORIGINAL)
        # A metadata upgrade that completed before source replacement is safe to resume.
        self.assertEqual(json.loads(marker.read_text()), sampling._marker())
        self.assertEqual(sampling.check_live_sampling()['status'], 'patched-v1')
        self.assertTrue(sampling.ensure_live_sampling()['changed'])

    def test_v1_hardlinked_cache_and_imported_module_are_protected_during_upgrade(self):
        v1 = ORIGINAL.replace(sampling._ORIGINAL_FRAGMENT, sampling._V1_FRAGMENT)
        self.source.write_bytes(v1)
        cache = self.root / 'uv-cache-v1'
        os.link(self.source, cache)
        with patch.dict(sys.modules, {sampling._MODULE: object()}):
            with self.assertRaisesRegex(sampling.SamplingRepairError, 'already imported'):
                sampling.ensure_live_sampling()
        self.assertEqual(self.source.read_bytes(), v1)
        self.assertFalse(any(p.exists() for p in sampling._paths(self.source)))
        self.assertTrue(sampling.ensure_live_sampling()['changed'])
        self.assertEqual(cache.read_bytes(), v1)
        self.assertNotEqual(cache.stat().st_ino, self.source.stat().st_ino)

    def test_restore_uses_verified_backup_and_can_be_applied_again(self):
        sampling.ensure_live_sampling()
        result = sampling.restore_live_sampling()
        self.assertEqual(result["status"], "original")
        self.assertTrue(result["changed"])
        self.assertEqual(self.source.read_bytes(), ORIGINAL)
        self.assertFalse(sampling.restore_live_sampling()["changed"])
        self.assertTrue(sampling.ensure_live_sampling()["changed"])

    def test_interrupted_atomic_replace_keeps_original_and_can_resume(self):
        with patch.object(sampling.os, "replace", side_effect=OSError("replacement unavailable")):
            with self.assertRaises(OSError):
                sampling.ensure_live_sampling()
        self.assertEqual(self.source.read_bytes(), ORIGINAL)
        self.assertFalse(list(self.source.parent.glob(".character-studio-sampling-*")))
        self.assertTrue(sampling.check_live_sampling()["backup_verified"])
        self.assertTrue(sampling.check_live_sampling()["marker_verified"])
        self.assertTrue(sampling.ensure_live_sampling()["changed"])

    def test_replacing_uv_hardlink_does_not_modify_cache(self):
        cache = self.root / "uv-cache-original"
        self.source.rename(cache)
        os.link(cache, self.source)
        original_inode = cache.stat().st_ino
        self.assertEqual(original_inode, self.source.stat().st_ino)
        sampling.ensure_live_sampling()
        self.assertEqual(cache.read_bytes(), ORIGINAL)
        self.assertEqual(cache.stat().st_ino, original_inode)
        self.assertNotEqual(cache.stat().st_ino, self.source.stat().st_ino)
        sampling.restore_live_sampling()
        self.assertEqual(cache.read_bytes(), ORIGINAL)
        self.assertEqual(self.source.read_bytes(), ORIGINAL)

    def test_unknown_source_and_custom_patch_are_never_overwritten(self):
        self.source.write_bytes(ORIGINAL + b"# custom\n")
        for action in (sampling.check_live_sampling, sampling.ensure_live_sampling, sampling.restore_live_sampling):
            with self.subTest(action=action.__name__), self.assertRaisesRegex(sampling.SamplingRepairError, "source differs"):
                action()
        self.assertEqual(self.source.read_bytes(), ORIGINAL + b"# custom\n")
        self.assertFalse(any(p.exists() for p in sampling._paths(self.source)))

    def test_invalid_backup_or_marker_blocks_apply_and_restore(self):
        sampling.ensure_live_sampling()
        patched = self.source.read_bytes()
        backup, marker = sampling._paths(self.source)
        for path, bad in ((backup, b"other source"), (marker, b'{"schema":2}')):
            good = path.read_bytes()
            path.write_bytes(bad)
            for action in (sampling.check_live_sampling, sampling.ensure_live_sampling, sampling.restore_live_sampling):
                with self.subTest(path=path.name, action=action.__name__), self.assertRaises(sampling.SamplingRepairError):
                    action()
            self.assertEqual(self.source.read_bytes(), patched)
            path.write_bytes(good)

    def test_wrong_commit_or_editable_provenance_cannot_use_version_fallback(self):
        for info in (
            {"url": "https://github.com/vllm-project/vllm-omni.git", "vcs_info": {"vcs": "git", "commit_id": "0" * 40}},
            {"url": "https://github.com/other/fork.git", "vcs_info": {"vcs": "git", "commit_id": sampling.PINNED_COMMIT}},
            {"url": "https://github.com/vllm-project/vllm-omni.git", "vcs_info": {"vcs": "git", "commit_id": sampling.PINNED_COMMIT}, "dir_info": {"editable": True}},
            [],
        ):
            self.distribution.provenance = json.dumps(info)
            with self.subTest(info=info), self.assertRaises(sampling.SamplingRepairError):
                sampling.ensure_live_sampling()
        self.assertEqual(self.source.read_bytes(), ORIGINAL)

    def test_missing_provenance_requires_pinned_version_and_exact_source(self):
        self.distribution.provenance = None
        self.assertTrue(sampling.ensure_live_sampling()["changed"])
        sampling.restore_live_sampling()
        self.distribution.version = "0.32.0"
        with self.assertRaisesRegex(sampling.SamplingRepairError, "version"):
            sampling.ensure_live_sampling()
        self.distribution.version = Installation.version
        self.source.write_bytes(b"custom")
        with self.assertRaisesRegex(sampling.SamplingRepairError, "source differs"):
            sampling.ensure_live_sampling()

    def test_wrong_interpreter_outside_install_and_symlink_are_rejected(self):
        with patch.object(sys, "prefix", str(self.root)):
            with self.assertRaisesRegex(sampling.SamplingRepairError, "checkout"):
                sampling.ensure_live_sampling()
        self.distribution.root = self.root
        with self.assertRaises(sampling.SamplingRepairError):
            sampling.ensure_live_sampling()
        self.distribution.root = self.site
        external = self.root / "external.py"
        external.write_bytes(ORIGINAL)
        self.source.unlink()
        self.source.symlink_to(external)
        with self.assertRaisesRegex(sampling.SamplingRepairError, "inside"):
            sampling.ensure_live_sampling()
        self.assertEqual(external.read_bytes(), ORIGINAL)

    def test_symlink_sidecars_and_already_imported_sampler_are_rejected(self):
        backup, _ = sampling._paths(self.source)
        external = self.root / "external-original"
        external.write_bytes(ORIGINAL)
        backup.symlink_to(external)
        with self.assertRaisesRegex(sampling.SamplingRepairError, "symbolic"):
            sampling.ensure_live_sampling()
        backup.unlink()
        with patch.dict(sys.modules, {sampling._MODULE: object()}):
            with self.assertRaisesRegex(sampling.SamplingRepairError, "already imported"):
                sampling.ensure_live_sampling()
        self.assertEqual(self.source.read_bytes(), ORIGINAL)

    def test_known_patch_can_rebuild_missing_owned_sidecars(self):
        patched = ORIGINAL.replace(sampling._ORIGINAL_FRAGMENT, sampling._PATCHED_FRAGMENT)
        self.source.write_bytes(patched)
        self.assertFalse(sampling.ensure_live_sampling()["changed"])
        backup, marker = sampling._paths(self.source)
        self.assertEqual(backup.read_bytes(), ORIGINAL)
        self.assertTrue(marker.exists())
        self.assertTrue(sampling.restore_live_sampling()["changed"])

    def test_restore_requires_backup_and_marker(self):
        self.source.write_bytes(ORIGINAL.replace(sampling._ORIGINAL_FRAGMENT, sampling._PATCHED_FRAGMENT))
        with self.assertRaisesRegex(sampling.SamplingRepairError, "backup and marker"):
            sampling.restore_live_sampling()

    def test_cli_defaults_to_check_and_requires_explicit_mutation_flags(self):
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(repair_sampling.main([]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "original")
        self.assertEqual(self.source.read_bytes(), ORIGINAL)
        with redirect_stdout(StringIO()):
            repair_sampling.main(["--apply"])
            self.assertEqual(sampling.check_live_sampling()["status"], "patched")
            repair_sampling.main(["--restore"])
        self.assertEqual(self.source.read_bytes(), ORIGINAL)

    def test_actual_patched_methods_leave_main_unrestricted_and_cap_acoustic_heads(self):
        sampling.ensure_live_sampling()
        tree = ast.parse(self.source.read_text())
        original_tree = ast.parse(ORIGINAL.decode())
        def method(parsed, name):
            cls = next(n for n in parsed.body if isinstance(n, ast.ClassDef) and n.name == "FusedFrameSampler")
            return next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
        self.assertEqual(ast.dump(method(tree, "main_tokens")), ast.dump(method(original_tree, "main_tokens")))
        methods = [method(tree, "main_tokens"), method(tree, "code")]
        selected = ast.Module(body=[ast.ClassDef(name="Sampler", bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[])
        calls = []
        class Number(int):
            def clamp(self, min=None, max=None):
                value = int(self)
                if min is not None: value = builtins.max(min, value)
                if max is not None: value = builtins.min(max, value)
                return Number(value)
            def unsqueeze(self, _): return self
            def __sub__(self, other): return Number(int(self) - other)
        class Window:
            def __eq__(self, other): return self
            def any(self, dim): return False
        class Noise:
            def __getitem__(self, key): return key
        class Logits:
            def clone(self): return self
            def __setitem__(self, key, value):
                self.mask = (key, value)
        def draw(*args):
            calls.append(args)
            return Number(117)
        torch = SimpleNamespace(Tensor=object, topk=lambda logits, cap, dim: ("values", "ids"),
                                where=lambda cond, a, b: a if cond else b)
        scope = {"torch": torch, "draw_top_k_top_p": draw}
        exec(compile(ast.fix_missing_locations(selected), "<verified-sampling-methods>", "exec"), scope)
        instance = scope["Sampler"]()
        instance.max_top_k = 4096
        instance.NUM_DRAWS_EXTRA = 2
        instance.ras_temperature = [1.0]
        instance.ras_top_k = [30]
        instance.ras_top_p = [.95]
        top_k = Number(4096)
        instance.main_tokens(SimpleNamespace(shape=(1, 4096)), object(), Window(), 1.0, top_k, .9, Noise(), 100, 4096)
        logits = Logits()
        instance.code(logits, 1.0, top_k, .9, Noise(), 0)
        self.assertEqual(calls[0][2], 4096)
        self.assertIs(calls[0][2], top_k)
        self.assertEqual(calls[0][5], 4096)
        self.assertEqual(calls[-1][2], 30)
        self.assertEqual(calls[-1][5], 4096)
        self.assertEqual(logits.mask, ((Ellipsis, slice(1024,None)), float('-inf')))

    def test_actual_residual_draw_masks_invalid_ids_without_shrinking_captured_shapes(self):
        """Execute the pinned methods with adversarial logits and a CPU tensor spy.

        vLLM's 4096-wide head and candidate/noise capacity stay intact. The codec
        accepts only IDs 0–1023, so raising every later logit must not let one
        win. No Torch import or model is involved.
        """
        sampling.ensure_live_sampling()
        def code_method(source):
            cls = next(n for n in ast.parse(source).body if isinstance(n,ast.ClassDef))
            fn = next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='code')
            tree = ast.Module(body=[ast.ClassDef(name='Sampler',bases=[],keywords=[],
                body=[fn],decorator_list=[])],type_ignores=[])
            return compile(ast.fix_missing_locations(tree),'<pinned-residual-draw>','exec')
        class Tensor:
            def __init__(self, values):
                self.values = list(values)
                self.shape = (1,len(self.values))
            def clone(self): return Tensor(self.values)
            def __setitem__(self, key, value):
                self.assert_key = key
                for i in range(*key[1].indices(len(self.values))): self.values[i] = value
        class TopK(int):
            def clamp(self, *, max): return TopK(min(self,max))
        class Noise:
            def __getitem__(self, key):
                assert key == (slice(None),2)
                return SimpleNamespace(shape=(1,4096))
        draws = []
        def draw(logits,temperature,top_k,top_p,noise,capacity):
            self.assertEqual(logits.shape, (1,4096))
            self.assertEqual(noise.shape, (1,4096))
            self.assertEqual(capacity, 4096)
            draws.append((logits,int(top_k)))
            return max(range(4096),key=logits.values.__getitem__)
        # Every invalid code has a larger logit than every legal code.
        logits = Tensor(range(4096))
        results = []
        for source in (ORIGINAL.decode(),
                       ORIGINAL.replace(sampling._ORIGINAL_FRAGMENT,sampling._V1_FRAGMENT).decode(),
                       self.source.read_text()):
            scope = {'torch':SimpleNamespace(Tensor=object),'draw_top_k_top_p':draw}
            exec(code_method(source),scope)
            sampler = scope['Sampler'](); sampler.max_top_k=4096; sampler.NUM_DRAWS_EXTRA=2
            results.append(sampler.code(logits,1.0,TopK(4096),.9,Noise(),0))
        self.assertEqual(results, [4095,4095,1023])
        masked,top_k = draws[-1]
        self.assertEqual(top_k,30)
        self.assertEqual(masked.values[:1024],list(range(1024)))
        self.assertTrue(all(value==float('-inf') for value in masked.values[1024:]))
        self.assertEqual(logits.values,list(range(4096)))


if __name__ == "__main__":
    unittest.main()
