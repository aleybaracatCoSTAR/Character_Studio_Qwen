"""Bounded Met Collection API searches using the current paginated endpoint."""
from __future__ import annotations

from urllib.parse import urlsplit

from .museum_common import MuseumImportError, museum_record, validate_import_input


PROVIDER = 'met'
SEARCH_URL = 'https://collectionapi.metmuseum.org/public/collection/v1.1/search'
OBJECT_URL = 'https://collectionapi.metmuseum.org/public/collection/v1/objects/'
PAGE_SIZE = 100


def _integer(value):
    return type(value) is int and value >= 0


def _text(record, name):
    value = record.get(name)
    return value if isinstance(value, str) else ''


def _image(value):
    if not isinstance(value, str) or not value or len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme == 'https' and parsed.netloc == 'images.metmuseum.org'
                and parsed.path.startswith('/CRDImages/') and not parsed.fragment):
            return value
    except ValueError:
        pass
    return None


def _record(data, identity):
    if not isinstance(data, dict) or not _integer(data.get('objectID')) or data['objectID'] != identity:
        raise MuseumImportError(PROVIDER, 'invalid_response')
    public = data.get('isPublicDomain') is True
    image = _image(data.get('primaryImageSmall')) or _image(data.get('primaryImage')) if public else None
    rights = 'Collection metadata: CC0. '
    rights += 'Image: public domain under the Met Open Access policy.' if public else 'No public-domain image selected.'
    restriction = _text(data, 'rightsAndReproduction')
    if restriction:
        rights += ' Source rights notice: ' + restriction
    details = []
    for key, label in (
        ('objectName', 'Object type'), ('department', 'Department'),
        ('culture', 'Culture'), ('period', 'Period'), ('dynasty', 'Dynasty'),
        ('reign', 'Reign'), ('portfolio', 'Portfolio'), ('artistDisplayBio', 'Artist biography'),
        ('artistPrefix', 'Attribution qualifier'), ('artistSuffix', 'Artist participation'),
    ):
        value = _text(data, key)
        if value:
            details.append(label + ': ' + value)
    tags = data.get('tags')
    if isinstance(tags, list):
        terms = [item['term'] for item in tags[:100]
                 if isinstance(item, dict) and isinstance(item.get('term'), str)]
        if terms:
            details.append('Subject tags: ' + '; '.join(terms))
    return museum_record(PROVIDER, identity, _text(data, 'title'),
        'https://www.metmuseum.org/art/collection/search/' + str(identity),
        image_url=image, image_alt=_text(data, 'title'),
        metadata={
            'artist': _text(data, 'artistDisplayName'), 'date': _text(data, 'objectDate'),
            'medium': _text(data, 'medium'), 'dimensions': _text(data, 'dimensions'),
            'credit_line': _text(data, 'creditLine'), 'accession_number': _text(data, 'accessionNumber'),
            'classification': _text(data, 'classification'), 'rights': rights,
        }, description='\n'.join(details))


def iter_records(fetch_json, query, limit):
    """Yield at most ``limit`` catalogue records; never enumerate the full API.

    Search v1.1 is capped by the Met at offset+limit=10,000. Our tighter
    1,000-record operation limit also bounds the number of detail requests.
    ``hasImages`` is deliberately absent: metadata without an eligible image
    remains useful and that search filter does not guarantee an Open Access image.
    """
    query, limit = validate_import_input(PROVIDER, query, limit)
    offset = 0
    seen = set()
    while offset < limit:
        count = min(PAGE_SIZE, limit - offset)
        page = fetch_json(SEARCH_URL, params={'q': query, 'offset': offset, 'limit': count})
        if not isinstance(page, dict) or not _integer(page.get('total')):
            raise MuseumImportError(PROVIDER, 'invalid_response')
        identities = page.get('objectIDs')
        if identities is None and page['total'] == 0:
            identities = []
        if not isinstance(identities, list) or len(identities) > count:
            raise MuseumImportError(PROVIDER, 'invalid_response')
        if not identities:
            if offset < page['total']:
                raise MuseumImportError(PROVIDER, 'invalid_response')
            break
        for identity in identities:
            if not _integer(identity) or identity in seen:
                raise MuseumImportError(PROVIDER, 'invalid_response')
            seen.add(identity)
            yield _record(fetch_json(OBJECT_URL + str(identity)), identity)
        offset += len(identities)
        if offset >= page['total']:
            break
