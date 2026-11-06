"""Native, turn-based Qwen3-Omni speech input with streaming speech output.

The vLLM-Omni 798fef99a chat protocol labels each SSE chunk with a top-level
``modality``. Audio lives in ``choices[].delta.content`` (base64 PCM), with
per-chunk ``audio_metadata``; it is not OpenAI's ``delta.audio.data`` format.
See the pinned runtime's entrypoints/openai/serving_chat.py and
entrypoints/openai/audio_utils_mixin.py. This adapter forwards microphone WAV
directly to the model and yields each valid PCM packet immediately. Unlike
the separate Drama 3 path, it cannot verify a whole reply before playback.
Completion checks confirm transport completion, not that every word was said.
"""
import asyncio
import base64
import binascii
import copy
from io import BytesIO
import json
import time
import wave

import httpx

from studio.characters import system_prompt
from .inspection import record_request
from .reactions import TriggerDecision, assessment_messages
from .voice import Delivery, EMOTION_DIRECTIONS


MODEL_ID = 'Qwen/Qwen3-Omni-30B-A3B-Instruct'
INPUT_RATE = 16000
OUTPUT_RATE = 24000
MAX_INPUT_PCM_BYTES = INPUT_RATE * 2 * 30
MAX_OUTPUT_PCM_BYTES = OUTPUT_RATE * 2 * 120
MAX_EVENT_CHARS = 4 * 1024 * 1024
MAX_TEXT_CHARS = 12000
SPEAKERS = frozenset(('Chelsie', 'Ethan', 'Aiden'))


class OmniStreamError(RuntimeError):
    """A fixed, safe error; previously emitted audio cannot be taken back."""
    def __init__(self, message, reason='incomplete'):
        super().__init__(message)
        self.reason = reason


def native_voice(character, settings):
    """Resolve supported built-in speakers; Drama references are not portable."""
    if (getattr(settings, 'preset', 'automatic') == 'custom'
            or getattr(settings, 'reference_audio', '')
            or getattr(settings, 'reference_text', '').strip()):
        raise ValueError('Qwen Omni uses built-in voices. Choose Automatic, Female or Male; custom WAV voices are not supported in this mode.')
    preset = getattr(settings, 'preset', 'automatic')
    if preset not in ('automatic', 'female', 'male'):
        raise ValueError('Choose Automatic, Female or Male for Qwen Omni.')
    speaker = {'female':'Chelsie', 'male':'Ethan'}.get(preset, character.speaker)
    if speaker not in SPEAKERS:
        raise ValueError('Choose a supported Qwen Omni voice.')
    return speaker


def _audio_url(pcm):
    if (not isinstance(pcm, bytes) or not pcm or len(pcm) % 2
            or len(pcm) > MAX_INPUT_PCM_BYTES):
        raise ValueError('Use a complete mono PCM16 recording at 16 kHz, no longer than 30 seconds.')
    buffer = BytesIO()
    with wave.open(buffer, 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(INPUT_RATE)
        writer.writeframes(pcm)
    return 'data:audio/wav;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')


def _delivery_prompt(delivery):
    delivery = Delivery() if delivery is None else Delivery.model_validate(delivery)
    directions = [EMOTION_DIRECTIONS.get(delivery.emotion, '')]
    if delivery.direction.strip(): directions.append(delivery.direction.strip())
    if delivery.event != 'none':
        directions.append({'sigh':'Begin with a soft sigh', 'laugh':'Begin with a brief laugh'}[delivery.event])
    directions = [item for item in directions if item]
    if not directions: return ''
    return ('\n\nBackstage performance direction for this entire reply: ' + '; '.join(directions)
            + '. Express this through your voice and phrasing. Do not say the direction or emotional labels aloud.')


def response_messages(character, history, *, pcm=None, delivery=None, archive=None):
    """Copy the prepared character/RAG/heard history, replacing only this input.

    The transcript is useful to the director and later conversation memory.
    When raw audio is available, the native actor receives the recording in
    place of that same utterance's text, not a second copy of the utterance.
    """
    prompt = system_prompt(character) + (
        '\n\nUse the heard conversation, including earlier details relevant to the current question; '
        'later corrections replace earlier claims. Do not invent a memory when the conversation '
        'does not contain it. Answer in plain spoken words, normally one to three short sentences. '
        'Do not include markdown, delivery tags or stage directions.') + _delivery_prompt(delivery)
    messages = [{'role':'system', 'content':prompt}]
    for message in list(archive or []) + list(history):
        if (not isinstance(message, dict) or message.get('role') not in ('system', 'user', 'assistant')
                or not isinstance(message.get('content'), str)):
            raise ValueError('Use prepared text conversation messages for Qwen Omni.')
        messages.append(copy.deepcopy(message))
    if pcm is not None:
        # An empty history may be useful for an initial recorded turn; never
        # replace an older user turn if the prepared history ends in assistant.
        if history:
            if messages[-1]['role'] != 'user':
                raise ValueError('The latest Qwen Omni microphone input must be a participant turn.')
            messages[-1]['content'] = [{'type':'audio_url', 'audio_url':{'url':_audio_url(pcm)}}]
        else:
            messages.append({'role':'user', 'content':[{'type':'audio_url', 'audio_url':{'url':_audio_url(pcm)}}]})
    return messages


def _inspection_payload(payload, pcm):
    if pcm is None: return payload
    safe = copy.deepcopy(payload)
    safe['messages'][-1]['content'] = [{'type':'text', 'text':
        f'[Native microphone WAV omitted: {len(pcm)} PCM bytes, 16000 Hz, mono PCM16.]'}]
    return safe


async def _events(response):
    """Read complete SSE data records despite arbitrary HTTP packet boundaries."""
    parts = []
    size = 0
    async for line in response.aiter_lines():
        if not line:
            if parts: yield '\n'.join(parts)
            parts = []; size = 0
        elif line.startswith(':'):
            continue
        else:
            name, separator, value = line.partition(':')
            if name == 'data' and separator:
                if value.startswith(' '): value = value[1:]
                size += len(value)
                if size > MAX_EVENT_CHARS:
                    raise OmniStreamError('Qwen Omni returned an oversized streaming event.', 'event_limit')
                parts.append(value)
    if parts:
        raise OmniStreamError('Qwen Omni ended during a streaming event.', 'unfinished_event')


class OmniProvider:
    engine = 'qwen-omni'
    output_rate = OUTPUT_RATE
    streaming_speech = True
    verify_voice = False

    def __init__(self, local_providers, base_url='http://127.0.0.1:18097', *, model=MODEL_ID, transport=None):
        self.local = local_providers
        self.stt = local_providers.stt
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=3), transport=transport)
        self.last_voice_metrics = {}

    async def close(self):
        # The app owns the shared director/STT provider and closes it separately.
        await self.http.aclose()

    async def health(self):
        async def check(url):
            try: return (await self.http.get(url + '/health', timeout=2)).is_success
            except httpx.HTTPError: return False
        omni, director = await asyncio.gather(check(self.base_url), check(self.local.director_url))
        return {'stt':getattr(self.stt, 'model', None) is not None, 'omni':omni, 'director':director}

    async def direct(self, *args, **kwargs):
        return await self.local.direct(*args, **kwargs)

    async def assess_trigger(self, character, trigger, history, latest, active):
        # Native mode need not launch the separate baseline actor. Use the
        # configured director endpoint/model for this backstage JSON decision.
        system, situation = assessment_messages(character, trigger, history, latest, active)
        payload = {'model':self.local.director_model, 'stream':False, 'temperature':0, 'max_tokens':110,
                   'response_format':{'type':'json_object'},
                   'chat_template_kwargs':{'enable_thinking':False},
                   'messages':[{'role':'system', 'content':system},
                               {'role':'user', 'content':json.dumps(situation, ensure_ascii=False)}]}
        await record_request('trigger', payload)
        response = await self.http.post(self.local.director_url + '/v1/chat/completions', json=payload, timeout=4)
        response.raise_for_status()
        return TriggerDecision.model_validate_json(response.json()['choices'][0]['message']['content'])

    async def stream_response(self, character, history, *, pcm=None, delivery=None, archive=None, voice='Chelsie'):
        """Yield native text/PCM events, then one done only after a valid stop.

        Audio is intentionally played before the entire take can be checked.
        Errors and cancellation are never retried, since replaying a request
        could repeat words already heard. No ASR or TTS substitution occurs.
        """
        if voice not in SPEAKERS: raise ValueError('Choose a supported Qwen Omni voice.')
        payload = {'model':self.model,
                   'messages':response_messages(character, history, pcm=pcm, delivery=delivery, archive=archive),
                   'stream':True, 'stream_options':{'include_usage':True},
                   'modalities':['text', 'audio'], 'audio':{'format':'pcm16'},
                   'speaker':voice, 'temperature':0.7, 'max_tokens':160}
        await record_request('actor', _inspection_payload(payload, pcm))
        started = time.perf_counter()
        metrics = {'audio_bytes':0, 'audio_chunks':0, 'text_chars':0}
        self.last_voice_metrics = {}
        stopped = done = False
        content_number = stop_number = 0
        async with self.http.stream('POST', self.base_url + '/v1/chat/completions', json=payload) as response:
            response.raise_for_status()
            if response.headers.get('content-type', '').split(';')[0].strip().lower() != 'text/event-stream':
                raise OmniStreamError('Qwen Omni must return a text and audio event stream.', 'content_type')
            async for raw in _events(response):
                if raw == '[DONE]':
                    done = True
                    break
                try: event = json.loads(raw)
                except (ValueError, TypeError) as exc:
                    raise OmniStreamError('Qwen Omni returned a malformed streaming event.', 'malformed_event') from exc
                if not isinstance(event, dict):
                    raise OmniStreamError('Qwen Omni returned a malformed streaming event.', 'malformed_event')
                if event.get('error'):
                    raise OmniStreamError('Qwen Omni reported a streaming generation error.', 'upstream_error')
                choices = event.get('choices')
                if not isinstance(choices, list) or len(choices) > 1:
                    raise OmniStreamError('Qwen Omni returned unsupported streaming choices.', 'malformed_event')
                for choice in choices:
                    if not isinstance(choice, dict) or choice.get('index', 0) != 0:
                        raise OmniStreamError('Qwen Omni returned unsupported streaming choices.', 'malformed_event')
                    delta = choice.get('delta', {})
                    if not isinstance(delta, dict):
                        raise OmniStreamError('Qwen Omni returned a malformed streaming delta.', 'malformed_event')
                    content = delta.get('content')
                    if content not in (None, ''):
                        if not isinstance(content, str):
                            raise OmniStreamError('Qwen Omni returned an unsupported response format.', 'malformed_event')
                        content_number += 1
                        modality = event.get('modality')
                        if modality == 'text':
                            metrics['text_chars'] += len(content)
                            if metrics['text_chars'] > MAX_TEXT_CHARS:
                                raise OmniStreamError('Qwen Omni exceeded the reply text limit.', 'text_limit')
                            metrics.setdefault('first_text_ms', round((time.perf_counter()-started)*1000, 2))
                            yield {'type':'text', 'text':content}
                        elif modality == 'audio':
                            metadata = choice.get('audio_metadata')
                            if (not isinstance(metadata, dict) or metadata.get('format') != 'pcm'
                                    or type(metadata.get('sample_rate_hz')) is not int
                                    or metadata['sample_rate_hz'] != OUTPUT_RATE
                                    or type(metadata.get('channels')) is not int or metadata['channels'] != 1
                                    or type(metadata.get('frame_count')) is not int or metadata['frame_count'] < 1):
                                raise OmniStreamError('Qwen Omni must return mono PCM16 audio at 24 kHz.', 'audio_format')
                            try: audio = base64.b64decode(content, validate=True)
                            except (ValueError, binascii.Error) as exc:
                                raise OmniStreamError('Qwen Omni returned malformed audio data.', 'malformed_audio') from exc
                            if len(audio) != metadata['frame_count'] * 2:
                                raise OmniStreamError('Qwen Omni audio metadata does not match its samples.', 'audio_alignment')
                            metrics['audio_bytes'] += len(audio)
                            metrics['audio_chunks'] += 1
                            if metrics['audio_bytes'] > MAX_OUTPUT_PCM_BYTES:
                                raise OmniStreamError('Qwen Omni exceeded the reply audio limit.', 'audio_limit')
                            metrics.setdefault('first_audio_ms', round((time.perf_counter()-started)*1000, 2))
                            for offset in range(0, len(audio), 16384):
                                yield {'type':'audio', 'pcm':audio[offset:offset+16384], 'sample_rate':OUTPUT_RATE}
                        else:
                            raise OmniStreamError('Qwen Omni did not identify the response modality.', 'modality')
                    finish = choice.get('finish_reason')
                    if finish is not None:
                        if finish != 'stop':
                            raise OmniStreamError('Qwen Omni stopped before completing its spoken reply.', 'incomplete')
                        stopped = True
                        stop_number = content_number
                usage = event.get('usage')
                if isinstance(usage, dict):
                    for name in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
                        value = usage.get(name)
                        if type(value) is int and value >= 0: metrics[name] = value
        if not done or not stopped or stop_number < content_number:
            raise OmniStreamError('Qwen Omni did not confirm the end of its spoken reply.', 'missing_completion')
        if not metrics['audio_bytes'] or not metrics['text_chars']:
            raise OmniStreamError('Qwen Omni did not return both speech and reply text.', 'empty_response')
        metrics.update({'audio_seconds':round(metrics['audio_bytes']/(OUTPUT_RATE*2), 4),
                        'elapsed_ms':round((time.perf_counter()-started)*1000, 2)})
        self.last_voice_metrics = dict(metrics)
        yield {'type':'done', 'metrics':dict(metrics)}
