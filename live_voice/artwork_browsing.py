"""Small call-local artwork browsing intent, independent of retrieval and pixels.

This remembers a participant's query and constraints, not inferred facts about
artworks. It cannot decide whether a painting is colourful from its medium or
title. The catalogue search and the actual selected-image feedback remain the
authorities for records and display availability.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import islice
import re

from .artwork_requests import (image_correction_request, image_reference_ack,
                              image_reference_question, image_reference_selection, image_request)
from .artwork_retrieval import one_edit


MAX_KEYS = 100
MAX_QUERY = 200
_TOKEN = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)
_NAMED = re.compile(r'\b(?:called|named|titled)\s+(?P<title>.+)$', re.I)
_QUOTED = re.compile(r'''["“](?P<title>[^"”\n]{1,300})["”]|(?<!\w)'(?P<single>[^'\n]{1,300})'(?!\w)''')
_ART = re.compile(r'\b(?:art|artworks?|paintings?|pictures?|images?|artists?|prints?|wood\s*cuts?|drawings?|sculptures?|photos?|photographs?|engravings?|etchings?|lithographs?|watercolou?rs?)\b', re.I)
_IMAGE = re.compile(r'\b(?:images?|pictures?|photos?|photographs?)\b', re.I)
_SHOW = re.compile(r'\b(?:show|display|view|see)\b', re.I)
_MORE = re.compile(r'\b(?:another|different|more|other|next)\b', re.I)
_INSTEAD = re.compile(r'\b(?:instead|rather|only)\b', re.I)
_MORE_FACTS = re.compile(r'\b(?:more\s+(?:information|details?|facts?|context)|tell\s+(?:me\s+)?more\s+about)\b', re.I)
_NEGATIVE_SEARCH = re.compile(r'\b(?:(?:do\s+not|don[\'’]t)\s+(?:show|find|want|need)|not\s+interested\s+in)\b', re.I)
_NEGATIVE_IMAGE = re.compile(r'\b(?:without|no|not)\s+(?:(?:an?|any|the)\s+)?(?:pictures?|images?|photos?|photographs?)\b', re.I)
_HESITATION = re.compile(r'(?:(?:um+|uh+|erm|er|hmm+|ah+)[\s,.!?…;:—–-]*)+', re.I)
_STATUS = re.compile(
    r'(?:any\s+luck(?:\s+(?:in|with|on)\s+that)?|'
    r'(?:did|have)\s+you\s+(?:find|found)\s+(?:anything|any|something)(?:\s+yet)?|'
    r'(?:have\s+you\s+had|having)\s+any\s+luck|'
    r'anything\s+yet)[\s,.!?…]*', re.I)
_UNAVAILABLE = re.compile(
    r'(?:(?:no|well|sorry)[\s,]+)?(?:'
    r'i\s+(?:do\s+not|don[\'’]t)\s+think\s+'
    r'(?:(?:it|this|that)(?:[\'’]s|\s+is)|the\s+(?:image|picture|artwork|painting)\s+is)\s+available'
    r'|'
    r'(?:it|this|that|this\s+one|that\s+one|the\s+(?:image|picture|artwork|painting))'
    r'(?:\s+(?:is|was)\s+not|\s+isn[\'’]t|\s+wasn[\'’]t|[\'’]s\s+not|\s+not)\s+available'
    r'|(?:none|nothing)(?:\s+(?:is|was|are|were))?\s+available'
    r'|(?:there\s+(?:are|were)\s+)?no\s+(?:images?|pictures?)(?:\s+(?:are|were))?\s+available'
    r')[\s,.!?…]*', re.I)
_CLEAR_MEDIUM = re.compile(r'\b(?:any\s+medium|all\s+media|any\s+type\s+of\s+art|not\s+just\s+paintings?)\b', re.I)
_MEDIUM = {
    'painting':'painting', 'paintings':'painting',
    'drawing':'drawing', 'drawings':'drawing', 'print':'print', 'prints':'print',
    'printing':'print', 'printings':'print',
    'woodcut':'woodcut', 'woodcuts':'woodcut',
    'engraving':'engraving', 'engravings':'engraving',
    'etching':'etching', 'etchings':'etching',
    'lithograph':'lithograph', 'lithographs':'lithograph',
    'photograph':'photograph', 'photographs':'photograph', 'photo':'photograph', 'photos':'photograph',
    'sculpture':'sculpture', 'sculptures':'sculpture',
    'watercolor':'watercolor', 'watercolors':'watercolor', 'watercolour':'watercolor', 'watercolours':'watercolor',
    'gouache':'gouache', 'tempera':'tempera', 'acrylic':'acrylic', 'oil':'oil',
}
_FRAMING = frozenset('''a an the and or to of for by in on at from with without please
    can could may would will do does did you your i we me us my our have has are is be some
    any something anything there these those this that it them one ones another other more
    different next new instead rather only looking look searching search find show display
    see view give get tell want wanted like love need works work art artwork artworks pieces piece
    image images picture pictures available availability called named titled artist artists
    painter painters about again now also just really maybe let try okay ok yes no uh um erm
    ah well think don’t don't not say kind kinds type types medium media all luck yet
    what which how something i'm i’m im interested sure honest honestly but'''.split()) | frozenset(_MEDIUM)


def _key(value):
    if (not isinstance(value, (tuple, list)) or len(value) != 2
            or any(not isinstance(part, str) or not 0 < len(part) <= 128 for part in value)):
        return None
    return tuple(value)


def _keys(matches):
    if not isinstance(matches, (tuple, list)):
        return ()
    result = []
    for match in matches[:3]:
        if not isinstance(match, dict): continue
        value = _key((match.get('collection_id'), match.get('id')))
        if value is not None and value not in result: result.append(value)
    return tuple(result)


def _bounded_add(values, additions):
    result = list(values)
    for item in islice(additions, MAX_KEYS):
        value = _key(item)
        if value is None: continue
        if value in result: result.remove(value)
        result.append(value)
    return tuple(result[-MAX_KEYS:])


def _words(text):
    return tuple(token.group() for token in _TOKEN.finditer(text))


def _medium(word):
    normalized = word.casefold()
    if normalized in _MEDIUM:
        return _MEDIUM[normalized]
    # Match the catalogue filter's bounded one-edit tolerance for this common
    # spoken/typed medium. Exact print/printing remains a different medium.
    if (normalized.startswith('pa') and normalized.isascii() and normalized.isalpha() and 6 <= len(normalized) <= 10
            and any(one_edit(normalized, canonical) for canonical in ('painting', 'paintings'))):
        return 'painting'
    return ''


@dataclass(frozen=True)
class BrowsingPlan:
    query: str
    require_images: bool
    medium: str
    exclude_keys: frozenset[tuple[str, str]]
    fresh_search: bool
    reason: str


@dataclass
class ArtworkBrowsing:
    query: str = ''
    require_images: bool = False
    medium: str = ''
    active: bool = False
    seen_keys: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    failed_keys: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    last_keys: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    selected_key: tuple[str, str] | None = None
    _more_keys: tuple[tuple[str, str], ...] = field(default_factory=tuple, repr=False)

    def observe(self, matches, selected_key=None):
        """Remember only published candidate identities, never source bodies."""
        self.last_keys = _keys(matches)
        self.seen_keys = _bounded_add(self.seen_keys, self.last_keys)
        selected = _key(selected_key)
        self.selected_key = selected if selected in self.last_keys else None

    def fail(self, selected_key):
        """Exclude the reported selected artwork; other candidates stay usable."""
        selected = _key(selected_key)
        if selected is not None:
            self.failed_keys = _bounded_add(self.failed_keys, (selected,))

    def restore(self, selected_key):
        """A successful browser load supersedes that image's earlier failure."""
        selected = _key(selected_key)
        if selected is not None:
            self.failed_keys = tuple(value for value in self.failed_keys if value != selected)

    def _plan(self, reason, fresh_search, *, more=False):
        if fresh_search:
            self._more_keys = self.seen_keys if more else ()
        # A status/hesitation after an empty "more" result retains the same
        # exclusions; it cannot silently bring the exhausted page back.
        excluded = _bounded_add(self._more_keys, self.failed_keys)
        return BrowsingPlan(self.query, self.require_images, self.medium,
                            frozenset(excluded), fresh_search, reason)

    def plan(self, text, matches=(), *, selected_key=None, failed_keys=()):
        """Return bounded retrieval intent while leaving ordinary referents alone.

        Named and quoted titles are protected before interpreting short status
        phrases. A filler alone never starts browsing or overwrites its topic.
        Call ``observe`` only after the corresponding sources were published.
        """
        if not isinstance(text, str) or not 0 < len(text) <= 4000:
            return None
        if any((ord(char) < 32 and not char.isspace()) or ord(char) == 127 for char in text):
            return None
        self.failed_keys = _bounded_add(self.failed_keys, failed_keys)
        text = text.strip()
        # The established correction parser extracts the wanted half and
        # excludes the rejected title. Never turn that whole sentence into a
        # fresh browse query containing both identities.
        if image_correction_request(text) is not None: return None
        named = _NAMED.search(text)
        quoted = _QUOTED.search(text) if _ART.search(text) else None
        protected = bool(named or quoted)
        if not protected:
            if _NEGATIVE_SEARCH.search(text): return None
            if _HESITATION.fullmatch(text):
                return self._plan('hesitation', False) if self.active else None
            if _STATUS.fullmatch(text):
                return self._plan('status', False) if self.active else None
            if _UNAVAILABLE.fullmatch(text):
                if not self.active: return None
                selected = _key(selected_key) or self.selected_key
                # If no explicit selection was ever reported, do not mark all
                # candidates failed just because the utterance is plural.
                self.fail(selected)
                self.require_images = True
                return self._plan('availability', True)
            if _NEGATIVE_IMAGE.search(text): return None
            if image_reference_ack(text) or image_reference_question(text):
                return None
        explicit_image = bool(_IMAGE.search(text))
        more = bool(not protected and _MORE.search(text) and not _MORE_FACTS.search(text))
        medium_text = text[:named.start()] if named else text
        if quoted:
            medium_text = text[:quoted.start()] + text[quoted.end():]
        medium_text = re.sub(r'\bwood\s+cuts?\b', 'woodcut', medium_text, flags=re.I)
        mediums = [value for word in _words(medium_text) if (value := _medium(word))]
        chosen_medium = next((value for value in mediums if value != 'painting'), mediums[0] if mediums else '')
        clear_medium = bool(_CLEAR_MEDIUM.search(medium_text))
        if named:
            query = named['title'].strip(' \t.,!?“”\"\'')[:MAX_QUERY]
        elif quoted:
            query = (quoted['title'] or quoted['single']).strip()[:MAX_QUERY]
        else:
            query_text = re.sub(r'\bwood\s+cuts?\b', 'woodcut', _CLEAR_MEDIUM.sub(' ', text), flags=re.I)
            query = ' '.join(word for word in _words(query_text)
                             if word.casefold() not in _FRAMING and not _medium(word))[:MAX_QUERY].strip()
        if not protected and not more and not _INSTEAD.search(text):
            image = image_request(text)
            # Existing exact referent/medium selection has precedence, e.g.
            # 'show me it' and 'can I see the woodcut' among current records.
            selected = image_reference_selection(image, matches) if image else None
            if image and (image['followup'] or selected is not None or (not query and chosen_medium and matches)):
                if explicit_image: self.require_images = True
                return None
            if _MORE_FACTS.search(text): return None
        if not query and not chosen_medium and not clear_medium:
            if more and self.active:
                self.require_images = self.require_images or explicit_image or bool(_SHOW.search(text))
                return self._plan('more', True, more=True)
            return None
        if not self.active or (query and not more):
            self.query = query
        elif query:
            self.query = query
        self.active = True
        self.require_images = self.require_images or explicit_image or bool(_SHOW.search(text))
        if clear_medium: self.medium = ''
        elif chosen_medium: self.medium = chosen_medium
        return self._plan('more' if more else 'topic', True, more=more)
