"""Small PCM WAV inputs, resampled locally. No external speech API or ffmpeg."""
from io import BytesIO
from math import gcd
import wave

import numpy as np
from scipy.signal import resample_poly

MAX_AUDIO_BYTES = 6 * 1024 * 1024
MAX_AUDIO_SECONDS = 30
INPUT_RATE = 16000
OUTPUT_RATE = 24000


def read_recording(data: bytes) -> np.ndarray:
    if not data or len(data) > MAX_AUDIO_BYTES:
        raise ValueError("Record a WAV clip of up to 30 seconds (maximum 6 MB).")
    try:
        with wave.open(BytesIO(data), "rb") as recording:
            rate, channels, width = recording.getframerate(), recording.getnchannels(), recording.getsampwidth()
            frames = recording.getnframes()
            if width != 2 or channels not in (1, 2) or not 8000 <= rate <= 96000:
                raise ValueError("Use a mono or stereo, 16-bit PCM WAV recording.")
            if not 0 < frames <= rate * MAX_AUDIO_SECONDS:
                raise ValueError("Record between a fraction of a second and 30 seconds.")
            raw = recording.readframes(frames)
            if len(raw) != frames * channels * width:
                raise ValueError("The WAV recording is incomplete.")
    except (wave.Error, EOFError) as exc:
        raise ValueError("This recording is not a supported PCM WAV file.") from exc
    audio = np.frombuffer(raw, dtype="<i2").astype(np.float32).reshape(-1, channels).mean(axis=1) / 32768
    if rate != INPUT_RATE:
        factor = gcd(rate, INPUT_RATE)
        audio = resample_poly(audio, INPUT_RATE // factor, rate // factor).astype(np.float32)
    return audio


def write_reply(audio: np.ndarray) -> bytes:
    import soundfile as sf
    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    if not len(samples) or not np.isfinite(samples).all():
        raise ValueError("The model did not return valid audio.")
    buffer = BytesIO()
    sf.write(buffer, np.clip(samples, -1, 1), OUTPUT_RATE, format="WAV", subtype="PCM_16")
    return buffer.getvalue()
