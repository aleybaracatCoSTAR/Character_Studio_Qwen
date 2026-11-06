"""Rijksmuseum's keyless Search/Linked Art APIs, with explicit image rights."""
import re
from urllib.parse import parse_qs, urlsplit

from .museum_common import MuseumImportError, museum_record, validate_import_input


PROVIDER = 'rijksmuseum'
SEARCH_URL = 'https://data.rijksmuseum.nl/search/collection'
PAGE_SIZE = 100
_PID = re.compile(r'^/([0-9]{1,30})$')
_IMAGE = re.compile(r'^/([A-Za-z0-9_-]{3,128})(?:/full/(?:max|full|![0-9]+,[0-9]+|[0-9]+,)/0/default\.(?:jpg|jpeg|png))?$')
_ENGLISH = 'http://vocab.getty.edu/aat/300388277'
_PUBLIC_DOMAIN = frozenset(scheme + '://creativecommons.org/publicdomain/' + kind + '/1.0/'
                           for scheme in ('http', 'https') for kind in ('zero', 'mark'))


def _classes(value):
    result = set()
    for item in _items(value.get('classified_as')):
        if not isinstance(item, dict) or item.get('id') is None: continue
        if not isinstance(item['id'], str): raise MuseumImportError(PROVIDER, 'invalid_response')
        result.add(item['id'])
    return result


def _items(value): return value if isinstance(value, list) else []


def _content(values, classification=None):
    candidates = [value for value in _items(values) if isinstance(value, dict)
                  and isinstance(value.get('content'), str)
                  and (classification is None or classification in _classes(value))]
    candidates.sort(key=lambda value:not any(item.get('id') == _ENGLISH
        for item in _items(value.get('language')) if isinstance(item, dict)))
    return candidates[0]['content'] if candidates else ''


def _name(value):
    if not isinstance(value, dict): return ''
    notations = [item for item in _items(value.get('notation')) if isinstance(item, dict)
                 and isinstance(item.get('@value'), str)]
    notations.sort(key=lambda item:item.get('@language') != 'en')
    name = (notations[0]['@value'] if notations else
            _content(value.get('identified_by')) or value.get('_label', ''))
    return name if isinstance(name, str) else ''


def _pid(value):
    if not isinstance(value, str): raise MuseumImportError(PROVIDER, 'invalid_link')
    parts = urlsplit(value)
    match = _PID.fullmatch(parts.path)
    if (parts.scheme != 'https' or parts.netloc != 'id.rijksmuseum.nl' or not match
            or parts.query or parts.fragment): raise MuseumImportError(PROVIDER, 'invalid_link')
    return match.group(1)


def _resolve(fetch_json, identifier, expected_type):
    data = fetch_json('https://data.rijksmuseum.nl/' + identifier, params={'_profile':'la-framed'})
    if (not isinstance(data, dict) or data.get('id') != 'https://id.rijksmuseum.nl/' + identifier
            or data.get('type') != expected_type): raise MuseumImportError(PROVIDER, 'invalid_response')
    return data


def _rights(image):
    return {identifier for right in _items(image.get('subject_to')) if isinstance(right, dict)
            for identifier in _classes(right) if isinstance(identifier, str)}


def _image(fetch_json, record):
    visual = next((value for value in _items(record.get('shows')) if isinstance(value, dict)), None)
    if visual is None: return None, ''
    visual = _resolve(fetch_json, _pid(visual.get('id')), 'VisualItem')
    rights = _rights(visual)
    allowed = rights & _PUBLIC_DOMAIN
    restricted = any('creativecommons.org/licenses/' in value for value in rights)
    if not allowed or restricted: return None, 'No image included: no unambiguous public-domain image rights.'
    digital = next((value for value in _items(visual.get('digitally_shown_by')) if isinstance(value, dict)), None)
    if digital is None: return None, ''
    digital = _resolve(fetch_json, _pid(digital.get('id')), 'DigitalObject')
    for access in _items(digital.get('access_point')):
        if not isinstance(access, dict): continue
        link = access.get('id')
        if not isinstance(link, str): raise MuseumImportError(PROVIDER, 'invalid_link')
        parts = urlsplit(link)
        match = _IMAGE.fullmatch(parts.path)
        if (parts.scheme != 'https' or parts.netloc != 'iiif.micr.io' or not match
                or parts.query or parts.fragment): raise MuseumImportError(PROVIDER, 'invalid_link')
        return ('https://iiif.micr.io/' + match.group(1) + '/full/!600,600/0/default.jpg',
                'Image: ' + sorted(allowed)[0])
    return None, ''


def _record(fetch_json, identifier):
    data = _resolve(fetch_json, identifier, 'HumanMadeObject')
    names = [value for value in _items(data.get('identified_by')) if isinstance(value, dict) and value.get('type') == 'Name']
    preferred = [value for value in names if 'http://vocab.getty.edu/aat/300404670' in _classes(value)]
    title = _content(preferred or names)
    production = data.get('produced_by') if isinstance(data.get('produced_by'), dict) else {}
    makers = []
    for part in [production, *_items(production.get('part'))]:
        if not isinstance(part, dict): continue
        for maker in _items(part.get('carried_out_by')):
            name = _name(maker)
            if isinstance(name, str) and name and name not in makers: makers.append(name)
    timespan = production.get('timespan') if isinstance(production.get('timespan'), dict) else {}
    metadata = {
        'artist':'; '.join(makers), 'date':_content(timespan.get('identified_by')),
        'medium':_content(data.get('referred_to_by'), 'http://vocab.getty.edu/aat/300435429'),
        'dimensions':_content(data.get('referred_to_by'), 'http://vocab.getty.edu/aat/300435430'),
        'credit_line':_content(data.get('referred_to_by'), 'http://vocab.getty.edu/aat/300026687'),
        'accession_number':_content(data.get('identified_by'), 'http://vocab.getty.edu/aat/300312355'),
        'classification':'; '.join(name for value in _items(data.get('classified_as')) if (name := _name(value)))
    }
    image_url, rights = _image(fetch_json, data)
    if rights: metadata['rights'] = rights
    return museum_record(PROVIDER, identifier, title, 'https://id.rijksmuseum.nl/' + identifier,
                         image_url=image_url, image_alt=title, metadata=metadata)


def _next_token(value, field, query):
    if value is None: return None
    if not isinstance(value, dict) or not isinstance(value.get('id'), str):
        raise MuseumImportError(PROVIDER, 'invalid_response')
    parts = urlsplit(value['id'])
    if (parts.scheme != 'https' or parts.netloc != 'data.rijksmuseum.nl'
            or parts.path != '/search/collection' or parts.fragment):
        raise MuseumImportError(PROVIDER, 'invalid_link')
    parameters = parse_qs(parts.query, keep_blank_values=True)
    token = parameters.get('pageToken', [])
    if (set(parameters) != {field, 'pageToken'} or parameters.get(field) != [query]
            or len(token) != 1 or not re.fullmatch(r'[A-Za-z0-9_-]{1,4096}={0,2}', token[0])):
        raise MuseumImportError(PROVIDER, 'invalid_response')
    return token[0]


def _iter_records(fetch_json, query, limit):
    query, limit = validate_import_input(PROVIDER, query, limit)
    seen = set()
    produced = 0
    # Search has field-specific parameters, not a generic q parameter. Separate
    # creator/title streams provide a bounded union rather than accidental AND.
    for field in ('creator', 'title'):
        token, tokens = None, set()
        for _ in range((limit + PAGE_SIZE - 1) // PAGE_SIZE):
            parameters = {field:query}
            if token is not None: parameters['pageToken'] = token
            page = fetch_json(SEARCH_URL, params=parameters)
            if not isinstance(page, dict) or not isinstance(page.get('orderedItems'), list):
                raise MuseumImportError(PROVIDER, 'invalid_response')
            items = page['orderedItems']
            if len(items) > PAGE_SIZE: raise MuseumImportError(PROVIDER, 'invalid_response')
            for value in items:
                if not isinstance(value, dict) or value.get('type') != 'HumanMadeObject':
                    raise MuseumImportError(PROVIDER, 'invalid_response')
                identifier = _pid(value.get('id'))
                if identifier in seen: continue
                seen.add(identifier)
                yield _record(fetch_json, identifier)
                produced += 1
                if produced >= limit: return
            token = _next_token(page.get('next'), field, query)
            if not token or not items: break
            if token in tokens: raise MuseumImportError(PROVIDER, 'invalid_response')
            tokens.add(token)


def iter_records(fetch_json, query, limit):
    try:
        yield from _iter_records(fetch_json, query, limit)
    except MuseumImportError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError):
        raise MuseumImportError(PROVIDER, 'invalid_response') from None
