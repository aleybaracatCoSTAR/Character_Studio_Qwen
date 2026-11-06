"""Read-only bounded Cleveland Museum of Art Collection API adapter."""
from __future__ import annotations

from urllib.parse import urlsplit

from .museum_common import MuseumImportError, museum_record, validate_import_input


PROVIDER = 'cleveland'
SEARCH_URL = 'https://openaccess-api.clevelandart.org/api/artworks/'
PAGE_SIZE = 25
# Only documented, currently verified fields. An unknown field can cause an
# upstream 500 rather than being ignored; in particular there is no `license`.
FIELDS = ','.join((
    'id', 'title', 'url', 'share_license_status', 'images', 'creators',
    'creation_date', 'technique', 'measurements', 'creditline', 'accession_number',
    'type', 'description', 'did_you_know', 'culture', 'department', 'collection',
    'tombstone', 'copyright',
))


def _integer(value):
    return type(value) is int and value >= 0


def _text(record, name):
    value = record.get(name)
    return value if isinstance(value, str) else ''


def _official_url(value, *, image=False):
    if not isinstance(value, str) or not value or len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value)
        hosts = ('openaccess-cdn.clevelandart.org',) if image else ('clevelandart.org', 'www.clevelandart.org')
        if (parsed.scheme == 'https' and parsed.netloc in hosts and not parsed.fragment
                and not parsed.query and (image or parsed.path.startswith('/art/'))):
            return value
    except ValueError:
        pass
    return None


def _record(data):
    if not isinstance(data, dict) or not _integer(data.get('id')):
        raise MuseumImportError(PROVIDER, 'invalid_response')
    source = _official_url(data.get('url'))
    if source is None:
        raise MuseumImportError(PROVIDER, 'invalid_response')
    # All metadata is CC0, including records for copyrighted artworks. Only
    # exact CC0 share status permits linking an Open Access image.
    eligible = data.get('share_license_status') == 'CC0'
    image = None
    images = data.get('images')
    if eligible and isinstance(images, dict):
        web = images.get('web')
        if isinstance(web, dict):
            image = _official_url(web.get('url'), image=True)
    rights = 'Collection metadata: CC0. '
    rights += 'Image: CC0 under the Cleveland Open Access policy.' if eligible else 'No CC0 image selected.'
    notice = _text(data, 'copyright')
    if notice:
        rights += ' Source rights notice: ' + notice
    artists = []
    creators = data.get('creators')
    if isinstance(creators, list):
        for creator in creators[:100]:
            if isinstance(creator, dict):
                name = _text(creator, 'description')
                if name:
                    role = _text(creator, 'role')
                    qualifier = _text(creator, 'qualifier')
                    artists.append(' · '.join(value for value in (name, role, qualifier) if value))
    details = []
    for key, label in (('description', 'Description'), ('did_you_know', 'Did you know'),
                       ('department', 'Department'), ('collection', 'Collection'),
                       ('tombstone', 'Museum caption')):
        value = _text(data, key)
        if value:
            details.append(label + ': ' + value)
    culture = data.get('culture')
    if isinstance(culture, list):
        values = [item for item in culture[:100] if isinstance(item, str)]
        if values:
            details.append('Culture: ' + '; '.join(values))
    return museum_record(PROVIDER, data['id'], _text(data, 'title'), source,
        image_url=image, image_alt=_text(data, 'title'),
        metadata={
            'artist': '; '.join(artists), 'date': _text(data, 'creation_date'),
            'medium': _text(data, 'technique'), 'dimensions': _text(data, 'measurements'),
            'credit_line': _text(data, 'creditline'), 'accession_number': _text(data, 'accession_number'),
            'classification': _text(data, 'type'), 'rights': rights,
        }, description='\n'.join(details))


def iter_records(fetch_json, query, limit):
    """Yield bounded keyword/artist search results using skip/limit pagination."""
    query, limit = validate_import_input(PROVIDER, query, limit)
    offset = 0
    seen = set()
    while offset < limit:
        count = min(PAGE_SIZE, limit - offset)
        page = fetch_json(SEARCH_URL, params={'q': query, 'skip': offset, 'limit': count, 'fields': FIELDS})
        if not isinstance(page, dict) or not isinstance(page.get('info'), dict):
            raise MuseumImportError(PROVIDER, 'invalid_response')
        total = page['info'].get('total')
        rows = page.get('data')
        if not _integer(total) or not isinstance(rows, list) or len(rows) > count:
            raise MuseumImportError(PROVIDER, 'invalid_response')
        if not rows:
            if offset < total:
                raise MuseumImportError(PROVIDER, 'invalid_response')
            break
        for row in rows:
            if not isinstance(row, dict) or not _integer(row.get('id')) or row['id'] in seen:
                raise MuseumImportError(PROVIDER, 'invalid_response')
            seen.add(row['id'])
            yield _record(row)
        offset += len(rows)
        if offset >= total:
            break
