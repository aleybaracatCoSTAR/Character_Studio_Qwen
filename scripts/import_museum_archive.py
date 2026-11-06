"""Import an artist/title/subject selection from official museum catalogues."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from live_voice.collections import CollectionDatabase
from live_voice.museum_common import MAX_MUSEUM_RECORDS, SOURCES, MuseumImportError
from live_voice.museum_import import MuseumImporter, museum_error


def default_directory():
    return Path(os.environ.get('LIVE_ARCHIVE_DIR') or
        str(Path(os.environ.get('LIVE_VOICE_STORE') or Path.home()/'.local/share/character-studio')/'archive'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',choices=[*SOURCES,'all'],required=True)
    parser.add_argument('--query',required=True,help='An artist, artwork title or subject. Not a full-catalogue harvest.')
    parser.add_argument('--limit',type=int,default=100,help='Maximum matching records per museum (1–1000; default100).')
    parser.add_argument('--archive-dir',type=Path,default=None)
    args = parser.parse_args(argv)
    if not 1<=args.limit<=MAX_MUSEUM_RECORDS: parser.error('--limit must be between 1 and 1000.')
    if not 1<=len(args.query.strip())<=200 or any(ord(char)<32 or ord(char)==127 for char in args.query):
        parser.error('--query must be plain text between 1 and 200 characters.')
    store = None
    errors = []
    try:
        store = CollectionDatabase(args.archive_dir or default_directory())
        importer = MuseumImporter()
        for provider in SOURCES if args.source=='all' else [args.source]:
            print('Importing '+SOURCES[provider].label+' catalogue selection…',flush=True)
            try:
                result = importer.import_collection(store,provider,args.query,args.limit)
                # Fixed counters and provider IDs; no artwork or query text.
                print(json.dumps({key:result[key] for key in ('source','added','duplicates','total','limit','requests','imported_scope')},sort_keys=True),flush=True)
            except MuseumImportError as error:
                diagnostic = museum_error(error,provider)
                errors.append(diagnostic); print(json.dumps(diagnostic,sort_keys=True),file=sys.stderr,flush=True)
        return 1 if errors else 0
    except (OSError,sqlite3.Error):
        print('The museum collection storage could not be opened or written.',file=sys.stderr)
        return 1
    finally:
        if store is not None: store.close()


if __name__=='__main__': raise SystemExit(main())
