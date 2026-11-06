"""One background full-catalogue import, with durable, metadata-only progress."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import threading
import uuid

from .museum_common import SOURCES, FULL_MUSEUM_ORDER


_COUNTERS = ('processed','added','updated','duplicates','images','requests','download_bytes')
_STATUSES = {'running','completed','paused','failed'}


class MuseumJobs:
    def __init__(self, store, runner=None):
        self.store, self.runner = store, runner
        self.path = Path(store.directory)/'museum-import-job.json'
        self._lock = threading.RLock()
        self._thread = None
        self._stop = threading.Event()
        self._job = None
        try:
            if self.path.stat().st_size <= 128*1024:
                value = json.loads(self.path.read_text())
                if (isinstance(value,dict) and isinstance(value.get('id'),str)
                        and len(value['id']) == 32 and value.get('status') in _STATUSES):
                    self._job = value
                    if value['status'] == 'running':
                        value.update(status='paused',message='Import paused when the server stopped. Resume to continue saved progress.')
                        self._save()
        except (OSError,ValueError):
            pass

    def _save(self):
        temporary = self.path.with_suffix('.tmp')
        with temporary.open('w',encoding='utf-8') as stream:
            os.chmod(temporary,0o600)
            json.dump(self._job,stream,ensure_ascii=False,separators=(',',':'))
        temporary.replace(self.path)

    def current(self):
        with self._lock:
            return copy.deepcopy(self._job)

    @property
    def running(self):
        with self._lock:
            return bool(self._job and self._job['status'] == 'running')

    def start(self, source):
        if not isinstance(source,str) or source not in (*SOURCES,'all'):
            raise ValueError('Choose a supported museum.')
        with self._lock:
            if self.running: raise RuntimeError('An import is already running. Stop it before starting another.')
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError('The previous import is still stopping. Try again in a moment.')
            sources = list(FULL_MUSEUM_ORDER) if source == 'all' else [source]
            self._stop = threading.Event()
            self._job = {'id':uuid.uuid4().hex,'status':'running','source':source,'sources':sources,
                'current_source':sources[0],'processed':0,'added':0,'updated':0,'duplicates':0,
                'images':0,'total_expected':None,'results':[],'errors':[],
                'message':'Preparing the published catalogue. Existing checkpoints will be resumed.'}
            self._save()
            self._thread = threading.Thread(target=self._run,args=(self._job['id'],source),
                                            name='museum-collection-import',daemon=True)
            self._thread.start()
            return self.current()

    def _progress(self, identity, values):
        if not isinstance(values,dict): return
        with self._lock:
            if not self._job or self._job['id'] != identity: return
            for key in _COUNTERS:
                value = values.get(key)
                if type(value) is int and 0 <= value <= 10**13: self._job[key] = value
            current = values.get('current_source',values.get('source'))
            if current in SOURCES: self._job['current_source'] = current
            expected = values.get('total_expected')
            if expected is None or type(expected) is int and 0 <= expected <= 10**8:
                self._job['total_expected'] = expected
            # The worker supplies fixed messages and counters, never source bodies.
            message = values.get('message')
            if isinstance(message,str): self._job['message'] = message[:500]
            for key in ('results','errors'):
                if isinstance(values.get(key),list): self._job[key] = copy.deepcopy(values[key][:5])
            self._save()

    def _run(self, identity, source):
        try:
            if self.runner is None:
                from .museum_bulk import run_museum_import
                runner = run_museum_import
            else: runner = self.runner
            result = runner(self.store,source,progress=lambda values:self._progress(identity,values),stop=self._stop)
            if not isinstance(result,dict): raise ValueError('Invalid import result.')
            self._progress(identity,result)
            status = result.get('status')
            status = 'completed' if status == 'complete' else status
            if status not in ('completed','paused','failed'): status = 'failed'
            message = {'completed':'Published collection import finished. Imported records are available to Robin.',
                       'paused':'Import paused. Resume to continue from the last saved batch.',
                       'failed':'The import stopped before completion. Saved records remain searchable; resume to retry.'}[status]
        except BaseException:
            status, message = 'failed','The import stopped before completion. Saved progress can be resumed; check the import log.'
        with self._lock:
            if self._job and self._job['id'] == identity:
                self._job.update(status=status,message=message)
                self._save()

    def stop(self, identity):
        with self._lock:
            if not self._job or identity != self._job['id']: raise KeyError('Unknown museum import.')
            if self._job['status'] == 'running':
                self._stop.set()
                self._job['message'] = 'Stopping after the current download or batch. Saved records will be kept.'
                self._save()
            return self.current()

    def close(self):
        self._stop.set()
        thread = self._thread
        if thread is not None: thread.join(timeout=65)
