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
_FILLERS = re.compile(r'^\s*(?:(?:uh+|um+|erm|er|hmm+|well|oh|so|hey)\b[\s,…]*){1,8}', re.I)
_VISIBILITY_REPAIR = re.compile(
    r"^(?:i|we)\s+(?:still\s+)?(?:can['’]t|cannot|can\s+not|couldn['’]t|could\s+not|don['’]t|do\s+not)"
    r"\s+(?:really\s+)?(?:see|view|find)\s+(?P<object>.+)$", re.I)
_VISIBLE_OBJECT = re.compile(
    r'^(?:(?:(?:the|a|an|any|my|our|this|that|these|those)\s+)?'
    r'(?:images?|pictures?|photos?|photographs?|artworks?|paintings?)|it|them|this|that|these|those)\b', re.I)
_OBJECT_REPAIR = re.compile(
    r"^(?P<object>(?:(?:the|this|that|these|those)\s+)?(?:images?|pictures?|photos?|photographs?|artworks?|paintings?))"
    r"\s+(?:(?:is|are|was|were)\s+not|isn['’]t|aren['’]t|wasn['’]t|weren['’]t|doesn['’]t|don['’]t|won['’]t)"
    r"\s+(?:show(?:ing)?|display(?:ed|ing)?|appear(?:ing)?|visible|load(?:ing)?)(?P<tail>.*)$", re.I)
_REQUEST = re.compile(r'''^\s*
    (?:(?:please|okay|ok|now|and|also|yes|sure|kindly)\b[\s,]*)*
    (?:
        (?P<imperative>show|display|open|see|view|enlarge)
      | (?:can|could|would|will)(?:\s+(?:can|could|would|will)){0,2}\s+you(?:\s+you){0,2}\s+(?:(?:please|just|kindly|maybe)\s+)*
        (?P<assisted>show|display|open|view|enlarge)
      | (?:can|could|may|might)(?:\s+(?:can|could|may|might)){0,2}\s+(?:i|we)\s+(?:(?:please|just)\s+)*
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
_PRONOUN = re.compile(r'\b(?:it|this|that|these|those|them|one|ones|both|all)\b', re.I)
_GENERIC_MODIFIER = re.compile(
    r'\b(?:another|different|new|first|second|third|last|previous|next|both|all|two|three)\b'
    r'(?=\s+(?:images?|pictures?|photos?|photographs?|artworks?|paintings?|'
    r'it|this|that|these|those|them|ones?|tabs?|windows?|browser)\b)', re.I)
_STOP = frozenset('''a an and any are as at be been being bigger by can could display enlarge
    for from full fullscreen give here i image images in into is it its just larger let like
    look looks me my now of on one ones open or our painting paintings photo photograph
    photographs photos picture pictures please screen see show shown so some that the their
    them there these they this those to up us version view want we what with would you your
    artwork artworks again chat separate separately window windows tab tabs browser then
    really kindly may might will clearly properly anywhere yet still'''.split())
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
    text = _FILLERS.sub('', text, count=1).strip()
    repair = _VISIBILITY_REPAIR.match(text)
    if repair and _VISIBLE_OBJECT.match(repair['object']):
        text = 'show ' + repair['object']
    else:
        repair = _OBJECT_REPAIR.match(text)
        if repair:
            tail = {part.group().casefold() for part in _TOKEN.finditer(repair['tail'])}
            if tail - (_STOP | {'loading', 'visible', 'appearing', 'displayed', 'displaying'}):
                return None
            text = 'show ' + repair['object']
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
    if not explicit and query.casefold() in ('both', 'all', 'two', 'three'):
        query = ''
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
    if available == 0 and candidates:
        direction = ('Image display request: the matched source records are present, but none includes an '
                     'eligible image URL in this local import. Explain this specific limitation briefly. '
                     'Do not say the artworks, names, or catalogue records are missing. Do not claim the '
                     'museum itself has no image, and do not substitute an unrelated work or invent a link. '
                     'The participant can still open the original source pages.')
    elif available == 0:
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
    response = (' Explain the missing imported image links and point to the available record cards and original '
                'source links; do not promise an image or claim an image is being shown.' if available == 0 else
                ' Fulfill the available display now. If several works remain possible, ask which one they '
                'would like to discuss instead.')
    return direction + (' The participant has already asked to see the source images. Do not ask again '
                        'whether they want images.' + response + ' '
                        'You have received text and metadata, not the image pixels. Do not claim to have '
                        'seen or analysed an image; describe only facts present in the supplied records.')


def image_capability_context(matches, state=None):
    """Always-available interface facts, including records without image URLs."""
    candidates = tuple(value for value in matches[:3] if isinstance(value, dict)) if isinstance(matches, (list, tuple)) else ()
    available = sum(_has_image(match) for match in candidates)
    counts = f'This reply has {len(candidates)} supplied source records and {available} available image URLs. '
    common = ('The application can display available source images directly in this chat, with an Open image '
              'link for a separate view. You must not claim that you cannot show images directly. You receive '
              'metadata and URLs, not image pixels; do not claim to have inspected an image. An available URL '
              'does not prove that the participant\'s browser has loaded it. ')
    if available:
        status = {
            'sent': 'An image-display message was sent; browser loading has not been confirmed. ',
            'loading': 'The matching image display is loading; completion has not been confirmed. ',
            'loaded': 'The browser reported that the matching image display loaded. This is not visual analysis by you. ',
            'unavailable': 'The browser reported that the matching image display was unavailable; the catalogue records and original source links are still present. ',
        }.get(state, '') if isinstance(state, str) else ''
        return counts + common + status + ('Available image URLs are included in the supplied source records. Offer only those '
                                          'images, and preserve uncertainty when several artworks are possible. Never invent '
                                          'another artwork or link as an image offer.')
    if candidates:
        return counts + common + ('The matched artwork records and names are present, but this local import supplies '
                         'no eligible image URLs for them. Say that specifically if asked; do not say that '
                         'their catalogue records are absent. Do not offer unavailable images. The original '
                         'source pages remain available as links.')
    return counts + common + ('No matching source records were supplied for this reply. Ask for an artist or title '
                     'when appropriate, without inventing an image or claiming to search the entire live museum.')


_REFERENT = (r'(?:it|them|they|this|that|these|those)(?:\s+(?:one|ones|artworks?|paintings?|pictures?|images?|photos?))?'
             r'|the\s+(?:artworks?|paintings?|pictures?|images?|photos?)')
_DESCRIPTION_QUESTION = re.compile(
    r'^\s*(?:(?:please|okay|ok)\s+)?(?:'
    r'(?:how|what)\s+(?:does|do)\s+(?:' + _REFERENT + r')\s+look(?:\s+like)?'
    r'|(?:what(?:[\'’]s|\s+is))\s+(?:in|on)\s+(?:' + _REFERENT + r')'
    r'|(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?)?describe\s+(?:' + _REFERENT + r')'
    r'|tell\s+me\s+(?:more\s+)?about\s+(?:' + _REFERENT + r')'
    r')(?:\s+(?:please|again|in\s+detail))?\s*[?.!,…]*\s*$', re.I)


def image_reference_question(text):
    """Recognize a description of the current referent, never a newly named work."""
    if not isinstance(text, str) or not 0 < len(text) <= 4000:
        return False
    return bool(_DESCRIPTION_QUESTION.fullmatch(_FILLERS.sub('', text, count=1).strip()))


_CONFIRMATIONS = frozenset((
    'yes', 'yes please', 'yes do', 'yes please do', 'yes go ahead', 'yes please go ahead',
    'go ahead', 'please do', 'sure', 'sure please', 'sure go ahead', 'sure please do',
    'okay', 'ok', 'okay please', 'ok please', 'absolutely', 'certainly', 'definitely',
    'that would be great', 'yes that would be great', 'sounds good', 'yes sounds good',
    "i'd like that", 'i would like that', 'yes i would', 'yes show me', 'yes show them',
))


def image_confirmation(text):
    """Only a confirmation phrase; the caller must already hold a valid offer."""
    if not isinstance(text, str) or not 0 < len(text) <= 300:
        return False
    text = _FILLERS.sub('', text, count=1).strip()
    if any(not (char.isalpha() or char.isspace() or char in ".,!?;:…'’—–-") for char in text):
        return False
    phrase = ' '.join(part.group().casefold().replace('’', "'") for part in _TOKEN.finditer(text))
    return phrase in _CONFIRMATIONS


_OFFER = re.compile(
    r'\b(?:(?:i|we)\s+(?:can|could|would\s+be\s+happy\s+to)\s+(?:show|display|provide|offer)'
    r'|(?:would|do)\s+you\s+(?:like|want)'
    r'|(?:shall|should|may|can)\s+(?:i|we)\s+(?:show|display|provide|offer))\b', re.I)
_IMAGE_ACTION = re.compile(
    r'\b(?:show|display|open|view|see|provide|offer)\s+(?:(?:you|me|us)\s+)?'
    r'(?:(?:an?|the|some|both|all|these|those|available|source|two|three)\s+)*'
    r'(?:images?|pictures?|photos?|photographs?|artworks?|paintings?|them|these|those|it)\b'
    r'|\b(?:like|want)\s+(?:(?:an?|the|some|available|source)\s+)*'
    r'(?:images?|pictures?|photos?|photographs?|artworks?|paintings?)\b', re.I)
_OFFER_WORDS = frozenset('''i we you your me us can could would will shall should may do be happy am are
    able show showing display displaying provide offer open see view image images picture pictures photo
    photos photograph photographs artwork artworks painting paintings work works a an the some both all
    them these those it this that one ones two three of and or from our available supplied retrieved source
    record records start starting begin first with please now here in chat directly separately original full
    linked links more details which interested choose prefer perhaps then just as well about called titled
    named is has have for if like want to by at look explore discuss talk let us shall okay ok yes catalogue collection
    you'd i'd we'd'''.split())
_GENERIC_TARGET_WORDS = frozenset('''both all them these those it this that the a an one ones two three
    of and or by work works artwork artworks painting paintings image images picture pictures photo photos
    photograph photographs record records source sources original'''.split())


def _offer_text(text):
    text = re.sub(r"['’]s\b", '', text.casefold())
    return ' '.join(part.group().replace('’', "'") for part in _TOKEN.finditer(text))


def _contains_phrase(text, phrase):
    return bool(phrase and ' ' + phrase + ' ' in ' ' + text + ' ')


def image_offer(text, matches):
    """Return known record (collection,id) keys offered by published actor text.

    This is intentionally conservative. An offer naming a supplied title can
    select it. A wholly generic plural offer can select the current candidates.
    Unrecognized names/URLs cannot arm an unrelated image for a later "yes".
    A known record without an image URL can still be selected so the interface
    can explain its actual limitation, rather than inventing an image or name.
    """
    if not isinstance(text, str) or not 0 < len(text) <= 4000 or not isinstance(matches, (list, tuple)):
        return None
    candidates = [value for value in matches[:3] if isinstance(value, dict)
                  and isinstance(value.get('collection_id'), str) and isinstance(value.get('id'), str)]
    if not candidates:
        return None
    quotes = tuple((part.start(), part.end()) for part in _QUOTED.finditer(text))
    offer = next((part for part in _OFFER.finditer(text)
                  if not any(start <= part.start() < end for start, end in quotes)), None)
    if offer is None:
        return None
    segment = text[offer.start():]
    if (not _IMAGE_WORD.search(segment) or not _IMAGE_ACTION.search(segment)
            or re.search(r'https?://|www\.', segment, re.I)):
        return None
    normalized = _offer_text(segment)
    titles = [(value, _offer_text(value.get('title', ''))) for value in candidates
              if isinstance(value.get('title'), str)]
    named = [value for value, title in titles if _contains_phrase(normalized, title)]
    known_phrases = [title for _, title in titles if title]
    for value in candidates:
        artist = value.get('metadata', {}).get('artist') if isinstance(value.get('metadata'), dict) else None
        if isinstance(artist, str):
            # Museum artist displays often append nationality/life dates and role.
            known_phrases.append(_offer_text(re.split(r'[;(·]|\s*\(', artist, maxsplit=1)[0]))
    for quoted in _QUOTED.finditer(segment):
        if _offer_text(quoted.group()) not in known_phrases:
            return None
    # Remove only exact, supplied names before checking that the remaining
    # offer is generic interface language. This rejects invented named targets.
    masked = ' ' + normalized + ' '
    for phrase in sorted((value for value in known_phrases if value), key=len, reverse=True):
        masked = masked.replace(' ' + phrase + ' ', ' ')
    for target in re.finditer(r'\b(?:images?|pictures?|photos?|photographs?|artworks?|paintings?)\s+of\b(.*)', masked):
        tail = re.split(r'\b(?:would|do|shall|should|may|can)\s+(?:you|i|we)\b', target[1], maxsplit=1)[0]
        if set(tail.split()) - _GENERIC_TARGET_WORDS:
            return None
    if set(masked.split()) - _OFFER_WORDS:
        return None
    if named:
        selected = named
    else:
        words = set(normalized.split())
        if len(candidates) > 1:
            if words & {'one', 'first', 'start', 'starting', 'choose', 'prefer'}:
                return None
            if not (words & {'images', 'pictures', 'photos', 'photographs', 'artworks', 'paintings', 'both', 'all', 'them', 'these', 'those'}):
                return None
        if ('both' in words or 'two' in words) and len(candidates) != 2:
            return None
        if 'three' in words and len(candidates) != 3:
            return None
        selected = candidates
    return tuple((value['collection_id'], value['id']) for value in selected)
