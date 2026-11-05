import asyncio
import base64
import binascii
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from io import BytesIO
import json
import logging
import math
from pathlib import Path
import re
import threading
import time
import wave

import httpx
import numpy as np

from studio.characters import system_prompt
from .reactions import TriggerDecision, assessment_messages
from .director import DirectorPlan, director_messages
from .inspection import record_request
from .drama_sampling import REQUEST_SAMPLING
from .voice_coverage import resample_for_recognition, compare_speech, spoken_text, normalized_prefix
from .voice_tail import analyze_tail, trim_tail
from .voice_progress import report_voice_progress

OUTPUT_RATE = 44100
MAX_VOICE_PCM_BYTES = 8 * 1024 * 1024
MAX_VOICE_EVENT_CHARS = 12 * 1024 * 1024
logger = logging.getLogger(__name__)


VOICE_FAILURE_REASONS = frozenset(('incomplete', 'unfinished_event', 'malformed_event',
    'inconsistent_event', 'events_after_done', 'upstream_error', 'malformed_audio',
    'audio_limit', 'missing_usage', 'token_limit', 'unknown_event', 'missing_done',
    'empty_audio', 'unaligned_audio', 'content_type', 'pcm_format', 'wav_instead_of_pcm',
    'http_status', 'transport_timeout', 'transport_network', 'transport_protocol', 'service_unavailable',
    'speech_mismatch', 'verification_unavailable'))
_VOICE_NUMERIC_FIELDS = frozenset(('budget_tokens', 'output_tokens', 'audio_bytes', 'audio_chunks',
    'audio_seconds', 'audio_limit_bytes', 'events', 'elapsed_ms', 'sample_rate', 'channels',
    'pending_event_chars', 'http_status', 'attempt', 'attempts', 'verification_ms',
    'expected_words', 'heard_words', 'matched_words', 'coverage', 'trailing_silence_ms',
    'total_samples', 'voiced_ms', 'last_non_silent_sample', 'trimmed_audio_seconds', 'first_audio_ms',
    'removed_silence_ms', 'next_budget_tokens'))
_VOICE_ENUM_FIELDS = {
    'finish_reason': frozenset(('stop', 'length', 'eos', 'abort', 'cancelled', 'unknown', 'not_reported')),
    'service_type': frozenset(('server_error', 'invalid_request_error', 'rate_limit_error',
                              'internal_server_error', 'engine_error', 'error', 'unreported', 'other')),
    'service_action': frozenset(('discard', 'unreported', 'other')),
    'service_code': frozenset(('model_not_found', 'rate_limit_exceeded', 'context_length_exceeded',
                              'out_of_memory', 'generation_error', 'engine_dead', 'server_error')),
}


def _safe_voice_metadata(values):
    """Accept numeric counters and fixed enums, never arbitrary server messages."""
    safe = {}
    if not isinstance(values, dict): return safe
    for key, value in values.items():
        if key in _VOICE_NUMERIC_FIELDS and type(value) in (int, float):
            if 0 <= value <= 1_000_000_000 and math.isfinite(value): safe[key] = value
        elif key in ('done_received', 'service_partial_audio', 'speech_detected', 'clean_ending',
                     'ordered_prefix', 'active_ending', 'recovered_after_limit') and type(value) is bool:
            safe[key] = value
        elif key == 'service_code' and type(value) is int and 100 <= value <= 599:
            safe[key] = value
        elif key in _VOICE_ENUM_FIELDS and isinstance(value, str):
            if value in _VOICE_ENUM_FIELDS[key]: safe[key] = value
    return safe


class VoiceFailure(RuntimeError):
    """A fixed failure category plus counters safe for logs and the portal."""
    def __init__(self, message, *, reason='incomplete', metadata=None):
        super().__init__(message)
        self.reason = reason if isinstance(reason, str) and reason in VOICE_FAILURE_REASONS else 'incomplete'
        self.metadata = _safe_voice_metadata(metadata)

    def add_metadata(self, **values):
        self.metadata = _safe_voice_metadata({**self.metadata, **values})
        return self

    def diagnostic(self):
        reason = self.reason if isinstance(self.reason, str) and self.reason in VOICE_FAILURE_REASONS else 'incomplete'
        return {'reason':reason, **_safe_voice_metadata(self.metadata)}

    def safe_summary(self):
        data = self.diagnostic()
        labels = {'token_limit':'generation token limit reached', 'audio_limit':'generated audio exceeds the allowed duration',
                  'upstream_error':'voice service returned an error', 'missing_done':'completion event missing',
                  'missing_usage':'generation token usage missing', 'unfinished_event':'voice event ended prematurely',
                  'malformed_event':'voice event was malformed', 'malformed_audio':'audio data was malformed',
                  'empty_audio':'voice service returned no audio', 'unaligned_audio':'audio ended between samples',
                  'inconsistent_event':'voice event labels disagreed', 'events_after_done':'events arrived after completion',
                  'unknown_event':'unsupported voice event received', 'content_type':'voice completion protocol unavailable',
                  'pcm_format':'unsupported audio format', 'wav_instead_of_pcm':'WAV received instead of raw PCM',
                  'http_status':'voice service rejected the request', 'transport_timeout':'voice service timed out',
                  'transport_network':'voice service connection failed', 'transport_protocol':'voice connection ended incorrectly',
                  'service_unavailable':'voice service unavailable', 'incomplete':'voice take did not finish',
                  'speech_mismatch':'generated speech skipped a substantial part of the reply',
                  'verification_unavailable':'the generated speech could not be checked'}
        details = []
        if 'output_tokens' in data and 'budget_tokens' in data:
            details.append(f"{data['output_tokens']}/{data['budget_tokens']} tokens")
        if data.get('audio_seconds', 0) > 0: details.append(f"{data['audio_seconds']:.1f} s generated audio")
        if 'service_code' in data: details.append(f"service code {data['service_code']}")
        if 'http_status' in data: details.append(f"HTTP {data['http_status']}")
        if 'matched_words' in data and 'expected_words' in data:
            details.append(f"{data['matched_words']}/{data['expected_words']} words matched")
        if data.get('trailing_silence_ms',0) >= 800:
            details.append(f"{data['trailing_silence_ms']/1000:.1f} s quiet at end")
        return labels[data['reason']] + ' [' + data['reason'] + ']' + (' (' + ', '.join(details) + ')' if details else '')


class VoiceTakeIncomplete(VoiceFailure):
    """A take failed protocol, spoken-word or completion checks."""


@dataclass(frozen=True, repr=False)
class _VoiceTake:
    """Private validated transport result; PCM never belongs to an exception."""
    pcm: bytes
    limited: bool
    metadata: dict


class VoiceProtocolError(VoiceFailure):
    """A non-retryable format or request failure with safe diagnostics."""


class VoiceServiceUnavailable(VoiceFailure):
    """A transient voice-service failure remained after the bounded retry."""
    def __init__(self, *, reason='service_unavailable', metadata=None):
        super().__init__('The voice service is temporarily unavailable. Check that Drama 3 is ready, '
                         'then retry this reply. The chosen emotion and voice have not changed.',
                         reason=reason, metadata=metadata)


TRANSIENT_VOICE_STATUSES = frozenset((408, 429, 502, 503, 504))
TRANSIENT_VOICE_ERRORS = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)


async def speech_events(response):
    """Read complete SSE events, independently of HTTP packet boundaries."""
    event = None
    data = []
    size = 0
    async for line in response.aiter_lines():
        if not line:
            if data:
                yield event, '\n'.join(data)
            event = None; data = []; size = 0
        elif line.startswith(':'):
            continue
        else:
            name, separator, value = line.partition(':')
            if value.startswith(' '): value = value[1:]
            if name == 'event': event = value
            elif name == 'data' and separator:
                size += len(value)
                if size > MAX_VOICE_EVENT_CHARS:
                    raise VoiceTakeIncomplete('Voice event exceeded the live audio limit.', reason='audio_limit',
                                              metadata={'pending_event_chars':size})
                data.append(value)
    if data:
        raise VoiceTakeIncomplete('Drama 3 returned an unfinished voice event.', reason='unfinished_event',
                                  metadata={'pending_event_chars':size})


def speech_token_budget(text):
    """Allow unhurried speech to finish; delivery tags are not spoken words.

    The former 13 tokens/word with a 400-token ceiling was a hard stop, not a
    speed control. Keep a bounded ceiling, with headroom for pauses and long
    words. Drama 3 can still end naturally before this maximum.
    """
    spoken = re.sub(r"<\|speaker:\d+\|>|\[[^\]]*\]", " ", text)
    spoken = " ".join(spoken.split())
    return max(240, min(1600, max(len(spoken.split()) * 28, len(spoken) * 4) + 96))


def speech_audio_limit(token_budget):
    """Match the allowed PCM to Drama 3's ~21.5 Hz codec, with two seconds spare.

    The pinned fish_omni deploy config specifies ~21.5 codec frames per second.
    Keep the former 4 MiB allowance for smaller requests; larger valid takes
    may grow only to their requested token duration, with an 8 MiB hard bound.
    """
    estimated = math.ceil((token_budget / 21.5 + 2) * OUTPUT_RATE * 2)
    return min(MAX_VOICE_PCM_BYTES, max(4 * 1024 * 1024, estimated))


def pcm_wav(pcm):
    buffer = BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(OUTPUT_RATE)
        writer.writeframes(pcm)
    return buffer.getvalue()


class SpeechRecognition:
    def __init__(self, directory, device="cpu"):
        self.directory = directory
        self.device = device
        self.model = None
        self.error = None
        # One persistent worker prevents cancelled ASR requests from racing the same model.
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-asr")
        self.lock = threading.Lock()

    def load(self):
        try:
            if not self.directory or not (Path(self.directory) / "model.bin").exists():
                raise RuntimeError("Speech-recognition weights are missing. Run scripts/download_live_models.py first.")
            from faster_whisper import WhisperModel
            self.model = WhisperModel(self.directory, device=self.device,
                                      compute_type="int8" if self.device == "cpu" else "float16",
                                      cpu_threads=8, num_workers=1, local_files_only=True)
        except Exception as exc:
            self.error = str(exc)

    async def transcribe(self, pcm):
        if self.model is None: raise RuntimeError("Speech recognition is not ready.")
        def run():
            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
            with self.lock:
                segments, _ = self.model.transcribe(samples, language="en", beam_size=1,
                                                    condition_on_previous_text=False, vad_filter=False,
                                                    temperature=0, without_timestamps=True)
                return " ".join(segment.text.strip() for segment in segments).strip()
        return await asyncio.get_running_loop().run_in_executor(self.pool, run)

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)


class LocalProviders:
    def __init__(self, stt, llm_url="http://127.0.0.1:18092", tts_url="http://127.0.0.1:18093", transport=None,
                 director_url=None, director_model="live-character", llm_model="live-character", director_timeout=6,
                 verify_voice=False, verification_timeout=20):
        self.stt = stt
        self.llm_url = llm_url.rstrip("/")
        self.tts_url = tts_url.rstrip("/")
        self.director_url = (director_url or self.llm_url).rstrip("/")
        self.director_model = director_model
        self.llm_model = llm_model
        self.director_timeout = max(2,min(60,float(director_timeout)))
        self.verify_voice = bool(verify_voice)
        self.verification_timeout = max(2,min(60,float(verification_timeout)))
        self.last_voice_check = None
        self.last_voice_metrics = {}
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=3), transport=transport)

    async def health(self):
        async def check(url):
            try: return (await self.http.get(url + "/health", timeout=2)).is_success
            except httpx.HTTPError: return False
        llm, tts = await asyncio.gather(check(self.llm_url), check(self.tts_url))
        health={"stt":self.stt.model is not None, "llm":llm, "tts":tts}
        if self.director_url != self.llm_url: health['director']=await check(self.director_url)
        return health

    async def text(self, character, history, *, archive=None):
        prompt = system_prompt(character) + "\n\nFor this voice call, reply in at most three short sentences. Use plain spoken words. Use the heard conversation, including earlier details relevant to the current question; later corrections replace earlier claims. Do not invent a memory when the conversation does not contain it. Do not include bracketed delivery tags, markdown or stage directions."
        messages = [{"role":"system", "content":prompt}]
        if archive:
            messages.extend(archive)
        payload = {"model":self.llm_model, "messages":messages + history,
                   "stream":True, "max_tokens":160, "temperature":0.7,
                   "chat_template_kwargs":{"enable_thinking":False}}
        await record_request('actor', payload)
        async with self.http.stream("POST", self.llm_url + "/v1/chat/completions", json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"): continue
                data = line[5:].strip()
                if data == "[DONE]": break
                event = json.loads(data)
                if event.get("error"): raise RuntimeError("The text model returned a streaming error.")
                choices = event.get("choices", [])
                if choices:
                    text = choices[0].get("delta", {}).get("content")
                    if text: yield text

    async def direct(self, character, history, latest, settings, voice, trigger=None,
                     active=False, previous=None, stage_action=False, narrative=None, *, archive=None):
        messages = director_messages(character,history,latest,settings,voice,trigger,
                                     active,previous,stage_action,narrative)
        if archive:
            messages[0]['content'] += '\n\n' + archive[0]['content'] + '\nFor this backstage task, keep returning only the required director JSON; do not write the spoken answer.'
            messages[1:1] = archive[1:]
        payload = {"model":self.director_model, "stream":False, "temperature":0.2, "max_tokens":420,
                   "chat_template_kwargs":{"enable_thinking":False},
                   "response_format":{"type":"json_object"},
                   "messages":messages}
        await record_request('director', payload)
        response = await self.http.post(self.director_url + "/v1/chat/completions", json=payload, timeout=self.director_timeout)
        response.raise_for_status()
        return DirectorPlan.model_validate_json(response.json()["choices"][0]["message"]["content"])

    async def assess_trigger(self, character, trigger, history, latest, active):
        system, situation = assessment_messages(character, trigger, history, latest, active)
        payload = {"model":"live-character", "stream":False, "temperature":0, "max_tokens":110,
                   "response_format":{"type":"json_object"},
                   "messages":[{"role":"system","content":system},
                               {"role":"user","content":json.dumps(situation,ensure_ascii=False)}]}
        await record_request('trigger', payload)
        response = await self.http.post(self.llm_url + "/v1/chat/completions", json=payload, timeout=4)
        response.raise_for_status()
        body = response.json()
        content = body["choices"][0]["message"]["content"]
        return TriggerDecision.model_validate_json(content)

    async def _voice_take(self, text, reference, *, token_budget=None):
        budget = speech_token_budget(text) if token_budget is None else token_budget
        if type(budget) is not int or not 1 <= budget <= 1600:
            raise ValueError('Use a bounded voice generation budget.')
        payload = {"model":"fishaudio/drama-3", "input":text, "voice":"default", "stream":True,
                   "stream_format":"sse", "response_format":"pcm",
                   "max_new_tokens":budget, "extra_params":dict(REQUEST_SAMPLING)}
        if reference:
            payload["ref_audio"], payload["ref_text"] = reference
        await record_request('tts', payload)
        started = time.perf_counter()
        pcm = bytearray()
        limit = speech_audio_limit(payload['max_new_tokens'])
        stats = {'budget_tokens':payload['max_new_tokens'], 'audio_limit_bytes':limit,
                 'audio_bytes':0, 'audio_chunks':0, 'events':0, 'done_received':False,
                 'finish_reason':'not_reported'}
        try:
            async with self.http.stream("POST", self.tts_url + "/v1/audio/speech", json=payload) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if content_type != 'text/event-stream':
                    raise VoiceProtocolError('Drama 3 must return voice completion events for live playback.', reason='content_type')
                done = limited = False
                async for event_name, raw in speech_events(response):
                    stats['events'] += 1
                    try:
                        event = json.loads(raw)
                    except (ValueError, TypeError) as exc:
                        raise VoiceTakeIncomplete('Drama 3 returned a malformed voice event.', reason='malformed_event') from exc
                    if not isinstance(event, dict):
                        raise VoiceTakeIncomplete('Drama 3 returned a malformed voice event.', reason='malformed_event')
                    kind = event.get('type')
                    if event_name and event_name != kind:
                        raise VoiceTakeIncomplete('Drama 3 returned inconsistent voice events.', reason='inconsistent_event')
                    if done:
                        raise VoiceTakeIncomplete('Drama 3 returned voice events after completion.', reason='events_after_done')
                    if kind == 'speech.audio.error':
                        error = event.get('error')
                        if isinstance(error, dict):
                            stats.update(_safe_voice_metadata({'service_type':error.get('type'),
                                'service_code':error.get('code'), 'service_partial_audio':error.get('partial_audio'),
                                'service_action':error.get('action')}))
                        raise VoiceTakeIncomplete('Drama 3 did not complete the voice take.', reason='upstream_error')
                    if kind == 'speech.audio.delta':
                        # Raw PCM has no self-describing rate. Do not silently guess
                        # metadata: a wrong rate changes both pitch and duration.
                        stats.update(_safe_voice_metadata({'sample_rate':event.get('sample_rate'),
                                                          'channels':event.get('channels')}))
                        if (type(event.get('sample_rate')) is not int or event['sample_rate'] != OUTPUT_RATE
                                or type(event.get('channels')) is not int or event['channels'] != 1
                                or event.get('response_format') != 'pcm'):
                            raise VoiceProtocolError('Drama 3 must return PCM at 44100 Hz with one channel.', reason='pcm_format')
                        encoded = event.get('audio')
                        if not isinstance(encoded, str):
                            raise VoiceTakeIncomplete('Drama 3 returned malformed voice audio.', reason='malformed_audio')
                        try:
                            pcm.extend(base64.b64decode(encoded, validate=True))
                        except (binascii.Error, ValueError) as exc:
                            raise VoiceTakeIncomplete('Drama 3 returned malformed voice audio.', reason='malformed_audio') from exc
                        stats['audio_chunks'] += 1
                        stats['audio_bytes'] = len(pcm)
                        if pcm and 'first_audio_ms' not in stats:
                            stats['first_audio_ms'] = round((time.perf_counter()-started)*1000)
                        if len(pcm) > limit:
                            raise VoiceTakeIncomplete('Voice phrase exceeded the live audio limit.', reason='audio_limit')
                    elif kind == 'speech.audio.done':
                        stats['done_received'] = True
                        stats.update(_safe_voice_metadata({'finish_reason':event.get('finish_reason')}))
                        usage = event.get('usage')
                        output_tokens = usage.get('output_tokens') if isinstance(usage, dict) else None
                        if type(output_tokens) is int and output_tokens >= 0:
                            stats['output_tokens'] = output_tokens
                        if event.get('finish_reason') in ('abort', 'cancelled'):
                            raise VoiceTakeIncomplete('Drama 3 reported an aborted voice take.', reason='upstream_error')
                        if type(output_tokens) is not int or output_tokens < 0:
                            raise VoiceTakeIncomplete('Drama 3 did not confirm the voice generation budget.', reason='missing_usage')
                        # A limit is not yet a playable take. Finish consuming
                        # the entire stream so later errors/duplicate events or
                        # unfinished SSE cannot be hidden by candidate recovery.
                        limited = (event.get('finish_reason') == 'length'
                                   or output_tokens >= payload['max_new_tokens'])
                        done = True
                    else:
                        raise VoiceTakeIncomplete('Drama 3 returned an unknown voice event.', reason='unknown_event')
                if not done:
                    raise VoiceTakeIncomplete('Drama 3 returned no voice completion event.', reason='missing_done')
                if not pcm:
                    raise VoiceTakeIncomplete('Drama 3 returned empty voice audio.', reason='empty_audio')
                if len(pcm) % 2:
                    raise VoiceTakeIncomplete('Drama 3 returned unaligned voice audio.', reason='unaligned_audio')
                if pcm.startswith(b'RIFF'):
                    raise VoiceProtocolError('Expected PCM, received WAV.', reason='wav_instead_of_pcm')
                stats.update(audio_seconds=round(len(pcm)/(2*OUTPUT_RATE),3),
                             elapsed_ms=round((time.perf_counter()-started)*1000))
                return _VoiceTake(bytes(pcm), limited, _safe_voice_metadata(stats))
        except VoiceFailure as exc:
            exc.add_metadata(**stats, audio_seconds=round(len(pcm)/(2*OUTPUT_RATE),3),
                             elapsed_ms=round((time.perf_counter()-started)*1000))
            raise
        except httpx.HTTPError as exc:
            # Preserve counters through transport errors without retaining the
            # response body, connection URL, submitted text or reference audio.
            exc.voice_metadata = _safe_voice_metadata({**stats,
                'audio_seconds':round(len(pcm)/(2*OUTPUT_RATE),3),
                'elapsed_ms':round((time.perf_counter()-started)*1000)})
            raise
        finally:
            if 'first_audio_ms' in stats:
                self.last_voice_metrics['first_audio_ms'] = stats['first_audio_ms']

    async def check_spoken_reply(self, text, pcm, *, enforce=True):
        if not self.verify_voice: return
        started = time.perf_counter()
        try:
            recognized = await asyncio.wait_for(self.stt.transcribe(resample_for_recognition(pcm)),
                                                timeout=self.verification_timeout)
            report = compare_speech(spoken_text(text), recognized)
            prefix = normalized_prefix(spoken_text(text), recognized)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise VoiceProtocolError('The generated speech could not be checked.', reason='verification_unavailable',
                metadata={'verification_ms':round((time.perf_counter()-started)*1000)}) from None
        finally:
            self.last_voice_metrics['verification_ms'] = self.last_voice_metrics.get('verification_ms',0) + round((time.perf_counter()-started)*1000)
        metadata = {'verification_ms':round((time.perf_counter()-started)*1000),
                    'expected_words':report.expected_words,'heard_words':report.heard_words,
                    'matched_words':report.matched_words,'coverage':round(report.coverage,3),
                    'ordered_prefix':prefix}
        self.last_voice_check = _safe_voice_metadata(metadata)
        if enforce and not report.acceptable:
            raise VoiceTakeIncomplete('Generated speech did not cover the requested reply.',
                                      reason='speech_mismatch', metadata=metadata)
        return report

    async def audio(self, text, reference=None, on_notice=None):
        """Check whole replies before playback, with one bounded same-delivery retry.

        A capped take needs exact independently recognized words plus a quiet
        ending; only confirmed trailing silence can be removed. A clean active
        prefix can grant the second attempt more tokens, never another attempt.
        """
        # Freeze a caller's mutable reference before the first attempt. Incomplete
        # takes and service failures share one retry, never nested retry loops.
        reference = tuple(reference) if reference else reference
        self.last_voice_metrics = {'attempt':0,'synthesis_ms':0,'verification_ms':0}
        budget = speech_token_budget(text)
        for attempt in range(2):
            self.last_voice_metrics['attempt'] = attempt + 1
            self.last_voice_metrics.pop('first_audio_ms',None)
            await report_voice_progress('generating',attempt=attempt+1)
            transient = False
            pcm = take = None
            trim_metadata = {}
            retry_budget = budget
            self.last_voice_check = None
            try:
                generation_started = time.perf_counter()
                try:
                    take = await self._voice_take(text, reference, token_budget=budget)
                finally:
                    self.last_voice_metrics['synthesis_ms'] += round((time.perf_counter()-generation_started)*1000)
                if 'first_audio_ms' in take.metadata:
                    self.last_voice_metrics['first_audio_ms'] = take.metadata['first_audio_ms']
                pcm = take.pcm
                if take.limited:
                    metadata = dict(take.metadata)
                    recovered = False
                    if self.verify_voice:
                        tail = analyze_tail(pcm)
                        metadata.update(tail.safe_metadata())
                        active = tail.speech_detected and tail.trailing_silence_ms < 20
                        metadata['active_ending'] = active
                        # Silent/noisy or ambiguous endings cannot qualify for
                        # either rescue or a longer retry. Avoid ASR on silence.
                        if tail.clean_ending or active:
                            checked_pcm = trim_tail(pcm, tail) if tail.clean_ending else pcm
                            await report_voice_progress('checking',**self.last_voice_metrics)
                            report = await self.check_spoken_reply(text, checked_pcm, enforce=False)
                            metadata.update(self.last_voice_check or {})
                            complete = (report is not None and report.expected_words > 0
                                        and report.matched_words == report.expected_words == report.heard_words)
                            if tail.clean_ending and complete:
                                pcm = checked_pcm
                                metadata.update(recovered_after_limit=True,
                                    trimmed_audio_seconds=round(len(pcm)/(2*OUTPUT_RATE),3),
                                    removed_silence_ms=round((len(take.pcm)-len(pcm))*1000/(2*OUTPUT_RATE),3))
                                self.last_voice_check = _safe_voice_metadata(metadata)
                                recovered = True
                                notice = 'The full reply was verified; trailing silence was removed.'
                                if on_notice is not None: await on_notice(notice)
                                logger.info('Voice take recovered: %s', json.dumps(self.last_voice_check, sort_keys=True))
                            elif (attempt == 0 and active and report is not None
                                  and report.heard_words >= 3 and report.coverage >= .2
                                  and report.matched_words == report.heard_words <= report.expected_words
                                  and metadata.get('ordered_prefix') is True):
                                retry_budget = min(1600,max(budget+256,math.ceil(budget*1.5)))
                                if retry_budget > budget: metadata['next_budget_tokens'] = retry_budget
                    if not recovered:
                        raise VoiceTakeIncomplete('Drama 3 reached the voice generation limit.',
                                                  reason='token_limit', metadata=metadata)
                else:
                    # A normal EOS can also leave a long quiet tail. Remove
                    # only the strict verified-silence candidate before ASR;
                    # the requested words must still pass the same check.
                    if self.verify_voice:
                        tail = analyze_tail(pcm)
                        if tail.clean_ending:
                            candidate = trim_tail(pcm,tail)
                            if len(candidate) < len(pcm):
                                trim_metadata = {'trimmed_audio_seconds':round(len(candidate)/(2*OUTPUT_RATE),3),
                                    'removed_silence_ms':round((len(pcm)-len(candidate))*1000/(2*OUTPUT_RATE),3)}
                                pcm = candidate
                    await report_voice_progress('checking',**self.last_voice_metrics)
                    await self.check_spoken_reply(text, pcm)
                    if self.last_voice_check is not None:
                        self.last_voice_check = {**take.metadata,**self.last_voice_check,**trim_metadata}
            except VoiceFailure as exc:
                if take is not None:
                    transport = take.metadata if take.limited else {
                        key:value for key,value in take.metadata.items() if type(value) in (int,float,bool)}
                    exc.add_metadata(**transport,**trim_metadata)
                exc.add_metadata(attempt=attempt+1, attempts=attempt+1)
                logger.warning('Voice take failed: %s', json.dumps(exc.diagnostic(), sort_keys=True))
                if not isinstance(exc, VoiceTakeIncomplete) or attempt:
                    pcm = take = None
                    raise
                # Never recover an expressive take by stripping its delivery.
                message = ('Voice generation did not finish: ' + exc.safe_summary() +
                           '. Retrying the whole reply with the same emotion and voice.')
                budget = retry_budget
            except httpx.HTTPStatusError as exc:
                failure = VoiceServiceUnavailable(reason='http_status', metadata={
                    **getattr(exc, 'voice_metadata', {}),
                    'http_status':exc.response.status_code, 'attempt':attempt+1, 'attempts':attempt+1,
                    'budget_tokens':budget})
                logger.warning('Voice take failed: %s', json.dumps(failure.diagnostic(), sort_keys=True))
                if exc.response.status_code not in TRANSIENT_VOICE_STATUSES:
                    exc.voice_failure = VoiceProtocolError('The voice service rejected the request.', reason='http_status',
                                                           metadata=failure.metadata)
                    raise
                if attempt: raise failure from None
                transient = True
                message = ('The voice service is temporarily unavailable: ' + failure.safe_summary() +
                           '. Retrying the whole reply with the same emotion and voice.')
            except TRANSIENT_VOICE_ERRORS as exc:
                reason = ('transport_timeout' if isinstance(exc, httpx.TimeoutException) else
                          'transport_protocol' if isinstance(exc, httpx.RemoteProtocolError) else 'transport_network')
                failure = VoiceServiceUnavailable(reason=reason, metadata={
                    **getattr(exc, 'voice_metadata', {}),
                    'attempt':attempt+1, 'attempts':attempt+1, 'budget_tokens':budget})
                logger.warning('Voice take failed: %s', json.dumps(failure.diagnostic(), sort_keys=True))
                if attempt: raise failure from None
                transient = True
                message = ('The voice service is temporarily unavailable: ' + failure.safe_summary() +
                           '. Retrying the whole reply with the same emotion and voice.')
            except (httpx.LocalProtocolError, httpx.UnsupportedProtocol) as exc:
                failure = VoiceProtocolError('The voice request could not be sent.', reason='transport_protocol',
                    metadata={**getattr(exc, 'voice_metadata', {}),
                              'attempt':attempt+1, 'attempts':attempt+1, 'budget_tokens':budget})
                logger.warning('Voice take failed: %s', json.dumps(failure.diagnostic(), sort_keys=True))
                exc.voice_failure = failure
                raise
            else:
                break
            if on_notice is not None: await on_notice(message)
            else: logger.warning(message)
            await report_voice_progress('retrying',**{**self.last_voice_metrics,'attempt':attempt+2})
            if transient: await asyncio.sleep(.25)
            # Failed raw candidates belong only to this attempt, never to a
            # reusable exception, session retry cache or model-input inspector.
            pcm = take = None
        # Send every retained sample unchanged; only a strictly verified quiet
        # suffix can have been removed above. WebSocket packets stay bounded.
        await report_voice_progress('ready',**self.last_voice_metrics)
        for start in range(0, len(pcm), 16384):
            yield pcm[start:start + 16384]

    async def close(self):
        await self.http.aclose(); self.stt.close()


def synthetic_reference(pcm, text):
    # The pinned serving runtime validates every inline reference as 1–30 s.
    # A whole reply outside that range must not poison the next request; audio
    # cannot be cropped while retaining a transcript of the uncropped reply.
    if not pcm or len(pcm) % 2 or not isinstance(text, str) or not text.strip(): return None
    duration = len(pcm) / (2 * OUTPUT_RATE)
    if not 1 <= duration <= 30: return None
    return "data:audio/wav;base64," + base64.b64encode(pcm_wav(pcm)).decode(), text
