"""Opt-in configuration and launch ownership, using fake workers and no GPUs."""
from contextlib import redirect_stdout
from copy import deepcopy
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

from live_voice.deployment import assigned_devices, profile
from live_voice.listening_config import BASELINE, EXPERIMENT, listening_mode, listening_profile
from scripts import run_live_listening, run_live_model

ROOT = Path(__file__).resolve().parents[1]


class ListeningProfileTests(unittest.TestCase):
    def test_baseline_returns_the_unchanged_original_configuration(self):
        for name in ('shared','quality-2gpu','quality-3gpu'):
            config=profile(name); before=deepcopy(config)
            with self.subTest(profile=name):
                self.assertIs(listening_profile(config,BASELINE),config)
                self.assertEqual(config,before)
        with patch.dict(os.environ,{'LIVE_LISTENING_MODE':BASELINE}):
            self.assertEqual(listening_mode(),BASELINE)

    def test_experiment_is_a_deep_copy_of_three_gpu_profile_with_only_director_budget_changed(self):
        config=profile('quality-3gpu'); before=deepcopy(config)
        selected=listening_profile(config,EXPERIMENT)
        expected=deepcopy(config); expected['director']['memory']=.75
        self.assertEqual(selected,expected)
        self.assertEqual(config,before)
        self.assertIsNot(selected,config)
        self.assertIsNot(selected['director'],config['director'])
        self.assertIsNot(selected['actor'],config['actor'])
        self.assertEqual(assigned_devices(selected,'GPU-first,GPU-second,GPU-third'),
                         {'tts':'GPU-first','actor':'GPU-second','director':'GPU-third'})
        for name in ('shared','quality-2gpu'):
            with self.subTest(profile=name), self.assertRaises(ValueError):
                listening_profile(profile(name),EXPERIMENT)
        with self.assertRaises(ValueError): listening_mode('unknown')

    def test_listener_wrapper_uses_actual_director_token_and_preserves_custom_store(self):
        for custom in (None,'/tmp/listening with spaces'):
            environment={'LIVE_PROFILE':'quality-3gpu','LIVE_LISTENING_MODE':EXPERIMENT,
                         'LIVE_VOICE_STORE':'/tmp/voice store','CUDA_VISIBLE_DEVICES':'GPU-a,GPU-b,GPU-c'}
            if custom: environment['LISTENING_STORE']=custom
            with self.subTest(store=custom), patch.dict(os.environ,environment,clear=True), redirect_stdout(StringIO()), patch.object(run_live_listening.os,'execve') as execute:
                run_live_listening.main()
            executable,arguments,child_environment=execute.call_args.args
            self.assertEqual(executable,sys.executable)
            self.assertEqual(arguments,[sys.executable,'-m','listening_runtime','--store',
                                        custom or '/tmp/voice store/listening','--port','18095'])
            self.assertEqual(child_environment['CUDA_VISIBLE_DEVICES'],'GPU-c')
            self.assertEqual(child_environment['OMP_NUM_THREADS'],'4')
            self.assertEqual(environment['CUDA_VISIBLE_DEVICES'],'GPU-a,GPU-b,GPU-c')

    def test_listener_wrapper_never_executes_with_invalid_mode_profile_allocation_or_store(self):
        base={'LIVE_PROFILE':'quality-3gpu','LIVE_LISTENING_MODE':EXPERIMENT,
              'LIVE_VOICE_STORE':'/tmp/store','CUDA_VISIBLE_DEVICES':'0,1,2'}
        cases=[{**base,'LIVE_LISTENING_MODE':BASELINE}, {**base,'LIVE_PROFILE':'quality-2gpu'},
               {**base,'CUDA_VISIBLE_DEVICES':'0,1'}, {key:value for key,value in base.items() if key!='LIVE_VOICE_STORE'}]
        for environment in cases:
            with self.subTest(environment=environment), patch.dict(os.environ,environment,clear=True), patch.object(run_live_listening.os,'execve') as execute:
                with self.assertRaises((SystemExit,ValueError,KeyError)):
                    run_live_listening.main()
                execute.assert_not_called()

    def test_actor_and_director_commands_use_real_paths_and_budget_changes_only_when_enabled(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Path(folder)/'model files'
            for role in ('actor','director'):
                path=store/profile('quality-3gpu')[role]['directory']; path.mkdir(parents=True)
                (path/'config.json').write_text('{}')
            for mode,role,memory,device in ((BASELINE,'director','.85','GPU-c'),
                                             (EXPERIMENT,'director','.75','GPU-c'),
                                             (EXPERIMENT,'actor','.85','GPU-b')):
                environment={'LIVE_PROFILE':'quality-3gpu','LIVE_LISTENING_MODE':mode,
                             'LIVE_VOICE_STORE':str(store),'CUDA_VISIBLE_DEVICES':'GPU-a,GPU-b,GPU-c'}
                with self.subTest(mode=mode,role=role), patch.dict(os.environ,environment,clear=True), patch.object(sys,'argv',['run_live_model.py',role]), patch.object(run_live_model.os,'execve') as execute:
                    run_live_model.main()
                _,arguments,child=execute.call_args.args
                self.assertEqual(arguments[2],str(store/profile('quality-3gpu')[role]['directory']))
                self.assertEqual(float(arguments[arguments.index('--gpu-memory-utilization')+1]),float(memory))
                self.assertEqual(child['CUDA_VISIBLE_DEVICES'],device)
            shutil.rmtree(store/'director-32b')
            with patch.dict(os.environ,environment,clear=True), patch.object(sys,'argv',['run_live_model.py','director']), patch.object(run_live_model.os,'execve') as execute:
                with self.assertRaises(SystemExit): run_live_model.main()
                execute.assert_not_called()


FAKE_PYTHON = r'''import json,os,pathlib,sys,time
args=sys.argv[1:]
code=sys.stdin.read() if args==['-'] else ''
root=pathlib.Path(os.environ['LISTENING_TEST_ROOT'])
def record(value):
    with open(root/'events.jsonl','a') as stream: stream.write(json.dumps(value)+'\n')
record({'tool':'python','args':args,'code':code,'mode':os.environ.get('LIVE_LISTENING_MODE'),
        'url':os.environ.get('LIVE_LISTENING_URL')})
if 'validate_store' in code and os.environ.get('LISTENING_TEST_PREFLIGHT_FAIL')=='1':
    sys.exit(37)
if args[:2]==['scripts/live_process.py','start']:
    role=pathlib.Path(args[-1]).stem.removeprefix('run_live_')
    record({'tool':'start','role':role,'pid':os.getpid()})
    if os.environ.get('LISTENING_TEST_FAIL_START')==role: sys.exit(73)
    (root/('ready-'+role)).touch()
    end=time.monotonic()+6
    while not (root/('stop-'+str(os.getpid()))).exists() and time.monotonic()<end:time.sleep(.01)
elif args[:2]==['scripts/live_process.py','stop']:
    record({'tool':'stop','pids':args[2:]})
    for pid in args[2:]:(root/('stop-'+pid)).touch()
elif args and args[0]=='-c' and 'director_timeout' in args[1]:
    print('12')
'''


class FakeStackTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)/'checkout with spaces'
        (self.root/'scripts').mkdir(parents=True)
        self.stack=self.root/'scripts/run_live_stack.sh'
        shutil.copyfile(ROOT/'scripts/run_live_stack.sh',self.stack)
        for environment in ('.venv-live','.venv-drama3'):
            executable=self.root/environment/'bin/python'; executable.parent.mkdir(parents=True)
            executable.write_text('#!'+sys.executable+'\n'+FAKE_PYTHON); executable.chmod(0o755)
        tools=self.root/'.venv-drama3/bin'
        curl=tools/'curl'
        curl.write_text('#!'+sys.executable+'''\nimport os,pathlib,sys,time
root=pathlib.Path(os.environ['LISTENING_TEST_ROOT'])
port=sys.argv[-1].split(':')[-1].split('/')[0]
role={'18095':'listening','18093':'tts','18092':'llm','18094':'director'}[port]
end=time.monotonic()+.5
while not (root/('ready-'+role)).exists() and time.monotonic()<end:time.sleep(.005)
raise SystemExit(0 if (root/('ready-'+role)).exists() else 1)
'''); curl.chmod(0o755)
        sleep=tools/'sleep'; sleep.write_text('#!'+sys.executable+'\nimport time\ntime.sleep(.01)\n'); sleep.chmod(0o755)
        self.environment=dict(os.environ,SLURM_JOB_ID='fake-job',CUDA_VISIBLE_DEVICES='GPU-a,GPU-b,GPU-c',
                              LIVE_PROFILE='quality-3gpu',LIVE_LISTENING_MODE=BASELINE,
                              LIVE_VOICE_STORE=str(self.root/'store'),LISTENING_TEST_ROOT=str(self.root))
        self.environment.pop('LIVE_LISTENING_URL',None)
        self.environment.pop('LIVE_DIRECTOR_URL',None)

    def listener_environment(self):
        executable=self.root/'.venv-listening/bin/python'; executable.parent.mkdir(parents=True)
        executable.write_text('#!'+sys.executable+'\n'+FAKE_PYTHON); executable.chmod(0o755)

    def events(self):
        file=self.root/'events.jsonl'
        return [json.loads(line) for line in file.read_text().splitlines()] if file.exists() else []

    def launch(self,*arguments):
        return subprocess.run(['bash',str(self.stack),'--profile','quality-3gpu',*arguments],
                              env=self.environment,capture_output=True,text=True,timeout=12)

    def test_baseline_does_not_require_or_start_listener_and_cleans_its_three_servers(self):
        result=self.launch(); self.assertEqual(result.returncode,0,result.stderr)
        events=self.events(); started=[item for item in events if item['tool']=='start']
        self.assertEqual({item['role'] for item in started},{'tts','llm','director'})
        self.assertFalse(any('validate_store' in item.get('code','') for item in events))
        self.assertFalse(any('.venv-listening' in ' '.join(item.get('args',[])) for item in events))
        stopped=[item for item in events if item['tool']=='stop']
        self.assertEqual(len(stopped),1)
        self.assertEqual(set(stopped[0]['pids']),{str(item['pid']) for item in started})

    def test_experiment_preflights_before_start_and_exports_local_service_url(self):
        self.listener_environment()
        result=self.launch('--listening',EXPERIMENT); self.assertEqual(result.returncode,0,result.stderr)
        events=self.events(); started=[item for item in events if item['tool']=='start']
        self.assertEqual({item['role'] for item in started},{'listening','tts','llm','director'})
        preflight=next(index for index,item in enumerate(events) if 'validate_store' in item.get('code',''))
        self.assertLess(preflight,min(index for index,item in enumerate(events) if item['tool']=='start'))
        portal=next(item for item in events if item.get('args',[])[:2]==['-m','studio'])
        self.assertEqual(portal['mode'],EXPERIMENT)
        self.assertEqual(portal['url'],'http://127.0.0.1:18095')
        stop=next(item for item in events if item['tool']=='stop')
        self.assertEqual(set(stop['pids']),{str(item['pid']) for item in started})

    def test_missing_listening_environment_or_invalid_files_stop_before_any_model_start(self):
        result=self.launch('--listening',EXPERIMENT)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('setup_listening.sh',result.stderr)
        self.assertFalse(any(item['tool']=='start' for item in self.events()))
        self.listener_environment(); self.environment['LISTENING_TEST_PREFLIGHT_FAIL']='1'
        result=self.launch('--listening',EXPERIMENT)
        self.assertEqual(result.returncode,37)
        self.assertFalse(any(item['tool']=='start' for item in self.events()))

    def test_listener_start_failure_cleans_owned_worker_without_starting_other_models(self):
        self.listener_environment(); self.environment['LISTENING_TEST_FAIL_START']='listening'
        result=self.launch('--listening',EXPERIMENT)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('listening exited',result.stderr)
        events=self.events(); started=[item for item in events if item['tool']=='start']
        self.assertEqual([item['role'] for item in started],['listening'])
        stop=next(item for item in events if item['tool']=='stop')
        self.assertEqual(stop['pids'],[str(started[0]['pid'])])
        self.assertFalse(any(item.get('args',[])[:2]==['-m','studio'] for item in events))


class BatchForwardingTests(unittest.TestCase):
    def test_batch_passes_optional_listening_arguments_to_the_same_single_job_step(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); (root/'scripts').mkdir()
            (root/'scripts/run_live_stack.sh').write_text('#!/bin/bash\nexit 99\n')
            batch=root/'scripts/run_live_slurm.sbatch'; shutil.copyfile(ROOT/'scripts/run_live_slurm.sbatch',batch)
            tools=root/'tools'; tools.mkdir(); output=root/'srun.json'
            srun=tools/'srun'; srun.write_text('#!'+sys.executable+'\n'+'''import json,os,sys
keys=('SLURM_JOB_ID','SLURM_CPUS_PER_TASK','SLURM_JOB_CPUS_PER_NODE','CUDA_VISIBLE_DEVICES',
      'SLURM_CPU_BIND','SLURM_CPU_BIND_LIST','SLURM_CPU_BIND_TYPE','SLURM_CPU_BIND_VERBOSE',
      'SRUN_CPU_BIND','LIVE_VERIFY_VOICE')
with open(os.environ['LISTENING_TEST_OUTPUT'],'w') as stream:
    json.dump({'argv':sys.argv[1:],'environment':{key:os.environ[key] for key in keys if key in os.environ}},stream)
raise SystemExit(31)
'''); srun.chmod(0o755)
            environment=dict(os.environ,PATH=str(tools)+os.pathsep+os.environ.get('PATH',''),SLURM_JOB_ID='fake-job',
                             SLURM_SUBMIT_DIR=str(root),LISTENING_TEST_OUTPUT=str(output),SLURM_CPUS_PER_TASK='24',
                             SLURM_JOB_CPUS_PER_NODE='24',CUDA_VISIBLE_DEVICES='GPU-a,GPU-b,GPU-c',LIVE_VERIFY_VOICE='0',
                             SLURM_CPU_BIND='mask_cpu:0xf000000',SLURM_CPU_BIND_LIST='0xf000000',
                             SLURM_CPU_BIND_TYPE='mask_cpu:',SLURM_CPU_BIND_VERBOSE='verbose',
                             SRUN_CPU_BIND='unrelated-value')
            result=subprocess.run(['bash',str(batch),'--listening',EXPERIMENT],env=environment,capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,31)
            recorded=json.loads(output.read_text())
            self.assertEqual(recorded['argv'],['--ntasks=1','--cpus-per-task=24','--cpu-bind=none',
                                              'bash','scripts/run_live_stack.sh',
                                              '--profile','quality-3gpu','--listening',EXPERIMENT])
            self.assertNotIn('--overlap',recorded['argv'])
            for key in ('SLURM_CPU_BIND','SLURM_CPU_BIND_LIST','SLURM_CPU_BIND_TYPE','SLURM_CPU_BIND_VERBOSE'):
                self.assertNotIn(key,recorded['environment'])
            for key in ('SLURM_JOB_ID','SLURM_CPUS_PER_TASK','SLURM_JOB_CPUS_PER_NODE',
                        'CUDA_VISIBLE_DEVICES','LIVE_VERIFY_VOICE','SRUN_CPU_BIND'):
                self.assertEqual(recorded['environment'][key],environment[key])


if __name__ == '__main__': unittest.main()
