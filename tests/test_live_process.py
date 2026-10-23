"""Exercise shutdown with real CPU worker processes and sockets, without loading models."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from scripts.live_process import stop_groups

HELPER = Path(__file__).resolve().parents[1] / "scripts" / "live_process.py"
WORKER = '''
import json,os,signal,socket,sys,time
s=socket.socket();s.bind(('127.0.0.1',0));s.listen()
if sys.argv[2]=='ignore': signal.signal(signal.SIGTERM,signal.SIG_IGN)
with open(sys.argv[1],'w') as f: json.dump({'pid':os.getpid(),'port':s.getsockname()[1]},f)
while True: time.sleep(1)
'''
PARENT = '''
import signal,subprocess,sys,time
child=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2],sys.argv[3]])
def stop(*args):
    child.wait();sys.exit(0)
signal.signal(signal.SIGTERM,stop)
while True: time.sleep(1)
'''


@unittest.skipUnless(os.name=='posix','Live GPU launch uses POSIX process groups')
class LiveProcessTests(unittest.TestCase):
    def wait_worker(self, path):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            try: return json.loads(path.read_text())
            except (FileNotFoundError,json.JSONDecodeError): time.sleep(.02)
        self.fail('Test worker did not start')

    def listening(self, port):
        with socket.socket() as probe:
            probe.settimeout(.2)
            return probe.connect_ex(('127.0.0.1',port))==0

    def test_shutdown_releases_worker_port_without_stopping_other_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            worker_file=Path(directory)/'worker.json';other_file=Path(directory)/'other.json'
            parent=subprocess.Popen([sys.executable,str(HELPER),'start',sys.executable,'-c',PARENT,WORKER,str(worker_file),'normal'])
            other=subprocess.Popen([sys.executable,'-c',WORKER,str(other_file),'normal'])
            try:
                worker=self.wait_worker(worker_file);unrelated=self.wait_worker(other_file)
                self.assertTrue(self.listening(worker['port']))
                stopper=subprocess.Popen([sys.executable,str(HELPER),'stop',str(parent.pid),'--timeout','2'])
                parent.wait(timeout=5)
                self.assertEqual(stopper.wait(timeout=5),0)
                self.assertFalse(self.listening(worker['port']))
                self.assertIsNone(other.poll());self.assertTrue(self.listening(unrelated['port']))
            finally:
                if parent.poll() is None:
                    os.killpg(parent.pid,signal.SIGKILL);parent.wait(timeout=5)
                other.terminate();other.wait(timeout=5)

    def test_unresponsive_owned_worker_is_stopped_after_grace_period(self):
        with tempfile.TemporaryDirectory() as directory:
            worker_file=Path(directory)/'worker.json'
            parent=subprocess.Popen([sys.executable,str(HELPER),'start',sys.executable,'-c',PARENT,WORKER,str(worker_file),'ignore'])
            try:
                worker=self.wait_worker(worker_file)
                stopper=subprocess.Popen([sys.executable,str(HELPER),'stop',str(parent.pid),'--timeout','.2'],stdout=subprocess.PIPE,text=True)
                parent.wait(timeout=5)
                output,_=stopper.communicate(timeout=5)
                self.assertEqual(stopper.returncode,0)
                self.assertIn('remaining workers',output)
                self.assertFalse(self.listening(worker['port']))
            finally:
                if parent.poll() is None:
                    os.killpg(parent.pid,signal.SIGKILL);parent.wait(timeout=5)

    def test_shared_shell_group_and_invalid_groups_are_never_signalled(self):
        for group in (0,1,-1,os.getpgrp()):
            with self.subTest(group=group), self.assertRaises(ValueError): stop_groups([group])


if __name__=='__main__': unittest.main()
