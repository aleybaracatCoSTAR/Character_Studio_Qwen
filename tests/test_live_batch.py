"""The batch wrapper uses one existing allocation, without real Slurm or GPUs."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1]/'scripts'/'run_live_slurm.sbatch'
STACK_COMMAND = ['bash','scripts/run_live_stack.sh','--profile','quality-3gpu']
AFFINITY_KEYS = ('SLURM_CPU_BIND','SLURM_CPU_BIND_LIST','SLURM_CPU_BIND_TYPE','SLURM_CPU_BIND_VERBOSE')
PRESERVED_ENV = {'SLURM_JOB_ID':'12345','SLURM_CPUS_PER_TASK':'24','SLURM_JOB_CPUS_PER_NODE':'24',
                 'CUDA_VISIBLE_DEVICES':'GPU-second,GPU-fifth,GPU-sixth','SLURM_CPU_BIND_CUSTOM':'unrelated',
                 'SRUN_CPU_BIND':'not-a-supported-slurm-option',
                 'LIVE_VOICE_STORE':'/scratch/example voice store','OMP_NUM_THREADS':'4'}


class LiveBatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.checkout = self.root/'a checkout with spaces'
        (self.checkout/'scripts').mkdir(parents=True)
        self.commands = self.root/'fake commands'
        self.commands.mkdir()
        self.srun_record = self.root/'srun.jsonl'
        self.stack_record = self.root/'stack.jsonl'
        fake_srun = self.commands/'srun'
        fake_srun.write_text('#!'+sys.executable+'\n'+'''import json,os,pathlib,subprocess,sys
args=sys.argv[1:]
keys=('SLURM_JOB_ID','SLURM_CPUS_PER_TASK','SLURM_JOB_CPUS_PER_NODE','CUDA_VISIBLE_DEVICES',
      'SLURM_CPU_BIND','SLURM_CPU_BIND_LIST','SLURM_CPU_BIND_TYPE','SLURM_CPU_BIND_VERBOSE',
      'SLURM_CPU_BIND_CUSTOM','SRUN_CPU_BIND','LIVE_VOICE_STORE','OMP_NUM_THREADS')
record={'argv':args,'cwd':os.getcwd(),'unbuffered':os.environ.get('PYTHONUNBUFFERED'),
        'environment':{key:os.environ[key] for key in keys if key in os.environ}}
with open(os.environ['TEST_SRUN_RECORD'],'a') as stream: stream.write(json.dumps(record)+'\\n')
try: command=args.index('bash')
except ValueError: sys.exit(98)
options=args[:command]
if len(options)!=3 or set(options)!={'--ntasks=1','--cpu-bind=none','--cpus-per-task='+(os.environ.get('SLURM_CPUS_PER_TASK') or '24')}: sys.exit(98)
if args[command:]!=['bash','scripts/run_live_stack.sh','--profile','quality-3gpu']: sys.exit(98)
if pathlib.Path.cwd()!=pathlib.Path(os.environ['SLURM_SUBMIT_DIR']): sys.exit(97)
sys.exit(subprocess.run(args[command:]).returncode)
''')
        fake_srun.chmod(0o755)
        self.stack = self.checkout/'scripts'/'run_live_stack.sh'
        self.stack.write_text('''#!/usr/bin/env bash
set -euo pipefail
"$TEST_PYTHON" - "$@" <<'PYTEST'
import json,os,sys
keys=('SLURM_JOB_ID','SLURM_CPUS_PER_TASK','SLURM_JOB_CPUS_PER_NODE','CUDA_VISIBLE_DEVICES',
      'SLURM_CPU_BIND','SLURM_CPU_BIND_LIST','SLURM_CPU_BIND_TYPE','SLURM_CPU_BIND_VERBOSE',
      'SLURM_CPU_BIND_CUSTOM','SRUN_CPU_BIND','LIVE_VOICE_STORE','OMP_NUM_THREADS')
with open(os.environ['TEST_STACK_RECORD'],'a') as stream:
    stream.write(json.dumps({'argv':sys.argv[1:],'cwd':os.getcwd(),'unbuffered':os.environ.get('PYTHONUNBUFFERED'),
                            'environment':{key:os.environ[key] for key in keys if key in os.environ}})+'\\n')
PYTEST
exit "${TEST_STACK_EXIT:-0}"
''')
        self.environment = dict(os.environ)
        self.environment.update({'PATH':str(self.commands)+os.pathsep+os.defpath,
            **PRESERVED_ENV,'SLURM_SUBMIT_DIR':str(self.checkout),
            'TEST_PYTHON':sys.executable,'TEST_SRUN_RECORD':str(self.srun_record),
            'TEST_STACK_RECORD':str(self.stack_record)})

    def tearDown(self): self.temporary.cleanup()

    def run_wrapper(self, **changes):
        environment = dict(self.environment)
        for key,value in changes.items():
            if value is None: environment.pop(key,None)
            else: environment[key] = str(value)
        return subprocess.run(['bash',str(SCRIPT)],cwd=self.root,env=environment,
                              capture_output=True,text=True,timeout=5)

    @staticmethod
    def records(path):
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_batch_script_has_valid_bash_syntax(self):
        result = subprocess.run(['bash','-n',str(SCRIPT)],capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_resource_directives_are_literal_and_precede_every_shell_command(self):
        directives = {}
        executable_seen = False
        for raw in SCRIPT.read_text().splitlines():
            line = raw.strip()
            if not line: continue
            if line.startswith('#SBATCH'):
                self.assertFalse(executable_seen,'Slurm ignores directives after shell commands')
                self.assertNotIn('$',line)
                values = shlex.split(line[len('#SBATCH'):].strip())
                name,value = values[0].split('=',1) if len(values)==1 else values
                self.assertNotIn(name,directives)
                directives[name] = value
            elif not line.startswith('#'): executable_seen = True
        self.assertEqual(directives,{'--job-name':'character-live','--partition':'proto',
            '--nodes':'1','--ntasks':'1','--gres':'gpu:nvidia_h200:3','--cpus-per-task':'24',
            '--mem':'256G','--time':'04:00:00','--output':'slurm-character-%j.log'})

    def test_manual_launch_without_slurm_job_id_is_rejected_before_srun(self):
        for value in (None,''):
            with self.subTest(value=value):
                result = self.run_wrapper(SLURM_JOB_ID=value)
                self.assertNotEqual(result.returncode,0)
                self.assertTrue(result.stdout or result.stderr)
                self.assertEqual(self.records(self.srun_record),[])
                self.assertEqual(self.records(self.stack_record),[])

    def test_missing_submit_directory_is_rejected_before_srun(self):
        for value in (None,''):
            with self.subTest(value=value):
                result = self.run_wrapper(SLURM_SUBMIT_DIR=value)
                self.assertNotEqual(result.returncode,0)
                self.assertEqual(self.records(self.srun_record),[])
                self.assertEqual(self.records(self.stack_record),[])

    def test_submit_path_with_spaces_is_preserved_and_python_output_is_unbuffered(self):
        result = self.run_wrapper(PYTHONUNBUFFERED='0')
        self.assertEqual(result.returncode,0,result.stderr)
        srun = self.records(self.srun_record)
        stack = self.records(self.stack_record)
        self.assertEqual(len(srun),1)
        self.assertEqual(len(stack),1)
        for record in (srun[0],stack[0]):
            self.assertEqual(record['cwd'],str(self.checkout))
            self.assertEqual(record['unbuffered'],'1')
        self.assertIn('12345',result.stdout)

    def test_missing_stack_script_fails_before_starting_a_step(self):
        self.stack.unlink()
        result = self.run_wrapper()
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.records(self.srun_record),[])
        self.assertEqual(self.records(self.stack_record),[])
        self.assertTrue(result.stdout or result.stderr)

    def test_child_stack_exit_status_is_propagated(self):
        result = self.run_wrapper(TEST_STACK_EXIT='37')
        self.assertEqual(result.returncode,37,result.stdout+result.stderr)
        self.assertEqual(len(self.records(self.srun_record)),1)
        self.assertEqual(len(self.records(self.stack_record)),1)

    def test_exactly_one_step_uses_the_explicit_three_gpu_profile_without_prepare_or_overlap(self):
        result = self.run_wrapper(LIVE_PROFILE='shared')
        self.assertEqual(result.returncode,0,result.stderr)
        records = self.records(self.srun_record)
        self.assertEqual(len(records),1)
        arguments=records[0]['argv']; command=arguments.index('bash')
        self.assertEqual(set(arguments[:command]),{'--ntasks=1','--cpus-per-task=24','--cpu-bind=none'})
        self.assertEqual(arguments[command:],STACK_COMMAND)
        self.assertEqual(self.records(self.stack_record)[0]['argv'],['--profile','quality-3gpu'])
        self.assertNotIn('--overlap',records[0]['argv'])
        self.assertNotIn('--prepare',records[0]['argv'])

    def test_prior_cpu_binding_is_removed_without_erasing_the_new_allocation_or_application_settings(self):
        poisoned={'SLURM_CPU_BIND':'verbose,mask_cpu:0xffff00000000','SLURM_CPU_BIND_LIST':'0xffff00000000',
                  'SLURM_CPU_BIND_TYPE':'mask_cpu:','SLURM_CPU_BIND_VERBOSE':'verbose'}
        result=self.run_wrapper(**poisoned)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        for record in (self.records(self.srun_record)[0],self.records(self.stack_record)[0]):
            for key in AFFINITY_KEYS:self.assertNotIn(key,record['environment'])
            for key,value in PRESERVED_ENV.items():self.assertEqual(record['environment'][key],value)

    def test_step_cpu_count_matches_the_current_allocation_instead_of_the_prior_interactive_step(self):
        result=self.run_wrapper(SLURM_CPUS_PER_TASK='32',SLURM_JOB_CPUS_PER_NODE='32',
                                SLURM_CPU_BIND='mask_cpu:0x1',SLURM_CPU_BIND_LIST='0x1')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        records=self.records(self.srun_record);self.assertEqual(len(records),1)
        self.assertIn('--cpus-per-task=32',records[0]['argv'])
        self.assertIn('--cpu-bind=none',records[0]['argv'])
        self.assertEqual(self.records(self.stack_record)[0]['environment']['SLURM_CPUS_PER_TASK'],'32')

    def test_missing_or_empty_cpu_count_uses_the_batch_directive_default(self):
        for value in (None,''):
            with self.subTest(value=value):
                result=self.run_wrapper(SLURM_CPUS_PER_TASK=value)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertIn('--cpus-per-task=24',self.records(self.srun_record)[-1]['argv'])


if __name__ == '__main__': unittest.main()
