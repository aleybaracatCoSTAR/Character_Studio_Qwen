"""Small topic imports using the Art Institute of Chicago's public API."""
import re

from .museum_common import MuseumImportError, museum_record, validate_import_input

ENDPOINT = 'https://api.artic.edu/api/v1/artworks/search'
FIELDS = ('id,title,artist_display,date_display,medium_display,dimensions,credit_line,'
          'main_reference_number,artwork_type_title,image_id,is_public_domain,thumbnail')
_IMAGE_ID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z')


def iter_records(fetch_json, query, limit):
    query, limit = validate_import_input('aic',query,limit)
    page, received, seen = 1, 0, set()
    page_size = min(limit,100)
    while received<limit:
        result = fetch_json(ENDPOINT,params={'q':query,'fields':FIELDS,'limit':page_size,'page':page})
        data, pagination = result.get('data'), result.get('pagination')
        if not isinstance(data,list) or not isinstance(pagination,dict):
            raise MuseumImportError('aic','invalid_response')
        total = pagination.get('total')
        if type(total) is not int or total<0 or len(data)>page_size:
            raise MuseumImportError('aic','invalid_response')
        config = result.get('config') or {}
        base = config.get('iiif_url') if isinstance(config,dict) else None
        if base!='https://www.artic.edu/iiif/2':
            raise MuseumImportError('aic','invalid_link')
        if not data: break
        for value in data:
            if received>=limit: return
            if not isinstance(value,dict): raise MuseumImportError('aic','invalid_response')
            identity = value.get('id')
            if type(identity) is not int or identity<1 or identity in seen:
                raise MuseumImportError('aic','invalid_response')
            seen.add(identity)
            image_id = value.get('image_id')
            eligible = value.get('is_public_domain') is True
            image = (base+'/'+image_id+'/full/400,/0/default.jpg'
                     if eligible and isinstance(image_id,str) and _IMAGE_ID.fullmatch(image_id) else None)
            thumbnail = value.get('thumbnail')
            alt = thumbnail.get('alt_text','') if isinstance(thumbnail,dict) else ''
            yield museum_record('aic',identity,value.get('title'),
                f'https://www.artic.edu/artworks/{identity}',image_url=image,image_alt=alt,
                metadata={'artist':value.get('artist_display'),'date':value.get('date_display'),
                    'medium':value.get('medium_display'),'dimensions':value.get('dimensions'),
                    'credit_line':value.get('credit_line'),'accession_number':value.get('main_reference_number'),
                    'classification':value.get('artwork_type_title'),
                    'rights':'Catalogue metadata: CC0. Image: public domain (museum flag).'
                             if eligible else 'Catalogue metadata: CC0. No eligible image supplied.'})
            # The description field has a separate CC BY licence and is not
            # requested or included in this CC0 catalogue importer.
            received += 1
        if page*page_size>=total: break
        page += 1
        if page*page_size>1000:
            raise MuseumImportError('aic','request_limit')
