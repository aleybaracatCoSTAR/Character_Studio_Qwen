"""Drama 3 delivery tags and optional local WAV voice references."""
import base64
import binascii
from io import BytesIO
from typing import Literal
import wave

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_REFERENCE_BYTES = 2 * 1024 * 1024
MAX_START_BYTES = 3 * 1024 * 1024
Emotion = Literal["neutral", "calm", "excited", "sad", "angry", "anxious", "whisper"]


class Delivery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emotion: Emotion = "neutral"
    event: Literal["none", "sigh", "laugh"] = "none"


class VoiceSettings(Delivery):
    reference_audio: str = Field(default="", max_length=MAX_START_BYTES - 65536)
    reference_text: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def check_reference(self):
        if not self.reference_audio and not self.reference_text.strip(): return self
        if not self.reference_audio.startswith("data:audio/wav;base64,") or not self.reference_text.strip():
            raise ValueError("Choose a WAV voice reference and enter its exact spoken words.")
        try:
            audio = base64.b64decode(self.reference_audio.split(",",1)[1], validate=True)
            if len(audio) > MAX_REFERENCE_BYTES: raise ValueError("Voice reference exceeds 2 MB.")
            with wave.open(BytesIO(audio), "rb") as reader:
                if (reader.getnchannels() not in (1,2) or reader.getsampwidth() != 2
                        or not 8000 <= reader.getframerate() <= 48000
                        or not 2 <= reader.getnframes() / reader.getframerate() <= 15):
                    raise ValueError("Use a 2–15 second PCM16 WAV, mono or stereo, at 8–48 kHz.")
                expected = reader.getnframes() * reader.getnchannels() * 2
                if len(reader.readframes(reader.getnframes())) != expected:
                    raise ValueError("Voice reference is incomplete.")
        except (binascii.Error, wave.Error, EOFError) as exc:
            raise ValueError("Use a valid uncompressed PCM16 WAV voice reference.") from exc
        return self

    def reference(self):
        if self.reference_audio: return self.reference_audio, self.reference_text.strip()
        return None


def spoken_input(text, delivery, first_phrase=False):
    """Keep tags out of visible dialogue; insert after opening words to avoid leading-tag stalls."""
    tags = [] if delivery.emotion == "neutral" else [delivery.emotion]
    if first_phrase and delivery.event != "none":
        tags.append({"sigh":"sighs", "laugh":"laughs"}[delivery.event])
    words = text.split()
    if not tags or len(words) < 2: return text
    position = min(3, len(words) - 1)
    return " ".join(words[:position] + [f"[{tag}]" for tag in tags] + words[position:])
