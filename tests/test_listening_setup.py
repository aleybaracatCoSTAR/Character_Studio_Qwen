"""Isolated setup and pinned offline files; fake installers/downloads only."""
from contextlib import redirect_stderr, redirect_stdout
import builtins
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import download_listening_models as models

ROOT = Path(__file__).resolve().parents[1]


class ModelFilesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Path(self.temporary.name) / 'listening models'
        self.calls = []

    def download(self, **kwargs):
        self.calls.append(kwargs)
        path = Path(kwargs['local_dir']) / kwargs['filename']
        path.parent.mkdir(parents=True, exist_ok=True)
        if kwargs['filename'] == 'config.json':
            value = json.dumps({'model_type':'nemotron_asr_streaming','architectures':['NemotronAsrStreamingForRNNT']}).encode()
        else:
            value = ('synthetic fixture '+kwargs['filename']).encode()
        path.write_bytes(value)
        return str(path)

    def seed(self):
        return models.download_models(self.store, download=self.download)

    def test_exact_files_and_revisions_exclude_nemo_gguf_and_older_turn_artifacts(self):
        report = self.seed()
        self.assertEqual(report['files'], len(models.NEMOTRON_FILES) + 1)
        self.assertEqual(len(self.calls), report['files'])
        for call in self.calls:
            self.assertIs(call['token'], False)
            if call['repo_id'] == models.NEMOTRON_ID:
                self.assertEqual(call['revision'], 'ebe59e5a817142986528bbbee5dba8db7b38ed50')
                self.assertIn(call['filename'], models.NEMOTRON_FILES)
                self.assertNotIn(Path(call['filename']).suffix, ('.nemo','.gguf'))
            else:
                self.assertEqual(call['repo_id'], models.SMARTTURN_ID)
                self.assertEqual(call['revision'], 'f766f81d3cfdf7737ac64aad813d91bbfd56bf93')
                self.assertEqual(call['filename'], 'smart-turn-v3.2-cpu.onnx')
        model, turn = models.model_paths(self.store)
        self.assertEqual(json.loads((model/models.NEMOTRON_MANIFEST).read_text())['files']['model.safetensors'],
                         {'bytes':(model/'model.safetensors').stat().st_size,
                          'sha256':hashlib.sha256((model/'model.safetensors').read_bytes()).hexdigest()})
        self.assertEqual(json.loads(turn.with_suffix('.onnx.json').read_text())['filename'], models.SMARTTURN_FILENAME)

    def test_verified_files_are_reused_and_check_never_imports_runtime_or_hub(self):
        expected = self.seed()
        self.calls.clear()
        original = builtins.__import__
        def guard(name, *args, **kwargs):
            if name.split('.')[0] in ('torch','transformers','onnxruntime','huggingface_hub'):
                raise AssertionError('Offline validation must not import model or download libraries.')
            return original(name,*args,**kwargs)
        with patch('builtins.__import__', side_effect=guard):
            self.assertEqual(models.validate_store(self.store), expected)
            self.assertEqual(models.download_models(self.store, download=self.download), expected)
            with redirect_stdout(StringIO()):
                self.assertEqual(models.main(['--check','--store',str(self.store)]), 0)
        self.assertEqual(self.calls, [])

    def test_tampered_or_missing_weights_do_not_pass_offline_validation(self):
        self.seed()
        model, _ = models.model_paths(self.store)
        weights = model/'model.safetensors'
        good = weights.read_bytes()
        for bad in (b'changed', b''):
            weights.write_bytes(bad)
            with self.subTest(size=len(bad)), self.assertRaises(models.ListeningFilesError):
                models.validate_store(self.store)
        weights.write_bytes(good)
        weights.unlink()
        with self.assertRaises(models.ListeningFilesError): models.validate_store(self.store)

    def test_manifest_provenance_and_complete_file_list_are_required(self):
        self.seed()
        model, turn = models.model_paths(self.store)
        manifest = model/models.NEMOTRON_MANIFEST
        original = manifest.read_text()
        for field, value in (('revision','0'*40), ('model_id','other/model'), ('files',{})):
            data = json.loads(original); data[field] = value; manifest.write_text(json.dumps(data))
            with self.subTest(field=field), self.assertRaises(models.ListeningFilesError):
                models.validate_store(self.store)
        manifest.write_text(original)
        sidecar = turn.with_suffix('.onnx.json')
        data = json.loads(sidecar.read_text()); data['filename']='smart-turn-v3.1-cpu.onnx'
        sidecar.write_text(json.dumps(data))
        with self.assertRaises(models.ListeningFilesError): models.validate_store(self.store)

    def test_hash_correct_wrong_model_configuration_is_rejected(self):
        self.seed()
        model, _ = models.model_paths(self.store)
        config = model/'config.json'; config.write_text('{"model_type":"different"}')
        manifest = model/models.NEMOTRON_MANIFEST; value=json.loads(manifest.read_text())
        value['files']['config.json']={'bytes':config.stat().st_size,'sha256':hashlib.sha256(config.read_bytes()).hexdigest()}
        manifest.write_text(json.dumps(value))
        with self.assertRaises(models.ListeningFilesError): models.validate_store(self.store)

    def test_symlink_artifact_and_model_directory_are_rejected(self):
        self.seed()
        model, _ = models.model_paths(self.store)
        target = self.store.parent/'outside'; target.write_bytes((model/'model.safetensors').read_bytes())
        weights=model/'model.safetensors'; weights.unlink(); weights.symlink_to(target)
        with self.assertRaises(models.ListeningFilesError): models.validate_store(self.store)
        weights.unlink(); weights.write_bytes(target.read_bytes())
        moved=self.store.parent/'moved-model'; model.rename(moved); model.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(models.ListeningFilesError): models.validate_store(self.store)

    def test_download_failure_does_not_create_a_complete_manifest_or_load_any_model(self):
        def fail(**kwargs):
            if kwargs['filename']=='model.safetensors': raise RuntimeError('private upstream message')
            return self.download(**kwargs)
        with redirect_stderr(StringIO()) as error:
            self.assertEqual(models.main(['--download','--store',str(self.store)], download=fail),1)
        self.assertNotIn('private upstream message',error.getvalue())
        model, _ = models.model_paths(self.store)
        self.assertFalse((model/models.NEMOTRON_MANIFEST).exists())
        with self.assertRaises(models.ListeningFilesError): models.validate_store(self.store)

    def test_download_requires_explicit_action_and_defaults_to_separate_listening_store(self):
        for arguments in ([],['--download','--check']):
            with self.subTest(arguments=arguments), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                models.main(arguments,download=lambda **_: self.fail('No download for invalid arguments.'))
        with patch.dict(os.environ,{'LIVE_VOICE_STORE':'/scratch/example/live'},clear=True):
            self.assertEqual(models.default_store(),Path('/scratch/example/live/listening'))
        with patch.dict(os.environ,{'LIVE_VOICE_STORE':'/scratch/example/live','LISTENING_STORE':'/tmp/separate'},clear=True):
            self.assertEqual(models.default_store(),Path('/tmp/separate'))


class IsolatedSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)/'checkout with spaces'; (self.root/'scripts').mkdir(parents=True)
        self.setup=self.root/'scripts/setup_listening.sh'; shutil.copyfile(ROOT/'scripts/setup_listening.sh',self.setup)
        self.tools=self.root/'fake-tools'; self.tools.mkdir(); self.log=self.root/'calls.jsonl'
        self.fake_python=self.tools/'fake-python'
        self.fake_python.write_text('#!'+sys.executable+'\nimport os,json,sys\nwith open(os.environ["LISTENING_TEST_LOG"],"a") as stream: stream.write(json.dumps({"tool":"python","args":sys.argv[1:],"code":sys.stdin.read()})+"\\n")\n')
        self.fake_python.chmod(0o755)
        uv=self.tools/'uv'
        uv.write_text('#!'+sys.executable+'\nimport os,json,sys,pathlib,shutil\nwith open(os.environ["LISTENING_TEST_LOG"],"a") as stream: stream.write(json.dumps({"tool":"uv","args":sys.argv[1:],"cache":os.environ.get("UV_CACHE_DIR")})+"\\n")\nif sys.argv[1]=="venv":\n p=pathlib.Path(sys.argv[-1])/"bin/python"; p.parent.mkdir(parents=True); shutil.copyfile(os.environ["LISTENING_TEST_PYTHON"],p); p.chmod(0o755)\n')
        uv.chmod(0o755)
        self.environment=dict(os.environ, PATH=str(self.tools)+os.pathsep+os.environ.get('PATH',''),
            LIVE_VOICE_STORE=str(self.root/'store'),LISTENING_STORE=str(self.root/'store/listening'),
            LISTENING_TEST_LOG=str(self.log),LISTENING_TEST_PYTHON=str(self.fake_python))
        for name in ('.venv-live','.venv-drama3'):
            (self.root/name).mkdir(); (self.root/name/'untouched').write_text('existing environment')

    def run_setup(self):
        return subprocess.run(['bash',str(self.setup)],env=self.environment,text=True,capture_output=True,timeout=15)

    def calls(self): return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_bash_syntax_and_requirements_are_isolated_pinned_and_cuda_matched(self):
        result=subprocess.run(['bash','-n',str(ROOT/'scripts/setup_listening.sh')],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        requirements=(ROOT/'experiments/listening/requirements.txt').read_text()
        self.assertIn('transformers==5.13.0',requirements)
        self.assertIn('torch-2.13.0%2Bcu130-cp312',requirements)
        self.assertIn('librosa==0.11.0',requirements)
        self.assertIn('onnxruntime==1.26.0',requirements)
        self.assertNotIn('onnxruntime-gpu',requirements)
        self.assertNotIn('nemo_toolkit',requirements)
        self.assertNotIn('git+',requirements)

    def test_fake_install_only_targets_new_venv_and_does_not_download_models(self):
        result=self.run_setup(); self.assertEqual(result.returncode,0,result.stderr)
        calls=self.calls(); installs=[call for call in calls if call['tool']=='uv']
        self.assertEqual(installs[0]['args'],['venv','--python','3.12','.venv-listening'])
        self.assertEqual(installs[1]['args'],['pip','install','--python','.venv-listening/bin/python','-r','experiments/listening/requirements.txt'])
        for call in installs:
            self.assertEqual(call['cache'],str(self.root/'store/listening/uv-cache'))
            self.assertNotIn('.venv-live', ' '.join(call['args']))
            self.assertNotIn('.venv-drama3', ' '.join(call['args']))
        for call in calls:
            self.assertNotIn('from_pretrained(',call.get('code',''))
            self.assertNotIn('hf_hub_download(',call.get('code',''))
        for name in ('.venv-live','.venv-drama3'):
            self.assertEqual((self.root/name/'untouched').read_text(),'existing environment')
        self.assertIn('No checkpoint was loaded',calls[-1]['code'])
        self.assertIn('--listening nemotron-smart-turn',result.stdout)

    def test_link_to_existing_environment_is_rejected_before_any_install(self):
        (self.root/'.venv-listening').symlink_to(self.root/'.venv-drama3',target_is_directory=True)
        result=self.run_setup(); self.assertNotEqual(result.returncode,0)
        self.assertIn('not a link',result.stderr)
        self.assertEqual(self.calls(),[])
        self.assertEqual((self.root/'.venv-drama3/untouched').read_text(),'existing environment')

    def test_existing_listening_environment_is_reused_without_recreating_other_venvs(self):
        executable=self.root/'.venv-listening/bin/python'; executable.parent.mkdir(parents=True)
        shutil.copyfile(self.fake_python,executable); executable.chmod(0o755)
        result=self.run_setup(); self.assertEqual(result.returncode,0,result.stderr)
        installs=[call['args'] for call in self.calls() if call['tool']=='uv']
        self.assertEqual(len(installs),1)
        self.assertEqual(installs[0][:2],['pip','install'])


if __name__ == '__main__': unittest.main()
