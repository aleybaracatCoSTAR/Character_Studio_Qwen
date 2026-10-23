import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
from pathlib import Path
import threading
import wave

import httpx
import numpy as np

from studio.characters import system_prompt

OUTPUT_RATE = 44100


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
    def __init__(self, stt, llm_url="http://127.0.0.1:18092", tts_url="http://127.0.0.1:18093", transport=None):
        self.stt = stt
        self.llm_url = llm_url.rstrip("/")
        self.tts_url = tts_url.rstrip("/")
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=3), transport=transport)

    async def health(self):
        async def check(url):
            try: return (await self.http.get(url + "/health", timeout=2)).is_success
            except httpx.HTTPError: return False
        llm, tts = await asyncio.gather(check(self.llm_url), check(self.tts_url))
        return {"stt":self.stt.model is not None, "llm":llm, "tts":tts}

    async def text(self, character, history):
        prompt = system_prompt(character) + "\n\nFor this voice call, reply in at most two short sentences. Use plain spoken words. Do not include bracketed delivery tags, markdown or stage directions."
        payload = {"model":"live-character", "messages":[{"role":"system", "content":prompt}] + history,
                   "stream":True, "max_tokens":100, "temperature":0.65}
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

    async def audio(self, text, reference=None):
        payload = {"model":"fishaudio/drama-3", "input":text, "voice":"default", "stream":True,
                   "stream_format":"audio", "response_format":"pcm",
                   "max_new_tokens":max(100, min(400, len(text.split()) * 13 + 32))}
        if reference:
            payload["ref_audio"], payload["ref_text"] = reference
        async with self.http.stream("POST", self.tts_url + "/v1/audio/speech", json=payload) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";")[0].lower()
            if content_type not in ("audio/pcm", "audio/raw", "audio/l16", "application/octet-stream"):
                raise RuntimeError("Drama 3 must return raw PCM for live playback.")
            remainder = b""; total = 0
            async for raw in response.aiter_bytes():
                merged = remainder + raw; boundary = len(merged) // 2 * 2
                chunk, remainder = merged[:boundary], merged[boundary:]
                if chunk:
                    if total == 0 and chunk.startswith(b"RIFF"): raise RuntimeError("Expected PCM, received WAV.")
                    total += len(chunk)
                    if total > 4 * 1024 * 1024: raise RuntimeError("Voice phrase exceeded the live audio limit.")
                    # Bound frames so WebSocket messages remain small.
                    for start in range(0, len(chunk), 16384): yield chunk[start:start+16384]
            if remainder or not total: raise RuntimeError("Drama 3 returned incomplete or empty audio.")

    async def close(self):
        await self.http.aclose(); self.stt.close()


def synthetic_reference(pcm, text):
    return "data:audio/wav;base64," + base64.b64encode(pcm_wav(pcm)).decode(), text
