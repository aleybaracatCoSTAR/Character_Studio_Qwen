"""One model and one generation at a time. Torch is imported only on the GPU path."""
from dataclasses import dataclass, field
from pathlib import Path
import threading
import time
import uuid

from .audio import write_reply
from .characters import Character, system_prompt

MODEL_ID = "Qwen/Qwen3-Omni-30B-A3B-Instruct"
MAX_PAIRS = 6
MAX_SESSIONS = 8
SESSION_TTL = 1800
MAX_INPUT_TOKENS = 8192


class NotReady(Exception):
    pass


class Busy(Exception):
    pass


class CharacterChanged(Exception):
    pass


@dataclass
class UserTurn:
    text: str
    audio: object = None


@dataclass
class Session:
    fingerprint: str
    pairs: list = field(default_factory=list)
    touched: float = field(default_factory=time.monotonic)


def messages_for(character: Character, pairs: list, current: UserTurn) -> tuple[list, list]:
    messages = [{"role": "system", "content": [{"type": "text", "text": system_prompt(character)}]}]
    audios = []

    def user_message(turn):
        content = []
        if turn.audio is not None:
            content.append({"type": "audio", "audio": "recording.wav"})
            audios.append(turn.audio)
        if turn.text:
            content.append({"type": "text", "text": turn.text})
        return {"role": "user", "content": content}

    for user, reply in pairs:
        messages.append(user_message(user))
        messages.append({"role": "assistant", "content": [{"type": "text", "text": reply}]})
    messages.append(user_message(current))
    return messages, audios


def generated_ids(output, prompt_length):
    """Transformers 5.2 returns a tensor+audio, or a thinker output for text-only."""
    sequences = getattr(output, "sequences", output)
    return sequences[:, prompt_length:]


class QwenEngine:
    def __init__(self, preview=True, model_dir=None, attention="sdpa"):
        self.preview = preview
        self.model_dir = Path(model_dir).expanduser() if model_dir else None
        self.attention = attention
        self.state = "preview" if preview else "not_loaded"
        self.error = None
        self.model = self.processor = None
        self.sessions = {}
        self.lock = threading.Lock()
        self.load_lock = threading.Lock()

    def status(self):
        return {"model": MODEL_ID, "state": self.state, "ready": self.state == "ready",
                "busy": self.lock.locked(), "error": self.error, "max_recording_seconds": 30}

    def load(self):
        with self.load_lock:
            if self.preview or self.state in ("ready", "loading"):
                return
            self.state = "loading"
            try:
                import torch
                if not torch.cuda.is_available():
                    raise RuntimeError("No CUDA GPU is visible. Start this inside a Surrey GPU allocation.")
                if not self.model_dir or not (self.model_dir / "config.json").is_file():
                    raise RuntimeError("Model files are missing. Set QWEN_MODEL_DIR and run scripts/download_model.py first.")
                from transformers import Qwen3OmniMoeForConditionalGeneration, Qwen3OmniMoeProcessor
                self.processor = Qwen3OmniMoeProcessor.from_pretrained(str(self.model_dir), local_files_only=True)
                self.model = Qwen3OmniMoeForConditionalGeneration.from_pretrained(
                    str(self.model_dir), local_files_only=True, dtype=torch.bfloat16,
                    device_map="cuda:0", attn_implementation=self.attention,
                ).eval()
                self.state = "ready"
            except Exception as exc:
                self.state = "error"
                self.error = str(exc)

    def _prepare(self, character, pairs, current):
        messages, audios = messages_for(character, pairs, current)
        text = self.processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        kwargs = {"text": [text], "return_tensors": "pt", "padding": True}
        if audios:
            kwargs["audio"] = audios
        return self.processor(**kwargs)

    def _generate(self, character, pairs, current, speak):
        import torch
        kept = list(pairs)
        dropped = 0
        while True:
            inputs = self._prepare(character, kept, current)
            if inputs["input_ids"].shape[1] <= MAX_INPUT_TOKENS:
                break
            if not kept:
                raise ValueError("The character definition or recording is too long. Shorten it and try again.")
            kept.pop(0)
            dropped += 1
        inputs = inputs.to(self.model.device).to(self.model.dtype)
        prefix = inputs["input_ids"].shape[1]
        with torch.inference_mode():
            output = self.model.generate(
                **inputs, speaker=character.speaker, return_audio=speak,
                thinker_max_new_tokens=256, thinker_do_sample=False,
                thinker_return_dict_in_generate=True, talker_max_new_tokens=2048,
            )
        if speak:
            text_ids, audio = output
            wav = write_reply(audio.reshape(-1).detach().float().cpu().numpy())
        else:
            text_ids, wav = output, None
        reply = self.processor.batch_decode(generated_ids(text_ids, prefix), skip_special_tokens=True,
                                            clean_up_tokenization_spaces=False)[0].strip()
        if not reply:
            raise RuntimeError("The model returned an empty reply. Try a shorter question.")
        return reply, wav, kept, dropped

    def reply(self, character, current, session_id=None, speak=True):
        if self.state != "ready":
            raise NotReady("The model is not loaded. Preview mode has no AI replies; follow docs/SURREY.md to run Qwen.")
        if not self.lock.acquire(blocking=False):
            raise Busy("Another reply is being generated. Please wait a moment.")
        try:
            now = time.monotonic()
            self.sessions = {key: value for key, value in self.sessions.items() if now - value.touched < SESSION_TTL}
            key = str(session_id) if session_id else str(uuid.uuid4())
            session = self.sessions.get(key)
            fingerprint = character.fingerprint()
            if session and session.fingerprint != fingerprint:
                raise CharacterChanged("Character details changed. Start a new conversation so identities do not mix.")
            if not session:
                session = Session(fingerprint)
            pairs = list(session.pairs[-MAX_PAIRS:])
            dropped = max(0, len(session.pairs) - len(pairs))
            started = time.monotonic()
            text, wav, kept, extra_dropped = self._generate(character, pairs, current, speak)
            # Commit history only after a successful generation.
            session.pairs = (kept + [(current, text)])[-MAX_PAIRS:]
            session.touched = time.monotonic()
            self.sessions[key] = session
            if len(self.sessions) > MAX_SESSIONS:
                oldest = min(self.sessions, key=lambda item: self.sessions[item].touched)
                del self.sessions[oldest]
            return {"session_id": key, "text": text, "wav": wav,
                    "seconds": round(time.monotonic() - started, 2),
                    "dropped_turns": dropped + extra_dropped, "retained_turns": len(session.pairs)}
        finally:
            self.lock.release()
