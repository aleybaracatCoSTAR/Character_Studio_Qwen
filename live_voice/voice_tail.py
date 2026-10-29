"""Conservative low-energy tail analysis, not proof that speech is complete.

A caller must separately verify recognized words before accepting a capped take.
Very quiet speech and noise are not perfectly distinguishable by energy alone.
"""
from dataclasses import dataclass

import numpy as np


RATE = 44100
FRAME_SAMPLES = 882  # Exactly 20 ms at the native output rate.
MAX_PCM_BYTES = 8 * 1024 * 1024
MIN_TAIL_SAMPLES = RATE * 800 // 1000
MIN_VOICE_SAMPLES = RATE * 200 // 1000
PAD_SAMPLES = RATE * 400 // 1000


@dataclass(frozen=True)
class TailReport:
    trailing_silence_ms: float
    speech_detected: bool
    clean_ending: bool
    total_samples: int
    voiced_ms: float
    last_non_silent_sample: int

    def safe_metadata(self):
        return {'trailing_silence_ms':self.trailing_silence_ms,
                'speech_detected':self.speech_detected, 'clean_ending':self.clean_ending,
                'total_samples':self.total_samples, 'voiced_ms':self.voiced_ms,
                'last_non_silent_sample':self.last_non_silent_sample}


def _samples(pcm):
    if not isinstance(pcm,(bytes,bytearray,memoryview)):
        raise ValueError('Use native-rate mono PCM16 audio for tail analysis.')
    size = pcm.nbytes if isinstance(pcm,memoryview) else len(pcm)
    if not size or size%2 or size>MAX_PCM_BYTES:
        raise ValueError('Use at most 8 MiB of nonempty, aligned mono PCM16 audio.')
    return np.frombuffer(bytes(pcm),dtype='<i2')


def analyze_tail(pcm):
    """Require varying earlier energy and at least 800 ms of strict quiet tail."""
    samples = _samples(pcm)
    total = len(samples)
    full_count,remainder = divmod(total,FRAME_SAMPLES)
    rms = []
    peak = []
    deviation = []
    lengths = []
    if full_count:
        blocks = samples[:full_count*FRAME_SAMPLES].reshape(full_count,FRAME_SAMPLES).astype(np.float64)
        rms.extend(np.sqrt(np.mean(blocks*blocks,axis=1)))
        peak.extend(np.max(np.abs(blocks),axis=1))
        deviation.extend(np.std(blocks,axis=1))
        lengths.extend([FRAME_SAMPLES]*full_count)
    if remainder:
        block = samples[-remainder:].astype(np.float64)
        rms.append(float(np.sqrt(np.mean(block*block))))
        peak.append(float(np.max(np.abs(block))))
        deviation.append(float(np.std(block)))
        lengths.append(remainder)
    # Both conditions must hold. A small average with an isolated larger peak
    # is ambiguous, so retain that entire block and begin the pad after it.
    quiet = (np.asarray(rms)<=30) & (np.asarray(peak)<=120)
    non_silent = np.flatnonzero(~quiet)
    last = min((int(non_silent[-1])+1)*FRAME_SAMPLES,total) if non_silent.size else 0
    trailing = total-last
    # Reject DC offsets, low-level breaths and clicks as speech evidence. This
    # is deliberately conservative; varying energy alone is not an ASR/VAD test.
    voice = (np.asarray(rms)>=150) & (np.asarray(deviation)>=120)
    voiced_samples = int(np.asarray(lengths,dtype=np.int64)[voice].sum())
    detected = voiced_samples>=MIN_VOICE_SAMPLES
    return TailReport(trailing_silence_ms=round(trailing*1000/RATE,3),
                      speech_detected=bool(detected),
                      clean_ending=bool(detected and trailing>=MIN_TAIL_SAMPLES),
                      total_samples=total,voiced_ms=round(voiced_samples*1000/RATE,3),
                      last_non_silent_sample=last)


def trim_tail(pcm,report=None):
    """Keep every nonquiet block plus 400 ms; uncertain endings are untouched.

    Re-analyse rather than trusting a report for different audio. This bounded
    check cannot remove a voiced block merely because a stale report was reused.
    """
    current = analyze_tail(pcm)
    original = bytes(pcm)
    if report is not None and report!=current: return original
    if not current.clean_ending: return original
    end = min(current.total_samples,current.last_non_silent_sample+PAD_SAMPLES)
    return original[:end*2]
