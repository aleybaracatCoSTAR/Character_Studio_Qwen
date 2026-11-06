"""Explicit per-call engine selection; the baseline remains the default."""
ENGINE_NAMES = ('baseline','qwen-omni')


def engine_name(value=None, default='baseline'):
    value = default if value is None else value
    if not isinstance(value,str) or value not in ENGINE_NAMES:
        raise ValueError('Choose Current Qwen + Drama 3 or Streaming Qwen3-Omni.')
    return value
