"""Getty's public SPARQL/Linked Art APIs; image CC0 is checked separately."""
import json
import re
from urllib.parse import urlsplit

from .museum_common import MuseumImportError, museum_record, validate_import_input


PROVIDER = 'getty'
SEARCH_URL = 'https://data.getty.edu/museum/collection/sparql'
PAGE_SIZE = 25
_UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
_OBJECT = re.compile(r'^/museum/collection/object/(' + _UUID + r')$')
_MEDIA = re.compile(r'^/media/image/(' + _UUID + r')$')
_IIIF = re.compile(r'^/iiif/image/(' + _UUID + r')(?:/full/(?:max|full|![0-9]+,[0-9]+|[0-9]+,)/0/default\.(?:jpg|jpeg|png))?$')
_HOMEPAGE = re.compile(r'^/art/collection/object/[A-Za-z0-9-]{1,160}/?$')
_CC0 = frozenset(scheme + '://creativecommons.org/publicdomain/zero/1.0/' for scheme in ('http', 'https'))


def _items(value): return value if isinstance(value, list) else []


def _classes(value):
    result = set()
    for item in _items(value.get('classified_as')):
        if not isinstance(item, dict) or item.get('id') is None: continue
        if not isinstance(item['id'], str): raise MuseumImportError(PROVIDER, 'invalid_response')
        result.add(item['id'])
    return result


def _content(values, classification=None):
    candidates = [value for value in _items(values) if isinstance(value, dict)
                  and isinstance(value.get('content'), str)
                  and (classification is None or classification in _classes(value))]
    candidates.sort(key=lambda value:not any(item.get('id') == 'http://vocab.getty.edu/aat/300388277'
        for item in _items(value.get('language')) if isinstance(item, dict)))
    return candidates[0]['content'] if candidates else ''


def _identifier(value, expression):
    if not isinstance(value, str): raise MuseumImportError(PROVIDER, 'invalid_link')
    parts = urlsplit(value)
    match = expression.fullmatch(parts.path)
    if (parts.scheme != 'https' or parts.netloc != 'data.getty.edu' or not match
            or parts.query or parts.fragment): raise MuseumImportError(PROVIDER, 'invalid_link')
    return match.group(1)


def _image(fetch_json, record):
    # `shows` also includes the artwork's conceptual /subjects VisualItem.
    # Only image-classified references lead to Getty's separate media records.
    image = next((item for item in _items(record.get('shows')) if isinstance(item, dict)
                  and 'http://vocab.getty.edu/aat/300215302' in _classes(item)), None)
    if image is None: return None, ''
    identifier = _identifier(image.get('id'), _MEDIA)
    image = fetch_json('https://data.getty.edu/media/image/' + identifier)
    if (not isinstance(image, dict) or image.get('type') != 'VisualItem'
            or image.get('id') != 'https://data.getty.edu/media/image/' + identifier):
        raise MuseumImportError(PROVIDER, 'invalid_response')
    rights = {value for right in _items(image.get('subject_to')) if isinstance(right, dict)
              for value in _classes(right) if isinstance(value, str)}
    restricted = any('creativecommons.org/licenses/' in value for value in rights)
    if not rights & _CC0 or restricted:
        return None, 'Metadata: Getty CC0; image omitted because explicit CC0 image rights were not supplied.'
    for digital in _items(image.get('digitally_shown_by')):
        if not isinstance(digital, dict): continue
        for access in _items(digital.get('access_point')):
            if not isinstance(access, dict): continue
            link = access.get('id')
            if not isinstance(link, str): raise MuseumImportError(PROVIDER, 'invalid_link')
            parts = urlsplit(link)
            match = _IIIF.fullmatch(parts.path)
            if (parts.scheme != 'https' or parts.netloc != 'media.getty.edu' or not match
                    or parts.query or parts.fragment): raise MuseumImportError(PROVIDER, 'invalid_link')
            return ('https://media.getty.edu/iiif/image/' + match.group(1) + '/full/!600,600/0/default.jpg',
                    'Metadata: Getty CC0. Image: https://creativecommons.org/publicdomain/zero/1.0/')
    return None, 'Metadata: Getty CC0; no image access point was supplied.'


def _record(fetch_json, identifier):
    url = 'https://data.getty.edu/museum/collection/object/' + identifier
    data = fetch_json(url)
    if (not isinstance(data, dict) or data.get('id') != url or data.get('type') != 'HumanMadeObject'):
        raise MuseumImportError(PROVIDER, 'invalid_response')
    names = [item for item in _items(data.get('identified_by')) if isinstance(item, dict) and item.get('type') == 'Name']
    preferred = [item for item in names if 'http://vocab.getty.edu/aat/300404670' in _classes(item)]
    title = _content(preferred or names) or data.get('_label', '')
    production = data.get('produced_by') if isinstance(data.get('produced_by'), dict) else {}
    makers = []
    for part in [production, *_items(production.get('part'))]:
        if not isinstance(part, dict): continue
        for maker in _items(part.get('carried_out_by')):
            name = maker.get('_label') if isinstance(maker, dict) else None
            if isinstance(name, str) and name and name not in makers: makers.append(name)
    timespan = production.get('timespan') if isinstance(production.get('timespan'), dict) else {}
    metadata = {
        'artist':'; '.join(makers), 'date':_content(timespan.get('identified_by')),
        'medium':_content(data.get('referred_to_by'), 'http://vocab.getty.edu/aat/300435429'),
        'dimensions':_content(data.get('referred_to_by'), 'http://vocab.getty.edu/aat/300435430'),
        'credit_line':_content(data.get('referred_to_by'), 'http://vocab.getty.edu/aat/300026687'),
        'accession_number':_content(data.get('identified_by'), 'http://vocab.getty.edu/aat/300312355'),
        'classification':'; '.join(item['_label'] for item in _items(data.get('classified_as'))
                                   if isinstance(item, dict) and isinstance(item.get('_label'), str)),
        'rights':'Metadata: https://creativecommons.org/publicdomain/zero/1.0/'
    }
    source = url
    for value in _items(data.get('subject_of')):
        if not isinstance(value, dict) or value.get('format') != 'text/html': continue
        link = value.get('id')
        if not isinstance(link, str): raise MuseumImportError(PROVIDER, 'invalid_link')
        parts = urlsplit(link)
        if (parts.scheme != 'https' or parts.netloc != 'www.getty.edu' or not _HOMEPAGE.fullmatch(parts.path)
                or parts.query or parts.fragment): raise MuseumImportError(PROVIDER, 'invalid_link')
        source = link
        break
    image_url, rights = _image(fetch_json, data)
    if rights: metadata['rights'] = rights
    # Curatorial descriptions may carry third-party/CC BY rights separately.
    # Use factual catalogue fields, without copying those prose descriptions.
    return museum_record(PROVIDER, identifier, title, source, image_url=image_url,
                         image_alt=title if isinstance(title, str) else '', metadata=metadata)


def _query(query, page_size, offset):
    literal = json.dumps(query.casefold(), ensure_ascii=False)
    return ('PREFIX crm: <http://www.cidoc-crm.org/cidoc-crm/> '
            'PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> '
            'SELECT DISTINCT ?object WHERE { ?object a crm:E22_Human-Made_Object . '
            '{ ?object rdfs:label ?term . } UNION '
            '{ ?object crm:P108i_was_produced_by/crm:P14_carried_out_by/rdfs:label ?term . } '
            'FILTER(CONTAINS(LCASE(STR(?term)), ' + literal + ')) } '
            f'ORDER BY ?object LIMIT {page_size} OFFSET {offset}')


def _iter_records(fetch_json, query, limit):
    query, limit = validate_import_input(PROVIDER, query, limit)
    produced, offset, seen = 0, 0, set()
    for _ in range((limit + PAGE_SIZE - 1) // PAGE_SIZE):
        page_size = min(PAGE_SIZE, limit - produced)
        page = fetch_json(SEARCH_URL, params={'query':_query(query, page_size, offset)},
                          headers={'Accept':'application/sparql-results+json'})
        bindings = page.get('results', {}).get('bindings') if isinstance(page, dict) and isinstance(page.get('results'), dict) else None
        if not isinstance(bindings, list) or len(bindings) > page_size:
            raise MuseumImportError(PROVIDER, 'invalid_response')
        for binding in bindings:
            value = binding.get('object') if isinstance(binding, dict) else None
            if not isinstance(value, dict) or value.get('type') != 'uri':
                raise MuseumImportError(PROVIDER, 'invalid_response')
            identifier = _identifier(value.get('value'), _OBJECT)
            if identifier in seen: continue
            seen.add(identifier)
            yield _record(fetch_json, identifier)
            produced += 1
            if produced >= limit: return
        offset += page_size
        if len(bindings) < page_size: return


def iter_records(fetch_json, query, limit):
    try:
        yield from _iter_records(fetch_json, query, limit)
    except MuseumImportError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError):
        raise MuseumImportError(PROVIDER, 'invalid_response') from None
