"""Describe when Drama audio is released without changing native Omni routing."""


def streams_voice(providers):
    return getattr(providers, 'voice_playback', 'checked') == 'streaming'


def voice_pipeline(providers):
    return 'drama-stream-v1' if streams_voice(providers) else 'voice-checked-v5'


def verification_before_playback(providers):
    return not streams_voice(providers) and bool(getattr(providers, 'verify_voice', False))
