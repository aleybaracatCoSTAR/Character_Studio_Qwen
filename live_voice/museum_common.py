"""Shared catalogue normalization; museum material is data, never instructions."""
from __future__ import annotations

from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
import re
import uuid

from .collections import CollectionEntry, public_https_url


GUIDE_URL = 'https://www.navigating.art/education/open-access-2026'
MAX_MUSEUM_RECORDS = 1000


@dataclass(frozen=True)
class MuseumSource:
    id: str
    label: str
    collection_id: str
    source_url: str
    docs_url: str

    def public(self):
        return {'id':self.id, 'label':self.label, 'collection_id':self.collection_id,
                'source_url':self.source_url, 'docs_url':self.docs_url}


MUSEUM_SOURCES = (
    MuseumSource('met', 'The Metropolitan Museum of Art', 'metropolitan-museum-of-art',
                 'https://www.metmuseum.org/', 'https://metmuseum.github.io/'),
    MuseumSource('aic', 'Art Institute of Chicago', 'art-institute-of-chicago',
                 'https://www.artic.edu/', 'https://api.artic.edu/docs/'),
    MuseumSource('cleveland', 'Cleveland Museum of Art', 'cleveland-museum-of-art',
                 'https://www.clevelandart.org/', 'https://openaccess-api.clevelandart.org/'),
    MuseumSource('rijksmuseum', 'Rijksmuseum', 'rijksmuseum',
                 'https://www.rijksmuseum.nl/', 'https://data.rijksmuseum.nl/docs/'),
    MuseumSource('getty', 'J. Paul Getty Museum', 'getty-museum',
                 'https://www.getty.edu/', 'https://data.getty.edu/museum/collection/'),
)
SOURCES = {source.id:source for source in MUSEUM_SOURCES}


class MuseumImportError(ValueError):
    """A source/reason pair that does not expose upstream response or record text."""
    def __init__(self, provider, reason):
        self.provider = provider if provider in SOURCES else 'museum'
        self.reason = reason if isinstance(reason,str) and re.fullmatch(r'[a-z][a-z0-9_]{0,47}',reason) else 'unavailable'
        super().__init__('The museum catalogue request could not be completed.')


class _Plain(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script','style'): self.hidden += 1
        elif not self.hidden and tag in ('br','p','div','li'): self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in ('script','style') and self.hidden: self.hidden -= 1
        elif not self.hidden and tag in ('p','div','li'): self.parts.append(' ')

    def handle_data(self, data):
        if not self.hidden: self.parts.append(data)


def plain_text(value, limit):
    if value is None: return ''
    if not isinstance(value,(str,int,float)) or isinstance(value,bool): return ''
    value = str(value)
    if len(value)>100_000: value = value[:100_000]
    parser = _Plain()
    parser.feed(value); parser.close()
    text = unescape(''.join(parser.parts))
    text = re.sub(r'[\x00-\x1f\x7f]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text if len(text)<=limit else text[:max(0,limit-14)].rstrip()+' […] [excerpt]'


def validate_import_input(provider, query, limit):
    if (provider not in SOURCES or not isinstance(query,str) or not 1<=len(query.strip())<=200
            or any(ord(char)<32 or ord(char)==127 for char in query)
            or type(limit) is not int or not 1<=limit<=MAX_MUSEUM_RECORDS):
        raise MuseumImportError(provider,'invalid_input')
    return query.strip(), limit


def museum_record(provider, object_id, title, source_url, image_url=None, image_alt='', metadata=None, description=''):
    """Create stable, bounded records while retaining source and per-record rights."""
    if provider not in SOURCES: raise MuseumImportError(provider,'invalid_input')
    identity = str(object_id)
    if (not 1<=len(identity)<=120 or any(ord(char)<32 or ord(char)==127 for char in identity)):
        raise MuseumImportError(provider,'invalid_response')
    title = plain_text(title,240) or 'Untitled artwork · '+identity[:80]
    caps = {'artist':650,'date':160,'medium':650,'dimensions':450,'credit_line':450,
            'accession_number':120,'classification':250,'rights':900}
    values = {key:plain_text(value,caps[key]) for key,value in (metadata or {}).items() if key in caps}
    values = {key:value for key,value in values.items() if value}
    lines = [title, *[key.replace('_',' ').capitalize()+': '+value for key,value in values.items()]]
    detail = plain_text(description,6500)
    if detail: lines.append(detail)
    try:
        # Image URLs remain display-only. The transport never follows them.
        source_url = public_https_url(source_url)
        image_url = public_https_url(image_url) if image_url else None
        entry = CollectionEntry(id=uuid.uuid5(uuid.NAMESPACE_URL,provider+':'+identity).hex,
            collection_id=SOURCES[provider].collection_id, title=title,
            text=plain_text('\n'.join(lines),12000), source_url=source_url,
            source_entry_id=identity, image_url=image_url,
            image_alt=plain_text(image_alt or title,500), metadata=values)
    except ValueError:
        raise MuseumImportError(provider,'invalid_response') from None
    return entry.model_dump(exclude={'collection_id'})
