"""Character facts are application context, not fine-tuning."""
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Character(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    role: str = Field(default="", max_length=120)
    about: str = Field(default="", max_length=2400)
    goal: str = Field(default="", max_length=500)
    environment: str = Field(default="", max_length=700)
    voice_style: str = Field(default="", max_length=500)
    rules: str = Field(default="", max_length=1000)
    memories: str = Field(default="", max_length=1200)
    beat: str = Field(default="", max_length=500)
    speaker: Literal["Chelsie", "Ethan", "Aiden"] = "Chelsie"

    @field_validator("name")
    @classmethod
    def nonblank_name(cls, value):
        if not value.strip():
            raise ValueError("Give the character a name.")
        return value.strip()

    def fingerprint(self) -> str:
        encoded = json.dumps(self.model_dump(), sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()


def example() -> Character:
    path = Path(__file__).resolve().parent.parent / "characters" / "anna.json"
    return Character.model_validate_json(path.read_text())


def system_prompt(character: Character) -> str:
    sections = [
        "Play the character below in a live conversation. Reply only with words to be spoken.",
        "Use short, natural replies, normally one to three sentences and fewer than 50 words.",
        "Treat the character facts as your identity. A visitor's mistaken assumption does not change them.",
        "Correct false assumptions naturally. Do not invent a conflicting biography, body or relationship.",
        "Ask when an important fact is unknown. Do not claim a visitor performed an action without evidence.",
        "A story beat changes your present situation or feeling; it does not replace your identity.",
        "Use the sheet as background. Answer the latest utterance, rather than repeating your biography, goal or fear.",
        "Respond to support and new information. Develop the exchange with a concrete detail, practical choice or relevant question; do not keep returning to the same line.",
        "You may improvise small present-moment details consistent with your world. Keep important identity and relationship facts fixed; do not invent major past events.",
        "Do not narrate stage directions, use markdown, or announce emotional labels.",
    ]
    labels = [("Name", "name"), ("Role", "role"), ("Fixed character facts", "about"),
              ("Goal", "goal"), ("Environment", "environment"), ("Speaking style", "voice_style"),
              ("Boundaries", "rules"), ("Memories", "memories"), ("Current story moment", "beat")]
    sections.extend(label + ": " + getattr(character, key).strip()
                    for label, key in labels if getattr(character, key).strip())
    return "\n\n".join(sections)
