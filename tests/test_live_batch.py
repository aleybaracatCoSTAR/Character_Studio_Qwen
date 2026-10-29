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
EXPECTED = ['--ntasks=1','bash','scripts/run_live_stack.sh','--profile','quality-3gpu']


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
record={'argv':args,'cwd':os.getcwd(),'unbuffered':os.environ.get('PYTHONUNBUFFERED')}
with open(os.environ['TEST_SRUN_RECORD'],'a') as stream: stream.write(json.dumps(record)+'\\n')
if args!=['--ntasks=1','bash','scripts/run_live_stack.sh','--profile','quality-3gpu']: sys.exit(98)
if pathlib.Path.cwd()!=pathlib.Path(os.environ['SLURM_SUBMIT_DIR']): sys.exit(97)
sys.exit(subprocess.run(args[1:]).returncode)
''')
        fake_srun.chmod(0o755)
        self.stack = self.checkout/'scripts'/'run_live_stack.sh'
        self.stack.write_text('''#!/usr/bin/env bash
set -euo pipefail
"$TEST_PYTHON" - "$@" <<'PYTEST'
import json,os,sys
with open(os.environ['TEST_STACK_RECORD'],'a') as stream:
    stream.write(json.dumps({'argv':sys.argv[1:],'cwd':os.getcwd(),'unbuffered':os.environ.get('PYTHONUNBUFFERED')})+'\\n')
PYTEST
exit "${TEST_STACK_EXIT:-0}"
''')
        self.environment = dict(os.environ)
        self.environment.update({'PATH':str(self.commands)+os.pathsep+os.defpath,
            'SLURM_JOB_ID':'12345','SLURM_SUBMIT_DIR':str(self.checkout),
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
        self.assertEqual(records[0]['argv'],EXPECTED)
        self.assertEqual(self.records(self.stack_record)[0]['argv'],['--profile','quality-3gpu'])
        self.assertNotIn('--overlap',records[0]['argv'])
        self.assertNotIn('--prepare',records[0]['argv'])


if __name__ == '__main__': unittest.main()
