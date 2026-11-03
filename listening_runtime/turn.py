"""Pinned Smart Turn 3.2 CPU inference, without importing the training stack."""
from pathlib import Path
import math

from .nemotron import ListeningError, MAX_AUDIO_BYTES, SAMPLE_RATE, verify_pin

MODEL_ID = 'pipecat-ai/smart-turn-v3'
MODEL_REVISION = 'f766f81d3cfdf7737ac64aad813d91bbfd56bf93'
MODEL_FILENAME = 'smart-turn-v3.2-cpu.onnx'


class SmartTurn:
    def __init__(self, path):
        self.path = Path(path)
        self.session = self.extractor = None

    @property
    def ready(self):
        return self.session is not None and self.extractor is not None

    def load(self):
        verify_pin(self.path, MODEL_ID, MODEL_REVISION, filename=MODEL_FILENAME)
        import onnxruntime as ort
        from transformers import WhisperFeatureExtractor
        options = ort.SessionOptions()
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(str(self.path), sess_options=options, providers=['CPUExecutionProvider'])
        self.extractor = WhisperFeatureExtractor(chunk_length=8)

    def predict(self, pcm):
        if not self.ready:
            raise ListeningError('not_ready')
        if not isinstance(pcm, bytes) or not 0 < len(pcm) <= MAX_AUDIO_BYTES or len(pcm) % 2:
            raise ListeningError('invalid_turn_audio')
        import numpy as np
        samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
        samples = samples[-8 * SAMPLE_RATE:]
        if len(samples) < 8 * SAMPLE_RATE:
            samples = np.pad(samples, (8 * SAMPLE_RATE - len(samples), 0))
        inputs = self.extractor(samples, sampling_rate=SAMPLE_RATE, return_tensors='np',
                                padding='max_length', max_length=8 * SAMPLE_RATE,
                                truncation=True, do_normalize=True)
        features = np.asarray(inputs.input_features, dtype=np.float32)
        probability = float(np.asarray(self.session.run(None, {'input_features': features})[0]).reshape(-1)[0])
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ListeningError('turn_failed')
        complete = probability > .5
        return {'complete': complete, 'probability': probability, 'prediction': int(complete)}

    def close(self):
        self.session = self.extractor = None
