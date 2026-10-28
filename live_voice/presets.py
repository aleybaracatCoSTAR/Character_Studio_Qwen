"""Authored live character bundles; browser-edited profiles stay in the browser."""
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from studio.characters import Character
from .director import DirectorSettings
from .narrative import NarrativeSettings
from .reactions import EmotionalTrigger
from .voice import VoiceSettings

DIRECTORY = Path(__file__).resolve().parent.parent / 'characters'
PRESET_IDS = ('kaden',)


class CharacterPreset(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[a-z0-9-]+$')
    character: Character
    voice: VoiceSettings
    director: DirectorSettings
    trigger: EmotionalTrigger | None = None
    narrative: NarrativeSettings


def preset(preset_id):
    if preset_id not in PRESET_IDS: raise KeyError(preset_id)
    settings = json.loads((DIRECTORY / f'{preset_id}-profile.json').read_text())
    settings['character'] = json.loads((DIRECTORY / f'{preset_id}.json').read_text())
    settings['narrative'] = json.loads((DIRECTORY / f'{preset_id}-story.json').read_text())
    return CharacterPreset.model_validate(settings).model_dump()


def presets():
    return [preset(preset_id) for preset_id in PRESET_IDS]
