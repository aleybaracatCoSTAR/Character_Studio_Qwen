"""Per-turn voice-stage feedback containing counters, never speech or audio."""
from contextlib import contextmanager
from contextvars import ContextVar
import inspect


_observer = ContextVar('voice_progress_observer', default=None)
STAGES = frozenset(('generating', 'checking', 'retrying', 'ready'))
COUNTERS = frozenset(('attempt', 'synthesis_ms', 'verification_ms', 'first_audio_ms'))


def voice_timing_metrics(values):
    if not isinstance(values, dict):
        return {}
    names = {'attempt':'voice_attempts', 'synthesis_ms':'voice_synthesis_ms',
             'verification_ms':'voice_verification_ms',
             'first_audio_ms':'voice_model_first_audio_ms'}
    return {names[key]: value for key, value in values.items()
            if key in names and type(value) is int and 0 <= value <= 3_600_000}


@contextmanager
def observe_voice_progress(callback):
    token = _observer.set(callback)
    try:
        yield
    finally:
        _observer.reset(token)


async def report_voice_progress(stage, **values):
    callback = _observer.get()
    if callback is None or stage not in STAGES:
        return
    event = {'stage': stage}
    event.update({key: value for key, value in values.items()
                  if key in COUNTERS and type(value) is int and 0 <= value <= 3_600_000})
    result = callback(event)
    if inspect.isawaitable(result):
        await result
