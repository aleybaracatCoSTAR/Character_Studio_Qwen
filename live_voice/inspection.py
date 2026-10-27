"""Inspect submitted model inputs in the current call, without storing audio.

Capture happens at the provider boundary, from the same JSON passed to HTTP.
Only an explicitly subscribed browser receives snapshots. Context-local scopes
keep concurrent tasks/calls separate; nothing is written to disk or logs.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import copy
import json
import logging

logger = logging.getLogger(__name__)
_current = ContextVar('live_model_inspection', default=None)
MAX_REQUESTS = 24
MAX_TURNS = 5
MAX_TOTAL_CHARS = 256_000
MAX_REQUEST_CHARS = 128_000
STAGES = frozenset(('actor', 'director', 'trigger', 'tts'))


class RequestInspection:
    def __init__(self, send_json):
        self.send_json = send_json
        self.requests = []
        self.subscribed = False
        self.closed = False
        self._next_id = 1

    @contextmanager
    def scope(self, turn_id, phase='reply'):
        token = _current.set((self, turn_id, phase))
        try:
            yield
        finally:
            _current.reset(token)

    def snapshot(self):
        return {'type':'model_inspection', 'requests':copy.deepcopy(self.requests)}

    async def subscribe(self, enabled):
        if type(enabled) is not bool: raise ValueError('Choose whether to show model inputs.')
        if self.closed: return
        self.subscribed = enabled
        if enabled: await self.send_json(self.snapshot())

    async def record(self, stage, payload):
        if self.closed: return
        if stage not in STAGES: raise ValueError('Unknown model stage.')
        scope = _current.get()
        turn_id, phase = (scope[1], scope[2]) if scope and scope[0] is self else (0, 'reply')
        # Copy only JSON body fields. No request headers, endpoint URLs, API
        # credentials or microphone buffers are accepted by this inspector.
        safe = dict(payload)
        reference = safe.pop('ref_audio', None)
        entry = {'id':self._next_id, 'turn_id':turn_id, 'phase':phase,
                 'stage':stage, 'model':safe.get('model', ''), 'payload':safe}
        if reference is not None:
            entry['omitted_fields'] = ['ref_audio']
            entry['reference_audio'] = {'present':True,
                                        'encoded_characters':len(reference) if isinstance(reference, str) else None}
        # JSON round-trip snapshots mutable messages and prevents later model
        # state edits from rewriting an earlier request in the inspector.
        encoded = json.dumps(entry, ensure_ascii=False)
        if len(encoded) > MAX_REQUEST_CHARS:
            entry = {key:value for key,value in entry.items() if key != 'payload'}
            entry['payload'] = {}
            entry['notice'] = 'This request exceeds the inspector size limit; its input was not retained.'
            encoded = json.dumps(entry, ensure_ascii=False)
        self.requests.append(json.loads(encoded))
        self._next_id += 1
        turns = sorted({item['turn_id'] for item in self.requests})
        if len(turns) > MAX_TURNS:
            keep = set(turns[-MAX_TURNS:])
            self.requests = [item for item in self.requests if item['turn_id'] in keep]
        while (len(self.requests) > MAX_REQUESTS
               or sum(len(json.dumps(item, ensure_ascii=False)) for item in self.requests) > MAX_TOTAL_CHARS):
            self.requests.pop(0)
        if self.subscribed: await self.send_json(self.snapshot())

    def clear(self):
        self.requests.clear()

    def close(self):
        self.closed = True
        self.subscribed = False
        self.clear()


async def record_request(stage, payload):
    scope = _current.get()
    if scope is None: return
    try:
        await scope[0].record(stage, payload)
    except Exception as exc:
        # Inspection must not stop an otherwise valid reply. Never include its
        # inputs in diagnostics. Cancellation still propagates normally.
        logger.warning('Model input inspection unavailable (%s).', type(exc).__name__)
