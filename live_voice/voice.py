"""Drama 3 delivery tags and optional local WAV voice references."""
import base64
import binascii
from pathlib import Path
from io import BytesIO
from typing import Literal
import wave

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_REFERENCE_BYTES = 2 * 1024 * 1024
MAX_START_BYTES = 3 * 1024 * 1024
Emotion = Literal["neutral", "calm", "excited", "sad", "angry", "anxious", "whisper"]
VOICE_DIRECTORY = Path(__file__).resolve().parent.parent / 'assets' / 'voices'
REFERENCE_TEXT = 'When a man looks for something beyond his reach, his friends say he is looking for the pot of gold at the end of the rainbow.'
EMOTION_DIRECTIONS = {
    "anxious":"frightened, shaky voice, urgent pleading, quick uneven breaths",
    "angry":"tense, forceful and sharply emphatic delivery",
    "sad":"subdued, fragile voice with hesitant pauses",
    "excited":"bright, energetic voice with animated pitch and lively pace",
    "calm":"gentle, unhurried, reassuring delivery",
    "whisper":"quiet intimate whisper",
}


class Delivery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emotion: Emotion = "neutral"
    event: Literal["none", "sigh", "laugh"] = "none"
    direction: str = Field(default="", max_length=200)


class VoiceSettings(Delivery):
    preset: Literal["automatic", "female", "male", "custom"] = "automatic"
    reference_audio: str = Field(default="", max_length=MAX_START_BYTES - 65536)
    reference_text: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def check_reference(self):
        if not self.reference_audio and not self.reference_text.strip():
            if self.preset == "custom": raise ValueError("Upload a WAV and transcript for a custom voice.")
            return self
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
        if self.preset in ("female", "male"):
            audio = (VOICE_DIRECTORY / f"{self.preset}.wav").read_bytes()
            return "data:audio/wav;base64," + base64.b64encode(audio).decode(), REFERENCE_TEXT
        return None


def spoken_input(text, delivery, first_phrase=False):
    """Set one performance before the complete reply, never between sentences.

    A speaker-prefixed descriptive cue completed the real regression lines;
    the former stacked mid-sentence emotion/direction tags stalled on them.
    """
    base = EMOTION_DIRECTIONS.get(delivery.emotion, "")
    custom = " ".join(delivery.direction.translate(str.maketrans('', '', '[]<>')).split())
    # A custom direction refines the selected emotional state; it must not
    # silently replace it (for example, excited + a conversational pace).
    direction = "; ".join(part for part in (base, custom if custom.lower().rstrip('.') != base.lower().rstrip('.') else "") if part)
    if first_phrase and delivery.event != "none":
        event = {"sigh":"begin with a soft sigh", "laugh":"begin with a brief laugh"}[delivery.event]
        direction = "; ".join(part for part in (direction, event) if part)
    if not direction: return text
    return f"<|speaker:0|> [{direction}] {text}"
