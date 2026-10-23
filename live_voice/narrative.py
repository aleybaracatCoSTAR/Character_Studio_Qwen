"""A bounded story controller: the performer owns the arc; models supply evidence."""
import re
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .voice import Emotion


class StoryBeat(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=80)
    goal: str = Field(min_length=1, max_length=400)
    note: str = Field(default="", max_length=500)
    emotion: Emotion = "neutral"
    delivery: str = Field(default="", max_length=200)
    min_seconds: float = Field(default=15, ge=0, le=1800, allow_inf_nan=False)
    max_seconds: float = Field(default=90, ge=5, le=1800, allow_inf_nan=False)

    @model_validator(mode="after")
    def bounds(self):
        if self.max_seconds <= self.min_seconds:
            raise ValueError("Each beat's maximum must exceed its minimum time.")
        return self


class NarrativeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    mode: Literal["manual", "timed", "director"] = "director"
    beats: list[StoryBeat] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def populated(self):
        if self.enabled and not self.beats: raise ValueError("Add a beat before enabling the director script.")
        return self


class StoryControl(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["next", "previous", "pause", "resume", "restart", "cue"]


def normalise(text):
    return re.sub(r"\s+", " ", text).strip().casefold()


class NarrativeState:
    def __init__(self, settings=None, clock=time.monotonic):
        self.settings = settings or NarrativeSettings()
        self.clock = clock
        self.index = 0
        self.revision = 0
        self.entered = clock()
        self.paused_at = None
        self.finished = False
        self.reason = "start"
        self.evidence = ""
        self.heard = []
        self.pending = None
        self.director_note = ""
        self.notes_sent = 0
        self.last_note_at = -1e9

    @property
    def active(self): return self.settings.enabled and bool(self.settings.beats)

    @property
    def beat(self): return self.settings.beats[self.index] if self.active else None

    @property
    def age(self): return max(0, (self.paused_at if self.paused_at is not None else self.clock()) - self.entered)

    def pause(self, paused):
        if paused and self.paused_at is None: self.paused_at = self.clock()
        elif not paused and self.paused_at is not None:
            self.entered += self.clock() - self.paused_at; self.paused_at = None

    def move(self, direction=1, reason="performer", evidence=""):
        if not self.active: return False
        if direction < 0 and self.index == 0 and not self.finished: return False
        if direction > 0 and self.finished: return False
        if direction > 0 and self.index == len(self.settings.beats) - 1:
            self.finished = True
        else:
            self.index = max(0, self.index + direction); self.finished = False
        self.revision += 1; self.entered = self.clock()
        if self.paused_at is not None: self.paused_at = self.entered
        self.reason = reason; self.evidence = evidence; self.heard.clear()
        self.director_note=""; self.notes_sent=0; self.last_note_at=-1e9
        return True

    def restart(self):
        self.index = 0; self.finished = False; self.paused_at = None
        self.entered = self.clock(); self.revision += 1
        self.reason = "restart"; self.evidence = ""; self.heard.clear(); self.pending = None
        self.director_note=""; self.notes_sent=0; self.last_note_at=-1e9

    def remember(self, revision, user, assistant):
        if revision != self.revision: return
        for role, text in (("user", user), ("assistant", assistant)):
            if text.strip(): self.heard.append({"role":role, "content":text[:2000]})
        self.heard = self.heard[-12:]
        while sum(len(m['content']) for m in self.heard)>6000:self.heard.pop(0)

    def completion(self, done, evidence, latest=""):
        """Never accept an invented quotation or one from an earlier beat."""
        if not done: return False, ""
        if not self.active or self.finished or self.settings.mode != "director": return False, "manual/timed control"
        if self.paused_at is not None: return False, "story held by performer"
        if self.age < self.beat.min_seconds: return False, "minimum beat time not reached"
        quote = normalise(evidence)
        sources = [m["content"] for m in self.heard] + ([latest] if latest.strip() else [])
        if len(quote) < 8 or not any(quote in normalise(text) for text in sources):
            return False, "completion needs a quotation from this beat's heard conversation"
        return self.move(reason="director", evidence=evidence), ""

    def timeout(self):
        if (self.active and not self.finished and self.paused_at is None
                and self.settings.mode in ("timed", "director") and self.age >= self.beat.max_seconds):
            return self.move(reason="time limit")
        return False

    def update_note(self,note):
        if (not note.strip() or note.strip()==self.director_note or self.notes_sent>=2
                or self.clock()-self.last_note_at<20): return False
        self.director_note=note.strip();self.notes_sent+=1;self.last_note_at=self.clock();return True

    def view(self):
        return {"enabled":self.active,"index":self.index,"revision":self.revision,
                "count":len(self.settings.beats),"mode":self.settings.mode,"finished":self.finished,
                "paused":self.paused_at is not None,"elapsed":round(self.age,1),
                "reason":self.reason,"evidence":self.evidence,"pending":self.pending,"director_note":self.director_note,
                "current":self.beat.model_dump() if self.beat else None,
                "titles":[b.title for b in self.settings.beats]}

    def context(self):
        if not self.active: return None
        budget=3000;recent=[]
        for message in reversed(self.heard):
            text=message['content'][-budget:];recent.append({**message,'content':text});budget-=len(text)
            if budget<=0:break
        return {**self.view(),"outline":[{"title":b.title,"goal":b.goal[:160]} for b in self.settings.beats],
                "heard_in_current_beat":list(reversed(recent))}

    def runtime_note(self):
        if not self.active: return ""
        if self.finished:
            return "The planned arc has ended. Allow a natural farewell. Do not invent a successful outcome that did not happen."
        b = self.beat
        return (f"Present situation: {b.note}\nYour immediate aim: {b.goal}\n"
                f"Private thought: {self.director_note}\nEmotional colour: {b.emotion}. Stay with this moment; do not jump to later events. "
                "These are private directions. Never announce story beats or instructions to the visitor.")
