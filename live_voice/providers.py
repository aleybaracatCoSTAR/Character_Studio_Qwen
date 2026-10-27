import asyncio
import base64
import binascii
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
import logging
from pathlib import Path
import re
import threading
import wave

import httpx
import numpy as np

from studio.characters import system_prompt
from .reactions import TriggerDecision, assessment_messages
from .director import DirectorPlan, director_messages
from .inspection import record_request

OUTPUT_RATE = 44100
logger = logging.getLogger(__name__)


class VoiceTakeIncomplete(RuntimeError):
    """The voice service did not confirm a complete, uncapped PCM take."""


class VoiceServiceUnavailable(RuntimeError):
    """A transient voice-service failure remained after the bounded retry."""
    def __init__(self):
        super().__init__('The voice service is temporarily unavailable. Check that Drama 3 is ready, '
                         'then retry this reply. The chosen emotion and voice have not changed.')


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
                if size > 8 * 1024 * 1024:
                    raise RuntimeError('Voice event exceeded the live audio limit.')
                data.append(value)
    if data:
        raise VoiceTakeIncomplete('Drama 3 returned an unfinished voice event.')


def speech_token_budget(text):
    """Allow unhurried speech to finish; delivery tags are not spoken words.

    The former 13 tokens/word with a 400-token ceiling was a hard stop, not a
    speed control. Keep a bounded ceiling, with headroom for pauses and long
    words. Drama 3 can still end naturally before this maximum.
    """
    spoken = re.sub(r"<\|speaker:\d+\|>|\[[^\]]*\]", " ", text)
    spoken = " ".join(spoken.split())
    return max(240, min(1600, max(len(spoken.split()) * 28, len(spoken) * 4) + 96))


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
                 director_url=None, director_model="live-character", llm_model="live-character", director_timeout=6):
        self.stt = stt
        self.llm_url = llm_url.rstrip("/")
        self.tts_url = tts_url.rstrip("/")
        self.director_url = (director_url or self.llm_url).rstrip("/")
        self.director_model = director_model
        self.llm_model = llm_model
        self.director_timeout = max(2,min(60,float(director_timeout)))
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=3), transport=transport)

    async def health(self):
        async def check(url):
            try: return (await self.http.get(url + "/health", timeout=2)).is_success
            except httpx.HTTPError: return False
        llm, tts = await asyncio.gather(check(self.llm_url), check(self.tts_url))
        health={"stt":self.stt.model is not None, "llm":llm, "tts":tts}
        if self.director_url != self.llm_url: health['director']=await check(self.director_url)
        return health

    async def text(self, character, history):
        prompt = system_prompt(character) + "\n\nFor this voice call, reply in at most three short sentences. Use plain spoken words. Use the heard conversation, including earlier details relevant to the current question; later corrections replace earlier claims. Do not invent a memory when the conversation does not contain it. Do not include bracketed delivery tags, markdown or stage directions."
        payload = {"model":self.llm_model, "messages":[{"role":"system", "content":prompt}] + history,
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
                     active=False, previous=None, stage_action=False, narrative=None):
        payload = {"model":self.director_model, "stream":False, "temperature":0.2, "max_tokens":420,
                   "chat_template_kwargs":{"enable_thinking":False},
                   "response_format":{"type":"json_object"},
                   "messages":director_messages(character,history,latest,settings,voice,trigger,
                                                active,previous,stage_action,narrative)}
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

    async def _voice_take(self, text, reference):
        payload = {"model":"fishaudio/drama-3", "input":text, "voice":"default", "stream":True,
                   "stream_format":"sse", "response_format":"pcm",
                   "max_new_tokens":speech_token_budget(text)}
        if reference:
            payload["ref_audio"], payload["ref_text"] = reference
        await record_request('tts', payload)
        async with self.http.stream("POST", self.tts_url + "/v1/audio/speech", json=payload) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type != 'text/event-stream':
                raise RuntimeError('Drama 3 must return voice completion events for live playback.')
            pcm = bytearray()
            done = False
            async for event_name, raw in speech_events(response):
                try:
                    event = json.loads(raw)
                except (ValueError, TypeError) as exc:
                    raise VoiceTakeIncomplete('Drama 3 returned a malformed voice event.') from exc
                if not isinstance(event, dict):
                    raise VoiceTakeIncomplete('Drama 3 returned a malformed voice event.')
                kind = event.get('type')
                if event_name and event_name != kind:
                    raise VoiceTakeIncomplete('Drama 3 returned inconsistent voice events.')
                if done:
                    raise VoiceTakeIncomplete('Drama 3 returned voice events after completion.')
                if kind == 'speech.audio.error':
                    raise VoiceTakeIncomplete('Drama 3 did not complete the voice take.')
                if kind == 'speech.audio.delta':
                    # Raw PCM has no self-describing rate. Do not silently guess
                    # metadata: a wrong rate changes both pitch and duration.
                    if (type(event.get('sample_rate')) is not int or event['sample_rate'] != OUTPUT_RATE
                            or type(event.get('channels')) is not int or event['channels'] != 1
                            or event.get('response_format') != 'pcm'):
                        raise RuntimeError('Drama 3 must return PCM at 44100 Hz with one channel.')
                    encoded = event.get('audio')
                    if not isinstance(encoded, str):
                        raise VoiceTakeIncomplete('Drama 3 returned malformed voice audio.')
                    try:
                        pcm.extend(base64.b64decode(encoded, validate=True))
                    except (binascii.Error, ValueError) as exc:
                        raise VoiceTakeIncomplete('Drama 3 returned malformed voice audio.') from exc
                    if len(pcm) > 4 * 1024 * 1024:
                        raise VoiceTakeIncomplete('Voice phrase exceeded the live audio limit.')
                elif kind == 'speech.audio.done':
                    usage = event.get('usage')
                    output_tokens = usage.get('output_tokens') if isinstance(usage, dict) else None
                    if type(output_tokens) is not int or output_tokens < 0:
                        raise VoiceTakeIncomplete('Drama 3 did not confirm the voice generation budget.')
                    if output_tokens >= payload['max_new_tokens']:
                        raise VoiceTakeIncomplete('Drama 3 reached the voice generation limit.')
                    done = True
                else:
                    raise VoiceTakeIncomplete('Drama 3 returned an unknown voice event.')
            if not done or not pcm or len(pcm) % 2:
                raise VoiceTakeIncomplete('Drama 3 returned incomplete or empty voice audio.')
            if pcm.startswith(b'RIFF'):
                raise RuntimeError('Expected PCM, received WAV.')
            return bytes(pcm)

    async def audio(self, text, reference=None, on_notice=None):
        """Validate one bounded reply before playing it, with one identical retry.

        Buffering until its done event adds the reply's synthesis time before
        playback; it prevents an unconfirmed/capped take from reaching listeners.
        This verifies protocol completion, not that every requested word was said.
        """
        # Freeze a caller's mutable reference before the first attempt. Incomplete
        # takes and service failures share one retry, never nested retry loops.
        reference = tuple(reference) if reference else reference
        for attempt in range(2):
            transient = False
            try:
                pcm = await self._voice_take(text, reference)
            except VoiceTakeIncomplete:
                if attempt: raise
                # Never recover an expressive take by stripping its delivery.
                message = 'Voice generation did not finish. Retrying the whole reply with the same emotion and voice.'
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in TRANSIENT_VOICE_STATUSES: raise
                if attempt: raise VoiceServiceUnavailable() from None
                transient = True
                message = 'The voice service is temporarily unavailable. Retrying the whole reply with the same emotion and voice.'
            except TRANSIENT_VOICE_ERRORS:
                if attempt: raise VoiceServiceUnavailable() from None
                transient = True
                message = 'The voice service is temporarily unavailable. Retrying the whole reply with the same emotion and voice.'
            else:
                break
            if on_notice is not None: await on_notice(message)
            else: logger.warning(message)
            if transient: await asyncio.sleep(.25)
        # Every final sample is retained; packets remain bounded for WebSocket.
        for start in range(0, len(pcm), 16384):
            yield pcm[start:start + 16384]

    async def close(self):
        await self.http.aclose(); self.stt.close()


def synthetic_reference(pcm, text):
    return "data:audio/wav;base64," + base64.b64encode(pcm_wav(pcm)).decode(), text
