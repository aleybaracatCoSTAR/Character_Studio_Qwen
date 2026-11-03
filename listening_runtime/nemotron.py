"""Bounded incoming-audio generator for Transformers' cache-aware RNNT API.

Primary API: Transformers v5.13.0 Nemotron ASR Streaming documentation and
generation_nemotron_asr_streaming.py. The model, dependencies and checkpoint
downloads belong to the separate listening environment, never the voice stack.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import threading
import time

SAMPLE_RATE = 16_000
MAX_AUDIO_BYTES = 15 * SAMPLE_RATE * 2
MAX_QUEUED_BYTES = 3 * SAMPLE_RATE * 2
MAX_TEXT = 4000
CHUNK_MS = 560
LOOKAHEAD = 6
TRANSFORMERS_VERSION = '5.13.0'
MODEL_ID = 'nvidia/nemotron-speech-streaming-en-0.6b'
MODEL_REVISION = 'ebe59e5a817142986528bbbee5dba8db7b38ed50'


class ListeningError(RuntimeError):
    MESSAGES = {
        'not_ready': 'The listening models are not ready.',
        'busy': 'Another listening utterance is active.',
        'invalid_start': 'Start one 16 kHz listening utterance first.',
        'invalid_packet': 'Send 20–80 ms aligned mono PCM16 audio packets.',
        'audio_limit': 'A listening utterance can contain at most 15 seconds of audio.',
        'queue_limit': 'Audio arrived faster than the listening model can process it.',
        'transcript_limit': 'The listening transcript exceeded its limit.',
        'timeout': 'The listening utterance timed out.',
        'cancelled': 'The listening utterance was cancelled.',
        'inference_failed': 'The listening model could not transcribe this utterance.',
        'invalid_control': 'Finish or cancel the current listening utterance.',
        'invalid_turn_audio': 'Send up to 15 seconds of aligned 16 kHz mono PCM16 audio.',
        'turn_failed': 'The turn-completion model could not check this audio.',
        'checkpoint_invalid': 'The local listening checkpoint does not match the pinned download.',
    }

    def __init__(self, code):
        self.code = code if code in self.MESSAGES else 'inference_failed'
        super().__init__(self.MESSAGES[self.code])


@dataclass(frozen=True)
class ChunkGeometry:
    first_samples: int
    subsequent_samples: int
    first_mel_frames: int
    subsequent_mel_frames: int
    hop_length: int
    n_fft: int

    @classmethod
    def from_processor(cls, processor):
        geometry = cls(processor.num_samples_first_audio_chunk, processor.num_samples_per_audio_chunk,
                       processor.num_mel_frames_first_audio_chunk, processor.num_mel_frames_per_audio_chunk,
                       processor.feature_extractor.hop_length, processor.feature_extractor.n_fft)
        if any(type(value) is not int or value <= 0 for value in geometry.__dict__.values()):
            raise ListeningError('checkpoint_invalid')
        return geometry


class PCMStream:
    """A bounded current utterance, read incrementally by one model thread."""
    def __init__(self, idle_timeout=20):
        self._condition = threading.Condition()
        self._audio = bytearray()
        self._consumed = 0
        self._finished = False
        self._cancelled = False
        self._last_packet = time.monotonic()
        self.idle_timeout = idle_timeout

    def feed(self, pcm):
        if not isinstance(pcm, bytes) or not 640 <= len(pcm) <= 2560 or len(pcm) % 2:
            raise ListeningError('invalid_packet')
        with self._condition:
            if self._finished or self._cancelled:
                raise ListeningError('invalid_control')
            if len(self._audio) + len(pcm) > MAX_AUDIO_BYTES:
                raise ListeningError('audio_limit')
            if len(self._audio) - self._consumed + len(pcm) > MAX_QUEUED_BYTES:
                raise ListeningError('queue_limit')
            self._audio.extend(pcm)
            self._last_packet = time.monotonic()
            self._condition.notify_all()

    def finish(self):
        with self._condition:
            if self._finished or self._cancelled:
                raise ListeningError('invalid_control')
            self._finished = True
            self._condition.notify_all()

    def cancel(self):
        with self._condition:
            self._cancelled = True
            self._condition.notify_all()

    def clear(self):
        with self._condition:
            self._audio.clear()

    def window(self, start, samples, new_audio_start):
        """Wait for a full window; on finish pad only the final incomplete one.

        ``new_audio_start`` excludes STFT overlap when deciding whether another
        chunk is needed. Any final real samples still get a padded model chunk.
        """
        end = (start + samples) * 2
        with self._condition:
            while len(self._audio) < end and not self._finished and not self._cancelled:
                remaining = self.idle_timeout - (time.monotonic() - self._last_packet)
                if remaining <= 0:
                    raise ListeningError('timeout')
                self._condition.wait(min(remaining, 1))
            if self._cancelled:
                raise ListeningError('cancelled')
            if self._finished and new_audio_start * 2 >= len(self._audio):
                return None
            value = bytes(self._audio[start * 2:end])
            self._consumed = max(self._consumed, min(end, len(self._audio)))
            return value + b'\0' * (samples * 2 - len(value))

    def windows(self, geometry):
        first = self.window(0, geometry.first_samples, 0)
        if first is None:
            return
        yield first, True
        mel_index = geometry.first_mel_frames
        while True:
            new_audio_start = mel_index * geometry.hop_length
            start = new_audio_start - geometry.n_fft // 2
            if start < 0:
                raise ListeningError('checkpoint_invalid')
            value = self.window(start, geometry.subsequent_samples, new_audio_start)
            if value is None:
                return
            yield value, False
            mel_index += geometry.subsequent_mel_frames


def verify_pin(path, model_id, revision, *, filename=None):
    """Require downloader provenance; all model/processor loading stays offline."""
    marker = path.with_name(path.name + '.json') if filename else path / 'listening-checkpoint.json'
    try:
        if marker.stat().st_size > 128 * 1024:
            raise ValueError()
        info = json.loads(marker.read_text(encoding='utf-8'))
        if (info.get('schema') != 1 or info.get('model_id') != model_id or info.get('revision') != revision
                or (filename is not None and info.get('filename') != filename)):
            raise ValueError()
    except (OSError, ValueError, TypeError, AttributeError):
        raise ListeningError('checkpoint_invalid') from None


class NemotronRecognizer:
    def __init__(self, directory, device='cuda:0'):
        self.directory = Path(directory)
        self.device = device
        self.model = self.processor = self.geometry = None

    @property
    def ready(self):
        return self.model is not None and self.processor is not None

    def load(self):
        from importlib.metadata import version
        if version('transformers') != TRANSFORMERS_VERSION:
            raise ListeningError('checkpoint_invalid')
        verify_pin(self.directory, MODEL_ID, MODEL_REVISION)
        import torch
        from transformers import AutoModelForRNNT, AutoProcessor
        processor = AutoProcessor.from_pretrained(str(self.directory), local_files_only=True, trust_remote_code=False)
        processor.set_num_lookahead_tokens(LOOKAHEAD)
        if processor.streaming_latency_ms != CHUNK_MS:
            raise ListeningError('checkpoint_invalid')
        model = AutoModelForRNNT.from_pretrained(str(self.directory), local_files_only=True,
                                               trust_remote_code=False, dtype=torch.float32).to(self.device).eval()
        self.geometry = ChunkGeometry.from_processor(processor)
        self.processor, self.model = processor, model

    def transcribe(self, audio, partial):
        if not self.ready:
            raise ListeningError('not_ready')
        import numpy as np
        import torch
        windows = audio.windows(self.geometry)
        first = next(windows, None)
        if first is None:
            return ''
        processor, model = self.processor, self.model

        def input_features_generator():
            # A real Python generator is required by the pinned Transformers API.
            item = first
            while item is not None:
                pcm, initial = item
                samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
                inputs = processor(samples, sampling_rate=SAMPLE_RATE, is_streaming=True,
                                   is_first_audio_chunk=initial, return_tensors='pt')
                features = inputs.input_features
                if initial:
                    features = features[:, : self.geometry.first_mel_frames, :]
                yield features.to(device=model.device, dtype=model.dtype)
                item = next(windows, None)

        class CumulativeStreamer:
            def __init__(self):
                self.ids = []
                self.last = ''

            def put(self, token_ids):
                values = token_ids.tolist()
                if values and isinstance(values[0], list):
                    if len(values) != 1:
                        raise ListeningError('inference_failed')
                    values = values[0]
                self.ids.extend(values)
                if len(self.ids) > 4096:
                    raise ListeningError('transcript_limit')
                # The processor disables CTC-style repeated-token grouping for
                # RNNT. Tokenizer.decode's default would lose real repetitions.
                text = processor.decode(self.ids, skip_special_tokens=True)
                if len(text) > MAX_TEXT:
                    raise ListeningError('transcript_limit')
                if text != self.last:
                    self.last = text
                    partial(text)

            def end(self):
                pass

        with torch.inference_mode():
            result = model.generate(input_features=input_features_generator(), num_lookahead_tokens=LOOKAHEAD,
                                    streamer=CumulativeStreamer(), max_new_tokens=4096, do_sample=False)
        text = processor.batch_decode(result.sequences, skip_special_tokens=True)[0].strip()
        if len(text) > MAX_TEXT:
            raise ListeningError('transcript_limit')
        return text

    def close(self):
        self.model = self.processor = self.geometry = None
