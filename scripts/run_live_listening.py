"""Launch isolated Nemotron/Smart Turn on the director's allocated GPU."""
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from live_voice.deployment import assigned_devices, profile
from live_voice.listening_config import EXPERIMENT, PORT, listening_mode, listening_profile


def main():
    if listening_mode() != EXPERIMENT:
        raise SystemExit('Select --listening nemotron-smart-turn in the live stack launcher.')
    config = listening_profile(profile())
    devices = assigned_devices(config,os.environ.get('CUDA_VISIBLE_DEVICES',''))
    store = Path(os.environ.get('LISTENING_STORE') or str(Path(os.environ['LIVE_VOICE_STORE'])/'listening'))
    env = os.environ.copy()
    env['CUDA_VISIBLE_DEVICES'] = devices['director']
    env.setdefault('OMP_NUM_THREADS','4')
    command = [sys.executable,'-m','listening_runtime','--store',str(store),'--port',str(PORT)]
    print('Experimental listening: Nemotron + Smart Turn; allocated GPU '+devices['director']+
          '; director memory budget 75%.',flush=True)
    os.execve(sys.executable,command,env)


if __name__ == '__main__': main()
