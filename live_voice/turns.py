from collections import deque
import re

RATE = 16000
FRAME_BYTES = 640  # 20 ms, mono PCM16
MAX_RECENT_TURNS = 12
MAX_ARCHIVE_TURNS = 100
MAX_ARCHIVE_CHARS = 100_000
MAX_CONTEXT_CHARS = 10_000
MAX_OLDER_CONTEXT_TURNS = 3
OLDER_CONTEXT_CHARS = 3_000

_MEMORY_STOP_WORDS = set('''a an and are as at be been but by did do does for from had has have
    how i if in is it me my of on or our please said say so that the their them then there
    they this to told us was we were what when where which who why will with would you your
    remember recall earlier before again mention mentioned tell about can could'''.split())
_MEMORY_QUESTION = re.compile(r'\b(?:remember|recall|earlier|before|again|my name)\b|who am i', re.I)
_CORRECTION = re.compile(r'\b(?:actually|correction|i meant|i was wrong|instead|no longer|not anymore)\b', re.I)


def memory_terms(text):
    """A small lexical index, not inferred facts or an LLM-generated summary."""
    words = re.findall(r"[^\W_]+(?:['’][^\W_]+)?", text.casefold())
    normalized = {'favourite':'favorite', 'favourites':'favorite', 'favorites':'favorite',
                  'names':'name', 'books':'book', 'colours':'color', 'colour':'color', 'colors':'color'}
    return {normalized.get(word, word) for word in words if word not in _MEMORY_STOP_WORDS}


class TurnDetector:
    """Fixed-frame VAD with pre-roll and bounded utterances, independent of network packet sizes."""
    def __init__(self, is_speech, silence_ms=400, max_seconds=15, prepare_ms=200):
        self.is_speech = is_speech
        self.silence_frames = max(10, silence_ms // 20)
        self.prepare_frames = max(1, prepare_ms // 20)
        self.max_frames = max_seconds * 50
        self.pending = bytearray()
        self.reset()

    def reset(self):
        self.preroll = deque(maxlen=10)
        self.votes = deque(maxlen=5)
        self.frames = []
        self.active = False
        self.silent = self.voiced = 0
        self.prepared = False

    def selected_audio(self):
        # Both provisional and final recognition use the same 80 ms tail. A
        # continued quiet pause therefore matches byte for byte; resumed speech
        # changes the final PCM and cannot reuse an earlier partial transcript.
        trim = max(0, self.silent - 4)
        return b"".join(self.frames[:-trim] if trim else self.frames)

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
            if (not self.prepared and self.voiced >= 5
                    and self.prepare_frames <= self.silent < self.silence_frames
                    and len(self.frames) < self.max_frames):
                self.prepared = True
                events.append(("prepare", self.selected_audio(), self.silent * 20))
            if self.silent >= self.silence_frames or len(self.frames) >= self.max_frames:
                if self.voiced >= 5:
                    events.append(("utterance", self.selected_audio(), self.silent * 20))
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
    """Bounded, active-call memory; only playback acknowledgements store AI words.

    Older turns remain in RAM for deterministic retrieval. Nothing is written to
    disk, shared between calls, or summarized into newly asserted facts.
    """
    def __init__(self):
        self.messages = []
        self.current = None
        self.phrases = {}
        self.next_ack = 0
        self.archive = []
        self._sequence = 0
        self._current_record = None
        self._message_records = {}

    def begin(self, turn_id, user_text, source='participant'):
        if source not in ('participant', 'stage'): raise ValueError('Unknown conversation source.')
        user = {"role":"user", "content":user_text}
        assistant = {"role":"assistant", "content":""}
        self.messages.extend([user, assistant])
        self.messages = self.messages[-2 * MAX_RECENT_TURNS:]
        record = {'sequence':self._sequence, 'user':user_text if source == 'participant' else '',
                  'assistant':'', 'source':source}
        self._sequence += 1
        self._current_record = record
        for message in (user, assistant): self._message_records[id(message)] = (message, record)
        live = {id(message) for message in self.messages}
        self._message_records = {key:value for key,value in self._message_records.items() if key in live}
        # Store only the stage-cued AI speech after acknowledgement. Its private
        # cue directs this turn but can never become a participant memory quote.
        if source == 'participant': self.archive.append(record)
        self._prune_archive()
        self.current = turn_id
        self.phrases = {}; self.next_ack = 0

    def _prune_archive(self):
        characters = sum(len(turn['user']) + len(turn['assistant']) for turn in self.archive)
        while self.archive and (len(self.archive) > MAX_ARCHIVE_TURNS or characters > MAX_ARCHIVE_CHARS):
            old = self.archive.pop(0)
            characters -= len(old['user']) + len(old['assistant'])

    def add_phrase(self, sequence, text):
        self.phrases[sequence] = text

    def acknowledge(self, turn_id, sequence):
        if turn_id != self.current or sequence != self.next_ack or sequence not in self.phrases:
            return False
        target = self.messages[-1]
        target["content"] = (target["content"] + " " + self.phrases[sequence]).strip()
        if self._current_record is not None:
            self._current_record['assistant'] = target['content']
            if (self._current_record['source'] == 'stage'
                    and not any(turn is self._current_record for turn in self.archive)):
                self.archive.append(self._current_record)
            self._prune_archive()
        self.next_ack += 1
        return True

    def interrupt(self):
        heard = self.messages[-1]["content"] if self.current is not None else ""
        self.current = None
        return heard

    @staticmethod
    def _record_messages(record):
        return [{'role':role, 'content':record[key]} for role,key in (('user','user'),('assistant','assistant'))
                if record[key].strip()]

    def _recent_groups(self):
        groups = []
        for message in self.messages:
            role, content = message.get('role'), message.get('content')
            if role not in ('user','assistant') or not isinstance(content,str) or not content.strip(): continue
            mapped = self._message_records.get(id(message))
            record = mapped[1] if mapped and mapped[0] is message else None
            copied = {'role':role, 'content':content}
            if record and record['source'] == 'stage':
                if role == 'user' and not (record is self._current_record and self.current is not None):
                    continue
                if role == 'assistant':
                    groups.append([copied])
                    continue
            if role == 'user' or not groups or groups[-1][-1]['role'] == 'assistant': groups.append([copied])
            else: groups[-1].append(copied)
        return groups

    def _older_candidates(self, latest):
        recent = {mapped[1]['sequence'] for mapped in self._message_records.values()}
        query = memory_terms(latest)
        is_memory_question = bool(_MEMORY_QUESTION.search(latest))
        ranked = []
        previous_terms = set()
        for index, record in enumerate(self.archive):
            if not record['user'].strip() and not record['assistant'].strip(): continue
            user_terms = memory_terms(record['user'])
            terms = user_terms | memory_terms(record['assistant'])
            correction = bool(_CORRECTION.search(record['user']))
            # An immediately following correction can refer to the prior topic
            # without repeating its nouns. Only the original quotes are returned.
            indexed = terms | (previous_terms if correction else set())
            previous_terms = indexed
            if record['sequence'] in recent: continue
            overlap = query & indexed
            score = len(overlap) + len(query & user_terms)
            if 'name' in query:
                explicit = re.search(r'\b(?:my name is|call me)\b', record['user'], re.I)
                introduction = re.search(r"\bI(?: am|['’]m)\s+([A-Z][^\W_]+)", record['user'])
                if explicit or introduction: score += 8
            if correction and overlap: score += 1
            if is_memory_question and index < 2: score += .25
            if score: ranked.append((score, record['sequence'], record))
        ranked.sort(key=lambda value:(value[0],value[1]), reverse=True)
        return [value[2] for value in ranked[:MAX_OLDER_CONTEXT_TURNS]]

    @staticmethod
    def _fit_group(group, budget):
        if budget <= 0: return []
        if sum(len(item['content']) for item in group) <= budget:
            return [dict(item) for item in group]
        # Oversized imported/manual context is bounded too. This clips original
        # text rather than generating a summary or inventing replacement facts.
        limit = budget // len(group)
        return [{'role':item['role'], 'content':item['content'][:limit]} for item in group if limit]

    def context_messages(self, latest='', budget=MAX_CONTEXT_CHARS):
        """Return copied heard context; latest is a retrieval query, not appended.

        Up to three relevant older turns precede the recent turns, in original
        chronology. Later corrections therefore remain later than earlier claims.
        Director callers can supply a new utterance before begin(); actor callers
        use prompt_messages() after begin(), which already contains that utterance.
        A smaller budget retains retrieval within that budget, rather than making
        callers discard the oldest retrieved quotes after assembling the context.
        """
        if type(budget) is not int: raise ValueError('Conversation context budget must be an integer.')
        budget = max(0, min(budget, MAX_CONTEXT_CHARS))
        if not budget: return []
        older = self._older_candidates(latest) if latest.strip() else []
        reserve = min(OLDER_CONTEXT_CHARS, budget // 3) if older else 0
        recent_budget = budget - reserve
        recent_groups = []
        for group in reversed(self._recent_groups()):
            size = sum(len(item['content']) for item in group)
            if size > recent_budget and recent_groups: break
            copied = self._fit_group(group, recent_budget)
            recent_groups.append(copied)
            recent_budget -= sum(len(item['content']) for item in copied)
        recent_groups.reverse()
        remaining = min(OLDER_CONTEXT_CHARS,
                        budget - sum(len(item['content']) for group in recent_groups for item in group))
        selected = []
        for record in older:
            group = self._fit_group(self._record_messages(record), min(remaining, OLDER_CONTEXT_CHARS))
            if group:
                selected.append((record['sequence'], group))
                remaining -= sum(len(item['content']) for item in group)
        selected.sort(key=lambda value:value[0])
        return [item for _,group in selected for item in group] + [item for group in recent_groups for item in group]

    def prompt_messages(self):
        latest = next((message['content'] for message in reversed(self.messages) if message.get('role') == 'user'), '')
        return self.context_messages(latest=latest)
