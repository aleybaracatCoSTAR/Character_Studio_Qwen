"""Resumable full catalogue enumeration for the two Linked Art sources.

The caller owns pacing, cancellation, durable page transactions and checkpoints.
Only published JSON metadata is fetched, including explicit image-rights records;
image pixels are never downloaded. A terminal cursor is emitted only after the
published count and observed enumeration agree. These live APIs are not atomic
snapshots: a changing source is reported rather than labelled complete.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree as ET

from .museum_common import MuseumImportError, museum_record
from . import museum_getty as getty
from . import museum_rijksmuseum as rijks


RIJKS_PAGE_SIZE = 100
GETTY_PAGE_SIZE = 500
MAX_SOURCE_RECORDS = 10_000_000
_TOKEN = re.compile(r'[A-Za-z0-9_-]{1,4096}={0,2}\Z')
_PID = re.compile(r'[0-9]{1,30}\Z')
_UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z')
_GETTY_PREFIX = 'https://data.getty.edu/museum/collection/object/'
_SPARQL_PREFIX = 'PREFIX crm: <http://www.cidoc-crm.org/cidoc-crm/> '
_OAI = 'https://data.rijksmuseum.nl/oai'
_NS = {'oai': 'http://www.openarchives.org/OAI/2.0/', 'rdf': 'http://www.w3.org/1999/02/22-rdf-syntax-ns#',
       'dc': 'http://purl.org/dc/elements/1.1/', 'dct': 'http://purl.org/dc/terms/',
       'edm': 'http://www.europeana.eu/schemas/edm/', 'ore': 'http://www.openarchives.org/ore/terms/',
       'skos': 'http://www.w3.org/2004/02/skos/core#', 'ef': 'http://www.europeanafashion.eu/edmfp/',
       'mrel': 'http://id.loc.gov/vocabulary/relators/'}
_RDF_ABOUT = '{' + _NS['rdf'] + '}about'
_RDF_RESOURCE = '{' + _NS['rdf'] + '}resource'
_LANG = '{http://www.w3.org/XML/1998/namespace}lang'
_OAI_TOKEN = re.compile(r'[A-Za-z0-9_+/=-]{1,4096}\Z')
_UNTIL = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z')


def _count(value):
    return type(value) is int and 0 <= value <= MAX_SOURCE_RECORDS


def _xml_text(element):
    return ' '.join(''.join(element.itertext()).split())


def _preferred(elements):
    elements = list(elements)
    elements.sort(key=lambda value: (value.get(_LANG) != 'en', value.get(_LANG) not in (None, 'en')))
    return next((_xml_text(value) for value in elements if _xml_text(value)), '')


def _edm_record(record):
    identity = rijks._pid(record.findtext('oai:header/oai:identifier', namespaces=_NS))
    uri = 'https://id.rijksmuseum.nl/' + identity
    choices = [value for value in record.findall('.//edm:ProvidedCHO', _NS) if value.get(_RDF_ABOUT) == uri]
    if len(choices) != 1:
        raise MuseumImportError('rijksmuseum', 'invalid_response')
    cho = choices[0]
    labels = {}
    for value in record.iter():
        resource = value.get(_RDF_ABOUT)
        if resource:
            label = _preferred(value.findall('skos:prefLabel', _NS))
            if label: labels[resource] = label
    def values(path):
        result = []
        for value in cho.findall(path, _NS):
            label = _xml_text(value) or labels.get(value.get(_RDF_RESOURCE), '')
            if label and label not in result: result.append(label)
        return '; '.join(result)
    aggregation = next((value for value in record.findall('.//ore:Aggregation', _NS)
        if any(link.get(_RDF_RESOURCE) == uri or any(child.get(_RDF_ABOUT) == uri
               for child in link.findall('edm:ProvidedCHO', _NS))
               for link in value.findall('edm:aggregatedCHO', _NS))), None)
    image_url, rights = None, set()
    if aggregation is not None:
        rights = {value.get(_RDF_RESOURCE) for value in aggregation.findall('edm:rights', _NS)
                  if value.get(_RDF_RESOURCE)}
        links = aggregation.findall('edm:isShownBy', _NS) + aggregation.findall('edm:object', _NS)
        image_links = [value.get(_RDF_RESOURCE) or next((child.get(_RDF_ABOUT)
                       for child in value.findall('edm:WebResource', _NS)), None) for value in links]
        image = next((value for value in image_links if value), None)
        for web in record.findall('.//edm:WebResource', _NS):
            if web.get(_RDF_ABOUT) == image:
                rights |= {value.get(_RDF_RESOURCE) for path in ('dc:rights', 'edm:rights')
                           for value in web.findall(path, _NS) if value.get(_RDF_RESOURCE)}
        if rights and rights <= rijks._PUBLIC_DOMAIN and isinstance(image, str):
            parts = urlsplit(image)
            match = rijks._IMAGE.fullmatch(parts.path)
            if (parts.scheme == 'https' and parts.netloc == 'iiif.micr.io' and match
                    and not parts.query and not parts.fragment):
                image_url = 'https://iiif.micr.io/' + match[1] + '/full/!600,600/0/default.jpg'
    title = _preferred(cho.findall('dc:title', _NS))
    subjects = values('dc:subject')
    metadata = {'artist': values('dc:creator'), 'date': _preferred(cho.findall('dct:created', _NS)) or values('dc:date'),
                'medium': '; '.join(value for value in (values('dct:medium'), values('ef:technique')) if value),
                'dimensions': _preferred(cho.findall('dct:extent', _NS)), 'accession_number': values('dc:identifier'),
                'classification': values('dc:type'),
                'rights': ('Rijksmuseum catalogue metadata. Image: ' + '; '.join(sorted(rights)) if image_url
                           else 'Rijksmuseum catalogue metadata. No eligible public-domain image URL selected.')}
    return museum_record('rijksmuseum', identity, title, uri, image_url=image_url, image_alt=title,
                         metadata=metadata, description='Subject: ' + subjects if subjects else '')


def _rijks_oai_pages(fetch_xml, cursor):
    keys = {'v', 'provider', 'mode', 'resumption_token', 'until', 'scanned', 'deleted', 'processed', 'expected'}
    if cursor is not None:
        if (not isinstance(cursor, dict) or set(cursor) != keys or type(cursor.get('v')) is not int or cursor.get('v') != 1
                or cursor.get('provider') != 'rijksmuseum' or cursor.get('mode') != 'oai-edm'
                or not isinstance(cursor.get('resumption_token'), str) or not _OAI_TOKEN.fullmatch(cursor['resumption_token'])
                or not isinstance(cursor.get('until'), str) or not _UNTIL.fullmatch(cursor['until'])
                or any(not _count(cursor.get(key)) for key in ('scanned', 'deleted', 'processed', 'expected'))
                or cursor['scanned'] != cursor['deleted'] + cursor['processed']
                or not 0 < cursor['scanned'] <= cursor['expected']):
            raise MuseumImportError('rijksmuseum', 'invalid_cursor')
    until = cursor['until'] if cursor else datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    token = cursor['resumption_token'] if cursor else None
    scanned, deleted, processed = ((cursor[key] for key in ('scanned', 'deleted', 'processed')) if cursor else (0, 0, 0))
    expected = cursor['expected'] if cursor else None
    tokens = {token} if token else set()
    while True:
        params = ({'verb': 'ListRecords', 'resumptionToken': token} if token else
                  {'verb': 'ListRecords', 'metadataPrefix': 'edm', 'until': until})
        raw = fetch_xml(_OAI, params=params)
        if (not isinstance(raw, bytes) or not raw or len(raw) > 8 * 1024 * 1024 or b'\x00' in raw
                or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper()):
            raise MuseumImportError('rijksmuseum', 'invalid_response')
        try: page = ET.fromstring(raw)
        except ET.ParseError:
            raise MuseumImportError('rijksmuseum', 'invalid_response') from None
        pending, nodes = [(page, 1)], 0
        while pending:
            element, depth = pending.pop(); nodes += 1
            if depth > 64 or nodes > 250_000:
                raise MuseumImportError('rijksmuseum', 'invalid_response')
            pending.extend((child, depth + 1) for child in element)
        if page.tag != '{' + _NS['oai'] + '}OAI-PMH' or page.find('oai:error', _NS) is not None:
            raise MuseumImportError('rijksmuseum', 'invalid_response')
        batch = page.find('oai:ListRecords', _NS)
        if batch is None: raise MuseumImportError('rijksmuseum', 'invalid_response')
        entries, marker = batch.findall('oai:record', _NS), batch.find('oai:resumptionToken', _NS)
        if len(entries) > 50: raise MuseumImportError('rijksmuseum', 'invalid_response')
        raw_total = marker.get('completeListSize') if marker is not None else None
        if raw_total is not None:
            if not re.fullmatch(r'[0-9]{1,8}', raw_total) or not _count(int(raw_total)):
                raise MuseumImportError('rijksmuseum', 'invalid_response')
            if expected is not None and expected != int(raw_total):
                raise MuseumImportError('rijksmuseum', 'source_changed')
            expected = int(raw_total)
        next_token = (marker.text or '').strip() if marker is not None else ''
        if next_token and (not _OAI_TOKEN.fullmatch(next_token) or next_token in tokens or not entries):
            raise MuseumImportError('rijksmuseum', 'invalid_response')
        records, page_ids = [], set()
        for entry in entries:
            header = entry.find('oai:header', _NS)
            if header is None or header.get('status') not in (None, 'deleted'):
                raise MuseumImportError('rijksmuseum', 'invalid_response')
            identity = rijks._pid(header.findtext('oai:identifier', namespaces=_NS))
            if identity in page_ids: raise MuseumImportError('rijksmuseum', 'invalid_response')
            page_ids.add(identity)
            if header.get('status') == 'deleted': deleted += 1
            else: records.append(_edm_record(entry))
        scanned += len(entries); processed += len(records)
        if expected is not None and scanned > expected:
            raise MuseumImportError('rijksmuseum', 'source_changed')
        if not next_token:
            if expected is None or scanned != expected:
                raise MuseumImportError('rijksmuseum', 'incomplete_catalogue')
            yield records, None, processed
            return
        if expected is None: raise MuseumImportError('rijksmuseum', 'invalid_response')
        tokens.add(next_token)
        state = {'v': 1, 'provider': 'rijksmuseum', 'mode': 'oai-edm', 'resumption_token': next_token,
                 'until': until, 'scanned': scanned, 'deleted': deleted, 'processed': processed, 'expected': expected}
        yield records, state, None
        token = next_token


def _cursor(provider, value):
    if value is None:
        return None
    keys = ({'v', 'provider', 'page_token', 'last_id', 'processed', 'expected'}
            if provider == 'rijksmuseum' else {'v', 'provider', 'after', 'processed', 'expected'})
    if (not isinstance(value, dict) or set(value) != keys or type(value.get('v')) is not int
            or value['v'] != 1 or value.get('provider') != provider
            or not _count(value.get('expected')) or not _count(value.get('processed'))
            or not 0 < value['processed'] <= value['expected']):
        raise MuseumImportError(provider, 'invalid_cursor')
    if provider == 'rijksmuseum':
        if (not isinstance(value['page_token'], str) or not _TOKEN.fullmatch(value['page_token'])
                or not isinstance(value['last_id'], str) or not _PID.fullmatch(value['last_id'])):
            raise MuseumImportError(provider, 'invalid_cursor')
    elif not isinstance(value['after'], str) or not _UUID.fullmatch(value['after']):
        raise MuseumImportError(provider, 'invalid_cursor')
    return dict(value)


def _rijks_next(value):
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get('id'), str):
        raise MuseumImportError('rijksmuseum', 'invalid_response')
    url = urlsplit(value['id'])
    parameters = parse_qs(url.query, keep_blank_values=True)
    tokens = parameters.get('pageToken', [])
    if (url.scheme != 'https' or url.netloc != 'data.rijksmuseum.nl'
            or url.path != '/search/collection' or url.fragment
            or set(parameters) != {'pageToken'} or len(tokens) != 1
            or not _TOKEN.fullmatch(tokens[0])):
        raise MuseumImportError('rijksmuseum', 'invalid_link')
    return tokens[0]


def _rijks_pages(fetch_json, cursor):
    state = _cursor('rijksmuseum', cursor)
    token = state['page_token'] if state else None
    previous = state['last_id'] if state else None
    processed = state['processed'] if state else 0
    expected = state['expected'] if state else None
    tokens = {token} if token else set()
    while True:
        page = fetch_json(rijks.SEARCH_URL, params={'pageToken': token} if token else None)
        part = page.get('partOf') if isinstance(page, dict) else None
        items = page.get('orderedItems') if isinstance(page, dict) else None
        total = part.get('totalItems') if isinstance(part, dict) else None
        if (not isinstance(page, dict) or page.get('type') != 'OrderedCollectionPage'
                or not _count(total) or not isinstance(items, list) or len(items) > RIJKS_PAGE_SIZE):
            raise MuseumImportError('rijksmuseum', 'invalid_response')
        if expected is None:
            expected = total
        elif expected != total:
            raise MuseumImportError('rijksmuseum', 'source_changed')
        identifiers = []
        for item in items:
            if not isinstance(item, dict) or item.get('type') != 'HumanMadeObject':
                raise MuseumImportError('rijksmuseum', 'invalid_response')
            identity = rijks._pid(item.get('id'))
            # The unfiltered API advances by the last identifier URI. Reject a
            # changed/repeated ordering instead of keeping a full-catalogue set.
            if previous is not None and identity <= previous:
                raise MuseumImportError('rijksmuseum', 'invalid_response')
            identifiers.append(identity)
            previous = identity
        next_token = _rijks_next(page.get('next'))
        if next_token and (not identifiers or next_token in tokens):
            raise MuseumImportError('rijksmuseum', 'invalid_response')
        new_processed = processed + len(identifiers)
        if new_processed > expected:
            raise MuseumImportError('rijksmuseum', 'source_changed')
        if not next_token and new_processed != expected:
            raise MuseumImportError('rijksmuseum', 'incomplete_catalogue')
        records = [rijks._record(fetch_json, identity) for identity in identifiers]
        processed = new_processed
        if next_token:
            tokens.add(next_token)
            next_cursor = {'v': 1, 'provider': 'rijksmuseum', 'page_token': next_token,
                           'last_id': previous, 'processed': processed, 'expected': expected}
        else:
            next_cursor = None
        yield records, next_cursor, expected
        if next_cursor is None:
            return
        token = next_token


def _getty_count(fetch_json):
    query = (_SPARQL_PREFIX + 'SELECT (COUNT(DISTINCT ?object) AS ?total) WHERE { '
             '?object a crm:E22_Human-Made_Object . '
             'FILTER(STRSTARTS(STR(?object), "' + _GETTY_PREFIX + '")) }')
    page = fetch_json(getty.SEARCH_URL, params={'query': query},
                      headers={'Accept': 'application/sparql-results+json'})
    result = page.get('results') if isinstance(page, dict) else None
    rows = result.get('bindings') if isinstance(result, dict) else None
    count = rows[0].get('total') if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict) else None
    value = count.get('value') if isinstance(count, dict) and count.get('type') in ('literal', 'typed-literal') else None
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,8}', value) or not _count(int(value)):
        raise MuseumImportError('getty', 'invalid_response')
    return int(value)


def _getty_query(after):
    # `after` is a validated UUID, never arbitrary query text or a remote URL.
    lower_bound = ('FILTER(STR(?object) > "' + _GETTY_PREFIX + after + '") ' if after else '')
    return (_SPARQL_PREFIX + 'PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> '
            'SELECT ?object (SAMPLE(?title0) AS ?title) '
            '(GROUP_CONCAT(DISTINCT ?artist0; separator="; ") AS ?artist) '
            '(SAMPLE(?date0) AS ?date) (SAMPLE(?medium0) AS ?medium) '
            '(SAMPLE(?accession0) AS ?accession) '
            '(GROUP_CONCAT(DISTINCT ?type0; separator="; ") AS ?classification) WHERE { '
            '{ SELECT DISTINCT ?object WHERE { ?object a crm:E22_Human-Made_Object . '
            'FILTER(STRSTARTS(STR(?object), "' + _GETTY_PREFIX + '")) ' + lower_bound +
            '} ORDER BY STR(?object) LIMIT ' + str(GETTY_PAGE_SIZE) + ' } '
            'OPTIONAL { ?object rdfs:label ?title0 } '
            'OPTIONAL { ?object crm:P108i_was_produced_by/crm:P14_carried_out_by/rdfs:label ?artist0 } '
            'OPTIONAL { ?object crm:P108i_was_produced_by/crm:P4_has_time-span/'
            'crm:P1_is_identified_by/crm:P190_has_symbolic_content ?date0 } '
            'OPTIONAL { ?object crm:P67i_is_referred_to_by ?m . '
            '?m crm:P2_has_type <http://vocab.getty.edu/aat/300435429>; crm:P190_has_symbolic_content ?medium0 } '
            'OPTIONAL { ?object crm:P1_is_identified_by ?a . '
            '?a crm:P2_has_type <http://vocab.getty.edu/aat/300312355>; crm:P190_has_symbolic_content ?accession0 } '
            'OPTIONAL { ?object crm:P2_has_type/rdfs:label ?type0 } '
            '} GROUP BY ?object ORDER BY STR(?object)')


def _getty_bulk_record(row, identity):
    def value(name):
        binding = row.get(name)
        if binding is None: return ''
        if (not isinstance(binding, dict) or binding.get('type') not in ('literal', 'typed-literal')
                or not isinstance(binding.get('value'), str)):
            raise MuseumImportError('getty', 'invalid_response')
        return binding['value']
    # Full imports batch factual fields. Getty's separately licensed prose and
    # images are not inferred from this metadata graph. The retrieved-record
    # image resolver can check a few media records later, without N+1 harvesting.
    return museum_record('getty', identity, value('title'), _GETTY_PREFIX + identity,
                         metadata={'artist': value('artist'), 'date': value('date'),
                                   'medium': value('medium'), 'accession_number': value('accession'),
                                   'classification': value('classification'),
                                   'rights': 'Catalogue metadata: Getty CC0. Image availability and rights are checked only when a record is retrieved.'})


def _getty_pages(fetch_json, cursor):
    state = _cursor('getty', cursor)
    current_count = _getty_count(fetch_json)
    expected = state['expected'] if state else current_count
    if current_count != expected:
        raise MuseumImportError('getty', 'source_changed')
    processed = state['processed'] if state else 0
    after = state['after'] if state else None
    while True:
        page = fetch_json(getty.SEARCH_URL, params={'query': _getty_query(after)},
                          headers={'Accept': 'application/sparql-results+json'})
        results = page.get('results') if isinstance(page, dict) else None
        rows = results.get('bindings') if isinstance(results, dict) else None
        if not isinstance(rows, list) or len(rows) > GETTY_PAGE_SIZE:
            raise MuseumImportError('getty', 'invalid_response')
        identifiers, previous = [], after
        for row in rows:
            value = row.get('object') if isinstance(row, dict) else None
            if not isinstance(value, dict) or value.get('type') != 'uri':
                raise MuseumImportError('getty', 'invalid_response')
            identity = getty._identifier(value.get('value'), getty._OBJECT)
            if previous is not None and identity <= previous:
                raise MuseumImportError('getty', 'invalid_response')
            identifiers.append(identity)
            previous = identity
        if not identifiers:
            if processed != expected:
                raise MuseumImportError('getty', 'incomplete_catalogue')
            if _getty_count(fetch_json) != expected:
                raise MuseumImportError('getty', 'source_changed')
            yield [], None, expected
            return
        if processed + len(identifiers) > expected:
            raise MuseumImportError('getty', 'source_changed')
        records = [_getty_bulk_record(row, identity) for row, identity in zip(rows, identifiers)]
        processed += len(identifiers)
        after = identifiers[-1]
        next_cursor = {'v': 1, 'provider': 'getty', 'after': after,
                       'processed': processed, 'expected': expected}
        # Even a short page is not an end marker. Confirm an empty keyset page
        # and the count before the caller can mark this catalogue complete.
        yield records, next_cursor, expected


def iter_pages(provider, fetch_json, cursor=None):
    """Yield ``(records, next_cursor, expected_count)`` page checkpoints.

    Save a cursor only after its records commit. A failed/cancelled page is safe
    to request again. ``next_cursor is None`` is the sole completion signal.
    """
    if provider not in ('rijksmuseum', 'getty'):
        raise MuseumImportError(provider, 'invalid_input')
    try:
        if provider == 'rijksmuseum':
            xml = getattr(fetch_json, 'xml', None)
            oai_cursor = isinstance(cursor, dict) and cursor.get('mode') == 'oai-edm'
            if oai_cursor or cursor is None and callable(xml):
                if not callable(xml): raise MuseumImportError(provider, 'invalid_cursor')
                yield from _rijks_oai_pages(xml, cursor)
            else:
                yield from _rijks_pages(fetch_json, cursor)
        else:
            yield from _getty_pages(fetch_json, cursor)
    except MuseumImportError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise MuseumImportError(provider, 'invalid_response') from None
