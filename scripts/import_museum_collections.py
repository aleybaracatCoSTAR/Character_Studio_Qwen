#!/usr/bin/env python3
"""Import complete official museum metadata snapshots, with resumable batches."""
from pathlib import Path
import argparse
import json
import os
import signal
import sys
import threading

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from live_voice.collections import CollectionDatabase
from live_voice.museum_bulk import run_museum_import
from live_voice.museum_common import SOURCES


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', choices=['all',*SOURCES], default='all')
    parser.add_argument('--archive-dir', type=Path, default=Path(os.environ.get('LIVE_ARCHIVE_DIR') or
        str(Path(os.environ.get('LIVE_VOICE_STORE',str(Path.home()/'.local/share/character-studio')))/'archive')))
    parser.add_argument('--refresh', action='store_true', help='Restart source enumeration with a new official snapshot; saved records are updated, not deleted.')
    args = parser.parse_args(argv)
    stop = threading.Event()
    def pause(signum,frame): stop.set()
    signal.signal(signal.SIGINT,pause)
    signal.signal(signal.SIGTERM,pause)
    store = CollectionDatabase(args.archive_dir)
    def progress(event):
        print(json.dumps({key:event.get(key) for key in ('current_source','status','processed','total_expected','message')},ensure_ascii=False),flush=True)
    try:
        result=run_museum_import(store,args.source,progress=progress,stop=stop,refresh=args.refresh)
        print(json.dumps(result,ensure_ascii=False),flush=True)
        return 0 if result['status']=='completed' else 2 if result['status']=='paused' else 1
    finally: store.close()


if __name__=='__main__': raise SystemExit(main())
