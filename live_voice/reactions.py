"""An explicit action changes the character's current behaviour and vocal delivery."""
import re

from pydantic import BaseModel, ConfigDict, Field

from .voice import Emotion


def normalised(text):
    return " ".join(re.findall(r"\w+", text.casefold()))


class EmotionalTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(default="Press reset", min_length=1, max_length=60)
    condition: str = Field(default="Someone presses Anna's reset button.", min_length=1, max_length=200)
    emotion: Emotion = "anxious"
    behaviour: str = Field(default="Your polite small talk gives way to anxiety. You fear a reset will erase your memories, especially the housekeeper's kindness. Ask them to stop and explain why those memories matter. Speak in short, urgent sentences. Stay a washing machine; do not invent a human body.", min_length=1, max_length=600)
    spoken_cue: str = Field(default="I pressed your reset button", max_length=160)

    def matches(self, text):
        # This is an explicit rehearsal cue, not a claim of general intent recognition.
        # Questions, negations and descriptions containing the word 'reset' alone do not fire it.
        cue = normalised(self.spoken_cue)
        return bool(cue) and "?" not in text and normalised(text) == cue

    def state_note(self):
        return (f"The following stage action has actually occurred: {self.condition}\n"
                f"Your current emotion is {self.emotion}. {self.behaviour}\n"
                "Maintain this changed state until the trigger is cleared. Preserve your fixed identity and memories.")
