"""CPU-only runtime contracts; never install, download or invoke inference."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from experiments.qwen_stream import runtime, download

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = ROOT / 'experiments/qwen_stream'


def checkpoint_fixture(base):
    location = {'live': base / 'live', 'model': base / 'old-qwen/model', 'experiment': base / 'experiment'}
    model = location['model']
    model.mkdir(parents=True)
    runtime.write_json(model / 'studio-checkpoint.json', {'model': runtime.MODEL_ID, 'revision': runtime.REVISION})
    runtime.write_json(model / 'config.json', {'model_type': 'qwen3_omni_moe', 'enable_audio_output': True})
    for name in ('preprocessor_config.json', 'tokenizer_config.json', 'chat_template.json', 'vocab.json', 'merges.txt'):
        (model / name).write_text('{}')
    weights = {}
    for index in range(1, 16):
        name = f'model-{index:05}-of-00015.safetensors'
        (model / name).write_bytes(b'fixture')
        weights[f'layer{index}'] = name
    runtime.write_json(model / 'model.safetensors.index.json', {'weight_map': weights})
    director = runtime.profile('quality-3gpu')['director']
    target = location['live'] / director['directory']
    target.mkdir(parents=True)
    runtime.write_json(target / 'config.json', {})
    (target / 'model.safetensors').write_bytes(b'fixture')
    runtime.write_json(target / 'model.safetensors.index.json', {'weight_map': {'weight': 'model.safetensors'}})
    stt = location['live'] / 'stt'
    stt.mkdir()
    runtime.write_json(stt / 'config.json', {})
    (stt / 'model.bin').write_bytes(b'fixture')
    runtime.write_json(location['live'] / 'checkpoints-quality-3gpu.json', {
        director['directory']: [director['id'], director['revision']], 'stt': list(runtime.STT)})
    return location


class RuntimeTests(unittest.TestCase):
    def test_actual_allocated_tokens_and_reject_unsafe_allocation(self):
        self.assertEqual(runtime.allocation('GPU-first,GPU-second,GPU-third'),
                         {'omni': 'GPU-first,GPU-second', 'director': 'GPU-third'})
        self.assertEqual(runtime.allocation('2,5,7')['director'], '7')
        for value in ('', '0', '0,1', '0,0,2', '0,1,2,3', '0,,2', '0,1,device x'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                runtime.allocation(value)

    def test_reuses_original_checkpoint_and_baseline_dependencies_without_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            location = checkpoint_fixture(Path(temporary))
            with patch('builtins.__import__', wraps=__import__) as imports:
                runtime.validate_store(location)
            self.assertNotIn('torch', [call.args[0] for call in imports.call_args_list])
            calls = []
            download.prepare(location, snapshot_download=lambda *a, **k: calls.append((a, k)))
            self.assertEqual(calls, [])
            self.assertEqual(location['model'].name, 'model')
            self.assertTrue((location['experiment'] / 'checkpoints.json').is_file())

    def test_bad_revision_missing_shard_and_unsafe_index_rejected_offline(self):
        with tempfile.TemporaryDirectory() as temporary:
            location = checkpoint_fixture(Path(temporary))
            model = location['model']
            runtime.write_json(model / 'studio-checkpoint.json', {'model': runtime.MODEL_ID, 'revision': 'wrong'})
            with self.assertRaisesRegex(ValueError, 'marker'):
                runtime.validate_model(model)
            runtime.write_json(model / 'studio-checkpoint.json', {'model': runtime.MODEL_ID, 'revision': runtime.REVISION})
            shard = model / 'model-00015-of-00015.safetensors'
            shard.unlink()
            with self.assertRaisesRegex(ValueError, 'missing or empty'):
                runtime.validate_model(model)
            shard.write_bytes(b'fixture')
            index = json.loads((model / 'model.safetensors.index.json').read_text())
            index['weight_map']['layer15'] = '../private.safetensors'
            runtime.write_json(model / 'model.safetensors.index.json', index)
            with self.assertRaisesRegex(ValueError, 'unsafe filename'):
                runtime.validate_model(model)

    def test_download_is_explicit_pinned_and_skips_unneeded_baseline_actor(self):
        with tempfile.TemporaryDirectory() as temporary:
            location = checkpoint_fixture(Path(temporary))
            marker = location['model'] / 'studio-checkpoint.json'
            marker.unlink()
            calls = []
            def fake_snapshot(model, **kwargs):
                calls.append((model, kwargs))
            download.prepare(location, snapshot_download=fake_snapshot)
            self.assertEqual(len(calls), 1)
            model, kwargs = calls[0]
            self.assertEqual(model, runtime.MODEL_ID)
            self.assertEqual(kwargs['revision'], runtime.REVISION)
            self.assertEqual(kwargs['local_dir'], location['model'])
            self.assertFalse(kwargs['token'])
            self.assertNotIn('actor-30b', str(calls))

    def test_model_directory_and_scratch_overrides_preserved(self):
        location = runtime.paths({'USER': 'example', 'LIVE_VOICE_STORE': '/tmp/voice store',
                                  'QWEN_MODEL_DIR': '/tmp/old model', 'QWEN_STREAM_STORE': '/tmp/stream logs'})
        self.assertEqual(str(location['model']), '/private/tmp/old model' if Path('/tmp').resolve() == Path('/private/tmp') else '/tmp/old model')
        self.assertEqual(location['live'], Path('/tmp/voice store').resolve())
        self.assertEqual(location['experiment'], Path('/tmp/stream logs').resolve())

    def test_overlay_inherits_upstream_contract_without_overriding_sampling(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary) / 'installed template.yaml'
            base.write_text('async_chunk: true\n')
            output = runtime.render_deploy(Path(temporary) / 'generated/config.json', base=base)
            config = json.loads(output.read_text())
            self.assertEqual(config['base_config'], str(base.resolve()))
            self.assertTrue(config['async_chunk'])
            self.assertEqual([stage['devices'] for stage in config['stages']], ['0', '1', '1'])
            self.assertTrue(all(stage['max_num_seqs'] == 1 for stage in config['stages']))
            self.assertNotIn('platforms', config)  # Keep upstream CUDA rotary safeguard.
            self.assertTrue(all('default_sampling_params' not in stage for stage in config['stages']))

    def test_commands_have_exact_model_name_local_paths_and_stage_deploy(self):
        with tempfile.TemporaryDirectory() as temporary:
            location = checkpoint_fixture(Path(temporary))
            argv = runtime.command('omni', location, executable='/isolated/bin/vllm', deploy='/safe/deploy.json')
            self.assertEqual(argv[argv.index('--served-model-name') + 1], runtime.MODEL_ID)
            self.assertEqual(argv[2], str(location['model']))
            self.assertEqual(argv[argv.index('--port') + 1], '18097')
            self.assertNotIn('--gpu-memory-utilization', argv)
            with patch.dict(os.environ, {'LIVE_LISTENING_MODE': 'nemotron-smart-turn'}):
                director = runtime.command('director', location, executable='/isolated/bin/vllm')
            self.assertEqual(director[director.index('--gpu-memory-utilization') + 1], '0.75')
            self.assertEqual(director[director.index('--served-model-name') + 1], 'live-director')

    def test_child_environment_maps_actual_gpus_offline_and_does_not_launch_on_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            location = checkpoint_fixture(Path(temporary))
            env = {'SLURM_JOB_ID': '123', 'CUDA_VISIBLE_DEVICES': '2,5,7', 'LIVE_LISTENING_MODE': 'baseline'}
            with patch.dict(os.environ, env), patch.object(runtime, 'validate_store', return_value=location), \
                    patch.object(runtime, 'check_ports') as ports, patch.object(runtime.os, 'execve') as execute, \
                    patch.object(sys, 'argv', ['runtime.py', '--check']):
                runtime.main()
            execute.assert_not_called()
            self.assertEqual(ports.call_args.args[0], runtime.PORTS)
            with patch.dict(os.environ, env), patch.object(runtime, 'validate_store', return_value=location), \
                    patch.object(runtime.os, 'execve') as execute, patch.object(sys, 'argv', ['runtime.py', '--role', 'director']):
                runtime.main()
            command, argv, child_env = execute.call_args.args
            self.assertEqual(child_env['CUDA_VISIBLE_DEVICES'], '7')
            self.assertEqual(child_env['HF_HUB_OFFLINE'], '1')
            self.assertEqual(child_env['TRANSFORMERS_OFFLINE'], '1')
            self.assertIn('.venv-qwen-stream', command)


class ShellTests(unittest.TestCase):
    def test_stack_failure_cleans_only_owned_workers_and_never_starts_portal(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / 'fake repo'
            target = repo / 'experiments/qwen_stream'
            target.mkdir(parents=True)
            shutil.copy(EXPERIMENT / 'run_stack.sh', target)
            calls = Path(temporary) / 'calls.jsonl'
            for name in ('.venv-live', '.venv-qwen-stream'):
                (repo / name / 'bin').mkdir(parents=True)
            live = repo / '.venv-live/bin/python'
            live.write_text('#!' + sys.executable + '\nimport json,os,signal,sys\na=sys.argv[1:]\n'
                'with open(os.environ["CALL_LOG"],"a") as f:f.write(json.dumps(a)+"\\n")\n'
                'if a[:2]==["scripts/live_process.py","start"]:os.execvp(a[2],a[2:])\n'
                'if a[:2]==["scripts/live_process.py","stop"]:\n'
                ' for p in a[2:]:\n'
                '  try:os.kill(int(p),signal.SIGTERM)\n'
                '  except ProcessLookupError:pass\n')
            live.chmod(0o755)
            isolated = repo / '.venv-qwen-stream/bin/python'
            isolated.write_text('#!' + sys.executable + '\nimport sys,time\na=sys.argv[1:]\n'
                'if "--role" in a:\n'
                ' if a[-1]=="director":sys.exit(17)\n'
                ' while True:time.sleep(.1)\n')
            isolated.chmod(0o755)
            (repo / '.venv-qwen-stream/bin/vllm').write_text('fixture')
            (repo / '.venv-qwen-stream/bin/vllm').chmod(0o755)
            fake_bin = Path(temporary) / 'bin'
            fake_bin.mkdir()
            curl = fake_bin / 'curl'
            curl.write_text('#!/bin/sh\ncase "$*" in *18097*) exit 0 ;; *) exit 1 ;; esac\n')
            curl.chmod(0o755)
            env = {**os.environ, 'PATH': str(fake_bin) + os.pathsep + os.environ['PATH'],
                   'CALL_LOG': str(calls), 'LIVE_VOICE_STORE': str(Path(temporary) / 'scratch'),
                   'QWEN_STREAM_STORE': str(Path(temporary) / 'scratch/stream'),
                   'LIVE_LISTENING_MODE': 'baseline', 'SLURM_JOB_ID': '123', 'CUDA_VISIBLE_DEVICES': '2,5,7'}
            result = subprocess.run(['bash', str(target / 'run_stack.sh')], env=env, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            logged = [json.loads(line) for line in calls.read_text().splitlines()]
            starts = [call for call in logged if call[:2] == ['scripts/live_process.py', 'start']]
            stops = [call for call in logged if call[:2] == ['scripts/live_process.py', 'stop']]
            self.assertEqual(len(starts), 2)
            self.assertEqual(len(stops), 1)
            self.assertEqual(len(stops[0][2:]), 2)
            self.assertTrue(all(int(pid) > 1 for pid in stops[0][2:]))
            self.assertFalse(any(call[:2] == ['-m', 'studio'] for call in logged))

    def test_shell_syntax_and_help_never_installs_or_requires_slurm(self):
        for filename in ('setup.sh', 'run_stack.sh', 'run.slurm.sbatch'):
            result = subprocess.run(['bash', '-n', str(EXPERIMENT / filename)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        for filename in ('setup.sh', 'run_stack.sh'):
            result = subprocess.run(['bash', str(EXPERIMENT / filename), '--help'], capture_output=True, text=True,
                                    env={**os.environ, 'SLURM_JOB_ID': '', 'CUDA_VISIBLE_DEVICES': ''})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Usage:', result.stdout)
        for filename in ('runtime.py', 'download.py'):
            result = subprocess.run([sys.executable, str(EXPERIMENT / filename), '--help'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_setup_targets_only_isolated_environment_and_scratch_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / 'repo with spaces'
            target = repo / 'experiments/qwen_stream'
            target.mkdir(parents=True)
            shutil.copy(EXPERIMENT / 'setup.sh', target)
            # Existing baseline environments must remain untouched by setup.
            for name in ('.venv-live', '.venv-drama3', '.venv-listening', '.venv-qwen-stream'):
                (repo / name / 'bin').mkdir(parents=True)
                (repo / name / 'marker').write_text('unchanged')
            fake_bin = Path(temporary) / 'bin'
            fake_bin.mkdir()
            log = Path(temporary) / 'calls.jsonl'
            uv = fake_bin / 'uv'
            uv.write_text('#!' + sys.executable + '\nimport json,os,sys\nwith open(os.environ["CALL_LOG"],"a") as f:f.write(json.dumps({"argv":sys.argv[1:],"cache":os.environ["UV_CACHE_DIR"]})+"\\n")\n')
            uv.chmod(0o755)
            uname = fake_bin / 'uname'
            uname.write_text('#!/bin/sh\nif [ "$1" = -s ]; then echo Linux; else echo x86_64; fi\n')
            uname.chmod(0o755)
            python = repo / '.venv-qwen-stream/bin/python'
            python.write_text('#!/bin/sh\nexit 0\n')
            python.chmod(0o755)
            env = {**os.environ, 'PATH': str(fake_bin) + os.pathsep + '/usr/bin:/bin',
                   'HOME': str(Path(temporary) / 'home'), 'CALL_LOG': str(log),
                   'LIVE_VOICE_STORE': str(Path(temporary) / 'scratch store')}
            env.pop('QWEN_STREAM_STORE', None)
            result = subprocess.run(['bash', str(target / 'setup.sh')], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(len(calls), 1)  # Existing isolated env was not recreated.
            self.assertIn('.venv-qwen-stream/bin/python', calls[0]['argv'])
            self.assertEqual(calls[0]['cache'], str(Path(env['LIVE_VOICE_STORE']) / 'qwen-stream/uv-cache'))
            for name in ('.venv-live', '.venv-drama3', '.venv-listening'):
                self.assertEqual((repo / name / 'marker').read_text(), 'unchanged')

    def test_batch_preserves_new_allocation_and_listening_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / 'repo with spaces'
            target = repo / 'experiments/qwen_stream'
            target.mkdir(parents=True)
            shutil.copy(EXPERIMENT / 'run.slurm.sbatch', target)
            (target / 'run_stack.sh').write_text('#!/bin/sh\nexit 17\n')
            fake_bin = Path(temporary) / 'bin'
            fake_bin.mkdir()
            log = Path(temporary) / 'argv.json'
            srun = fake_bin / 'srun'
            srun.write_text('#!' + sys.executable + '\nimport json,os,sys\nwith open(os.environ["CALL_LOG"],"w") as f:json.dump({"argv":sys.argv[1:],"job":os.environ.get("SLURM_JOB_ID"),"gpu":os.environ.get("CUDA_VISIBLE_DEVICES"),"bind":os.environ.get("SLURM_CPU_BIND")},f)\na=sys.argv[1:]\ni=a.index("bash")\nos.execvp(a[i],a[i:])\n')
            srun.chmod(0o755)
            env = {**os.environ, 'PATH': str(fake_bin) + os.pathsep + os.environ['PATH'],
                   'CALL_LOG': str(log), 'SLURM_SUBMIT_DIR': str(repo), 'SLURM_JOB_ID': '987',
                   'SLURM_CPUS_PER_TASK': '12', 'CUDA_VISIBLE_DEVICES': '2,5,7', 'SLURM_CPU_BIND': 'old-mask'}
            result = subprocess.run(['bash', str(target / 'run.slurm.sbatch'), '--listening', 'nemotron-smart-turn'],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 17, result.stderr)
            call = json.loads(log.read_text())
            self.assertEqual(call['argv'], ['--ntasks=1', '--cpus-per-task=12', '--cpu-bind=none',
                                           'bash', 'experiments/qwen_stream/run_stack.sh', '--listening', 'nemotron-smart-turn'])
            self.assertEqual((call['job'], call['gpu'], call['bind']), ('987', '2,5,7', None))


if __name__ == '__main__':
    unittest.main()
