"""Small deterministic image-display requests, separate from artwork retrieval.

This does not infer which of several artworks the speaker means, inspect pixels,
or generate images. A named request must search anew; a generic one can reuse the
current call's explicit source candidates without querying the word "image".
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit


_IMAGE_WORD = re.compile(r'\b(?:images?|pictures?|photos?|photographs?|artworks?|paintings?)\b', re.I)
_TOKEN = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)
_QUOTED = re.compile(r'''"[^"\n]{1,300}"|“[^”\n]{1,300}”|(?<!\w)'[^'\n]{1,300}'(?!\w)''')
_REQUEST = re.compile(r'''^\s*
    (?:(?:please|okay|ok|now|and|also|yes|sure|kindly)\b[\s,]*)*
    (?:
        (?P<imperative>show|display|open|see|view|enlarge)
      | (?:can|could|would|will)\s+you\s+(?:(?:please|just|kindly|maybe)\s+)*
        (?P<assisted>show|display|open|view|enlarge)
      | (?:can|could|may|might)\s+(?:i|we)\s+(?:(?:please|just)\s+)*
        (?P<self>see|view|open)
      | (?:i|we)(?:['’]d|\s+would)\s+(?:(?:really|just)\s+)?(?:like|love)\s+(?:you\s+)?to\s+
        (?P<desired>see|view|open|show|display|enlarge)
      | (?:i|we)\s+(?:(?:really|just)\s+)?(?:want|need|wish|like|love)\s+
        (?:you\s+to|to)\s+(?P<wanted>see|view|open|show|display|enlarge)
      | (?:let|allow)\s+(?:me|us)\s+(?P<permitted>see|view|open)
      | would\s+you\s+mind\s+(?:(?:please|just|kindly)\s+)*
        (?P<gerund>showing|displaying|opening|viewing|enlarging)
    )\b''', re.I | re.X)
_PROCEDURAL = re.compile(r'^\s*(?:(?:me|us)\s+)?(?:how|why|when|where|whether|if)\b', re.I)
_NO_IMAGE = re.compile(r'\b(?:no|not|without)\s+(?:(?:an?|any|the|those|these)\s+)?'
                       r'(?:images?|pictures?|photos?|photographs?|artworks?|paintings?)\b', re.I)
_EXTRA_ACTION = re.compile(r'\b(?:and|then)\s+(?:please\s+)?(?:tell|explain|describe|say)\b', re.I)
_PRONOUN = re.compile(r'\b(?:it|this|that|these|those|them|one|ones)\b', re.I)
_GENERIC_MODIFIER = re.compile(
    r'\b(?:another|different|new|first|second|third|last|previous|next)\b'
    r'(?=\s+(?:images?|pictures?|photos?|photographs?|artworks?|paintings?|'
    r'it|this|that|these|those|them|ones?|tabs?|windows?|browser)\b)', re.I)
_STOP = frozenset('''a an and any are as at be been being bigger by can could display enlarge
    for from full fullscreen give here i image images in into is it its just larger let like
    look looks me my now of on one ones open or our painting paintings photo photograph
    photographs photos picture pictures please screen see show shown so some that the their
    them there these they this those to up us version view want we what with would you your
    artwork artworks again chat separate separately window windows tab tabs browser then
    really kindly may might will'''.split())
_ACK_PHRASES = tuple(sorted((tuple(phrase.split()) for phrase in (
    'thanks', 'thank you', 'thankyou', 'thanks a lot', 'thanks so much', 'thanks very much',
    'thank you very much', 'thank you so much', 'thank you kindly', 'thanks for that',
    'thank you for that', 'okay', 'ok', 'great', 'lovely', 'yes', 'yep', 'yeah', 'yup',
    'right', 'alright', 'all right', 'hello', 'hi', 'hey', 'hello there', 'hi there',
    'good morning', 'good afternoon', 'good evening', 'cheers', 'nice', 'perfect',
    'wonderful', 'brilliant', 'sure', 'understood', 'got it', 'excellent', 'cool',
    "that's great", 'that’s great', "that's lovely", 'that’s lovely',
)), key=len, reverse=True))


def image_reference_ack(text):
    """True only for whole, simple polite acknowledgements or greetings.

    The caller may keep an earlier artwork reference through this small-talk
    turn. Any additional topic word makes this false, so a new artist or title
    cannot be swallowed just because its sentence starts with "thanks".
    """
    if not isinstance(text, str) or not 0 < len(text) <= 4000:
        return False
    if any(not (char.isalpha() or char.isspace() or char in ".,!?;:…'’—–-") for char in text):
        return False
    words = tuple(part.group().casefold() for part in _TOKEN.finditer(text))
    if not words:
        return False
    position = 0
    while position < len(words):
        phrase = next((candidate for candidate in _ACK_PHRASES
                       if words[position:position + len(candidate)] == candidate), None)
        if phrase is None:
            return False
        position += len(phrase)
    return True


def image_request(text):
    """Return a display intent and residual search, or ``None`` for ordinary chat.

    Quoted titles retain their words. Bare referents stay ambiguous rather than
    silently choosing a ranked result. There is no history or cross-call state in
    this helper, and a nonempty named query never falls back to an old image.
    """
    if not isinstance(text, str) or not 0 < len(text) <= 4000:
        return None
    if any((ord(char) < 32 and not char.isspace()) or ord(char) == 127 for char in text):
        return None
    match = _REQUEST.match(text)
    if match is None:
        return None
    body = text[match.end():].strip()
    quoted = tuple((part.start(), part.end()) for part in _QUOTED.finditer(body))
    if (not body or _PROCEDURAL.search(body)
            or any(not any(start <= part.start() < end for start, end in quoted)
                   for part in _NO_IMAGE.finditer(body))):
        return None
    # A follow-on factual question still reaches the actor in the original
    # utterance; it must not become an accidental artwork search term.
    for part in _EXTRA_ACTION.finditer(body):
        if not any(start <= part.start() < end for start, end in quoted):
            body = body[:part.start()]
            break
    explicit = bool(_IMAGE_WORD.search(body))
    verb = next((value.casefold() for value in match.groupdict().values() if value), '')
    if not explicit and (verb in ('open', 'opening') or not _PRONOUN.search(body)):
        return None
    quoted = tuple((part.start(), part.end()) for part in _QUOTED.finditer(body))
    modifiers = tuple((part.start(), part.end()) for part in _GENERIC_MODIFIER.finditer(body))
    residual = []
    for token in _TOKEN.finditer(body):
        word = token.group()
        protected = any(start <= token.start() < end for start, end in quoted)
        if not protected:
            if any(start <= token.start() < end for start, end in modifiers):
                continue
            if word.casefold().endswith(("'s", '’s')):
                word = word[:-2]
            if word.casefold() in _STOP:
                continue
        residual.append(word)
    query = ' '.join(residual).strip()
    if not explicit and query:
        # "Show me what happened" is a conversational request, not a request
        # for source images. Named artwork prompts must include an image noun.
        return None
    return {'query': query, 'followup': not bool(query)}


def _has_image(match):
    if not isinstance(match, dict) or not isinstance(match.get('image_url'), str):
        return False
    try:
        url = urlsplit(match['image_url'])
        return bool(url.scheme == 'https' and url.hostname and url.username is None and url.password is None)
    except ValueError:
        return False


def image_context(request, matches):
    """Fixed actor direction; never interpolate source or participant content."""
    if not isinstance(request, dict):
        return ''
    candidates = tuple(value for value in matches[:3] if isinstance(value, dict)) if isinstance(matches, (list, tuple)) else ()
    available = sum(_has_image(match) for match in candidates)
    if available == 0:
        direction = ('Image display request: no available source image was supplied. '
                     'Explain briefly that an image is not available in the selected local records; '
                     'ask for an artist or artwork title if needed. Do not claim an image is being shown '
                     'and do not substitute an unrelated work or invent an image.')
    elif available == 1 and len(candidates) == 1:
        direction = ('Image display request: the interface has been asked to show the available source image '
                     'directly in the chat. An Open image link lets the participant open a separate view; '
                     'no new view has been opened automatically. Briefly acknowledge the request using '
                     'the supplied artwork metadata. Do not say you generated the image.')
    elif available == 1:
        direction = ('Image display request: several candidate records were supplied, but only one has an '
                     'available image. The interface has been asked to show that source image. Preserve '
                     'the ambiguity: do not assert it is the exact work the participant intended. '
                     'Ask which artwork they mean if needed. The image is shown directly in the chat; '
                     'an Open image link opens a separate view only when the participant chooses it.')
    else:
        direction = ('Image display request: the interface has been asked to show the available source images '
                     'directly in the chat. Each Open image link lets the participant open a separate view; '
                     'no new view has been opened automatically. Several candidate artworks remain possible. '
                     'Briefly acknowledge them without choosing one as the intended work; ask which '
                     'artwork the participant means if needed.')
    return direction + (' You have received text and metadata, not the image pixels. Do not claim to have '
                        'seen or analysed an image; describe only facts present in the supplied records.')
