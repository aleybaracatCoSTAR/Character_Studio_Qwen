from collections import deque
import re

RATE = 16000
FRAME_BYTES = 640  # 20 ms, mono PCM16


class TurnDetector:
    """Fixed-frame VAD with pre-roll and bounded utterances, independent of network packet sizes."""
    def __init__(self, is_speech, silence_ms=480, max_seconds=15):
        self.is_speech = is_speech
        self.silence_frames = max(10, silence_ms // 20)
        self.max_frames = max_seconds * 50
        self.pending = bytearray()
        self.reset()

    def reset(self):
        self.preroll = deque(maxlen=10)
        self.votes = deque(maxlen=5)
        self.frames = []
        self.active = False
        self.silent = self.voiced = 0

    def feed(self, packet):
        self.pending.extend(packet)
        events = []
        while len(self.pending) >= FRAME_BYTES:
            frame = bytes(self.pending[:FRAME_BYTES]); del self.pending[:FRAME_BYTES]
            speech = self.is_speech(frame)
            if not self.active:
                self.preroll.append(frame); self.votes.append(bool(speech))
                if sum(self.votes) >= 3:
                    self.active = True
                    self.frames = list(self.preroll)
                    self.voiced = sum(self.votes)
                    events.append(("start", None, 0))
                continue
            self.frames.append(frame)
            self.voiced += int(speech)
            self.silent = 0 if speech else self.silent + 1
            if self.silent >= self.silence_frames or len(self.frames) >= self.max_frames:
                if self.voiced >= 5:
                    # Keep 80 ms of the trailing pause, rather than feeding half a second of silence to ASR.
                    trim = max(0, self.silent - 4)
                    selected = self.frames[:-trim] if trim else self.frames
                    events.append(("utterance", b"".join(selected), self.silent * 20))
                else:
                    events.append(("noise", None, 0))
                self.reset()
        return events


class PhraseChunker:
    """Stream sentences or clauses without mistaking a partial token for an end.

    Expressive speech keeps short sentences intact. A long sentence still has a
    bounded clause/word fallback, so a missing full stop cannot stall the stream.
    """
    punctuation = re.compile(r'''[.!?;:,](?:["”’')\]])*''')
    abbreviation = re.compile(r'\b(?:Mr|Mrs|Ms|Dr|Prof|St)\.$', re.IGNORECASE)

    def __init__(self, target_chars=85, sentence_only=False):
        self.buffer = ""
        self.target_chars = max(1, target_chars)
        self.sentence_only = sentence_only

    def boundaries(self, final):
        """Yield confirmed punctuation boundaries, including their closing quotes."""
        for match in self.punctuation.finditer(self.buffer):
            mark = match.group()[0]
            end = match.end()
            # An LLM delta can end after the first dot of an ellipsis, before a
            # closing quote, or halfway through a decimal. Wait for lookahead.
            if end == len(self.buffer):
                if not final: continue
            elif not self.buffer[end].isspace():
                continue
            if mark == '.':
                start = match.start()
                # ASCII ellipses are hesitation, just like the Unicode … form.
                # Never detach one or two remaining dots into the next request.
                if start and self.buffer[start - 1] == '.': continue
                if start + 1 < len(self.buffer) and self.buffer[start + 1] == '.': continue
                if self.abbreviation.search(self.buffer[:start + 1]): continue
            yield end, mark

    def fallback(self, boundaries):
        if len(self.buffer) < self.target_chars: return None
        # Prefer a natural clause when an expressive sentence is unusually long.
        clauses = [end for end, mark in boundaries
                   if mark in ',;:' and end <= self.target_chars
                   and len(self.buffer[:end].split()) >= 5]
        if clauses: return clauses[-1]
        spaces = [m.start() for m in re.finditer(r'\s+', self.buffer) if m.start()]
        before = [position for position in spaces if position <= self.target_chars]
        if before: return before[-1]
        # A single word can exceed the target. Preserve it rather than truncating
        # it or splitting its spoken syllables between synthesis requests.
        return spaces[0] if spaces else None

    def feed(self, delta, final=False):
        self.buffer += delta
        phrases = []
        while self.buffer.strip():
            boundaries = list(self.boundaries(final))
            boundary = next((end for end, mark in boundaries
                             if end <= self.target_chars
                             and (mark in '.!?' if self.sentence_only else len(self.buffer[:end].split()) >= 5)), None)
            if boundary is None:
                # Do not let the size fallback race the pending punctuation at
                # exactly the limit; the next delta may confirm a sentence end.
                pending_end = any(match.end() == len(self.buffer) and match.group()[0] in '.!?'
                                  and match.start() < self.target_chars
                                  for match in self.punctuation.finditer(self.buffer))
                if not final and pending_end and len(self.buffer) <= self.target_chars:
                    break
                boundary = self.fallback(boundaries)
            if boundary is None:
                break
            phrases.append(self.buffer[:boundary].strip())
            self.buffer = self.buffer[boundary:].lstrip()
        if final and self.buffer.strip():
            phrases.append(self.buffer.strip()); self.buffer = ""
        return phrases


class ReplyHistory:
    """Only fully played phrases enter the assistant's remembered answer."""
    def __init__(self):
        self.messages = []
        self.current = None
        self.phrases = {}
        self.next_ack = 0

    def begin(self, turn_id, user_text):
        self.messages.extend([{"role":"user", "content":user_text}, {"role":"assistant", "content":""}])
        self.messages = self.messages[-12:]
        self.current = turn_id
        self.phrases = {}; self.next_ack = 0

    def add_phrase(self, sequence, text):
        self.phrases[sequence] = text

    def acknowledge(self, turn_id, sequence):
        if turn_id != self.current or sequence != self.next_ack or sequence not in self.phrases:
            return False
        target = self.messages[-1]
        target["content"] = (target["content"] + " " + self.phrases[sequence]).strip()
        self.next_ack += 1
        return True

    def interrupt(self):
        heard = self.messages[-1]["content"] if self.current is not None else ""
        self.current = None
        return heard

    def prompt_messages(self):
        # The unfinished assistant placeholder belongs to the output, not its input.
        selected = [dict(item) for item in self.messages[:-1]]
        # Retain whole older pairs while bounding context for the small text model.
        while len(selected) > 1 and sum(len(item["content"]) for item in selected) > 6000:
            del selected[:2]
        return selected
