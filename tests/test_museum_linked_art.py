"""Fixtures mirror verified Rijksmuseum/Getty APIs; no live network in tests."""
from copy import deepcopy
import json
import re
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from live_voice import museum_getty as getty, museum_rijksmuseum as rijks
from live_voice.museum_common import MuseumImportError


CC0 = 'http://creativecommons.org/publicdomain/zero/1.0/'
PDM = 'https://creativecommons.org/publicdomain/mark/1.0/'
COPYRIGHT = 'https://creativecommons.org/licenses/by/4.0/'
ENGLISH = 'http://vocab.getty.edu/aat/300388277'
DUTCH = 'http://vocab.getty.edu/aat/300388256'


def text(content, classification=None, language=None, **extra):
    return {'type':'Name', 'content':content,
            'classified_as':[{'id':classification}] if classification else [],
            'language':[{'id':language}] if language else [], **extra}


def rights(*licenses): return [{'type':'Right', 'classified_as':[{'id':value} for value in licenses]}]


def rijks_object(number=1, image=True):
    return {'id':f'https://id.rijksmuseum.nl/{number}', 'type':'HumanMadeObject',
            'identified_by':[
                text('Nederlandse titel', 'http://vocab.getty.edu/aat/300404670', DUTCH),
                text('English title', 'http://vocab.getty.edu/aat/300404670', ENGLISH),
                text('SK-A-1', 'http://vocab.getty.edu/aat/300312355', type='Identifier')],
            'produced_by':{'part':[{'carried_out_by':[{'notation':[{'@language':'en', '@value':'Rembrandt van Rijn'}]}]}],
                           'timespan':{'identified_by':[text('c. 1638', language=ENGLISH)]}},
            'referred_to_by':[
                text('oil on panel', 'http://vocab.getty.edu/aat/300435429', ENGLISH),
                text('29.5 × 42.5 cm', 'http://vocab.getty.edu/aat/300435430', ENGLISH),
                text('Purchased with support', 'http://vocab.getty.edu/aat/300026687', ENGLISH)],
            'classified_as':[{'notation':[{'@language':'en', '@value':'painting'}]}],
            'shows':[{'id':f'https://id.rijksmuseum.nl/{2000 + number}', 'type':'VisualItem'}] if image else []}


class RijksFixture:
    def __init__(self, objects=None, licenses=(PDM,), image_link='https://iiif.micr.io/AbCdE/full/max/0/default.jpg'):
        self.objects = objects or {1:rijks_object()}
        self.licenses = licenses; self.image_link = image_link; self.calls = []
        self.pages = {('creator', None):[1]}
        self.next = {}

    def __call__(self, url, params=None, headers=None):
        self.calls.append((url, deepcopy(params), deepcopy(headers)))
        if url == rijks.SEARCH_URL:
            field = 'creator' if 'creator' in params else 'title'
            key = (field, params.get('pageToken'))
            items = self.pages.get(key, [])
            result = {'type':'OrderedCollectionPage', 'orderedItems':[
                {'id':f'https://id.rijksmuseum.nl/{number}', 'type':'HumanMadeObject'} for number in items]}
            if key in self.next: result['next'] = self.next[key]
            return result
        identifier = int(url.rsplit('/', 1)[-1])
        if identifier in self.objects: return deepcopy(self.objects[identifier])
        if 2001 <= identifier <= 2999:
            return {'id':f'https://id.rijksmuseum.nl/{identifier}', 'type':'VisualItem',
                    'subject_to':rights(*self.licenses),
                    'digitally_shown_by':[{'id':'https://id.rijksmuseum.nl/500691108150747650111', 'type':'DigitalObject'}]}
        if identifier == 500691108150747650111:
            return {'id':f'https://id.rijksmuseum.nl/{identifier}', 'type':'DigitalObject',
                    'access_point':[{'id':self.image_link}]}
        raise AssertionError('Unexpected fixture URL: ' + url)


class RijksmuseumTests(unittest.TestCase):
    def test_verified_three_stage_image_resolution_and_english_metadata(self):
        source = RijksFixture()
        item = list(rijks.iter_records(source, 'Rembrandt', 1))[0]
        self.assertEqual(item['title'], 'English title')
        self.assertEqual(item['metadata']['artist'], 'Rembrandt van Rijn')
        self.assertEqual(item['metadata']['date'], 'c. 1638')
        self.assertEqual(item['metadata']['medium'], 'oil on panel')
        self.assertEqual(item['metadata']['dimensions'], '29.5 × 42.5 cm')
        self.assertEqual(item['metadata']['accession_number'], 'SK-A-1')
        self.assertEqual(item['source_url'], 'https://id.rijksmuseum.nl/1')
        self.assertEqual(item['image_url'], 'https://iiif.micr.io/AbCdE/full/!600,600/0/default.jpg')
        self.assertIn(PDM, item['metadata']['rights'])
        self.assertNotIn('collection_id', item)
        self.assertEqual(len(source.calls), 4)
        self.assertTrue(all(call[1] == {'_profile':'la-framed'} for call in source.calls[1:]))

    def test_unknown_restricted_or_conflicting_image_rights_leave_metadata_only(self):
        for licenses in ((), (COPYRIGHT,), (PDM, COPYRIGHT)):
            with self.subTest(licenses=licenses):
                source = RijksFixture(licenses=licenses)
                item = list(rijks.iter_records(source, 'Rembrandt', 1))[0]
                self.assertIsNone(item['image_url'])
                self.assertEqual(item['metadata']['artist'], 'Rembrandt van Rijn')
                self.assertEqual(len(source.calls), 3)

    def test_cursor_pagination_is_bounded_and_uses_only_validated_tokens(self):
        source = RijksFixture(objects={number:rijks_object(number, image=False) for number in (1, 2, 3)})
        source.pages = {('creator', None):[1, 2], ('creator', 'abc'):[3]}
        source.next = {('creator', None):{'id':rijks.SEARCH_URL + '?' + urlencode({'creator':'Rembrandt', 'pageToken':'abc'})}}
        with patch.object(rijks, 'PAGE_SIZE', 2): items = list(rijks.iter_records(source, 'Rembrandt', 3))
        self.assertEqual([item['source_entry_id'] for item in items], ['1', '2', '3'])
        searches = [call for call in source.calls if call[0] == rijks.SEARCH_URL]
        self.assertEqual([call[1] for call in searches], [{'creator':'Rembrandt'}, {'creator':'Rembrandt', 'pageToken':'abc'}])

    def test_creator_and_title_searches_form_a_deduplicated_union(self):
        source = RijksFixture(objects={1:rijks_object(1, image=False), 2:rijks_object(2, image=False)})
        source.pages = {('creator', None):[1], ('title', None):[1, 2]}
        items = list(rijks.iter_records(source, 'Night Watch', 3))
        self.assertEqual([item['source_entry_id'] for item in items], ['1', '2'])
        self.assertEqual([call[1] for call in source.calls if call[0] == rijks.SEARCH_URL],
                         [{'creator':'Night Watch'}, {'title':'Night Watch'}])

    def test_untrusted_cursor_resolver_and_image_links_never_become_network_targets(self):
        source = RijksFixture(objects={1:rijks_object(1, image=False)})
        source.next = {('creator', None):{'id':'https://127.0.0.1/private?pageToken=abc'}}
        with self.assertRaises(MuseumImportError) as error: list(rijks.iter_records(source, 'Rembrandt', 2))
        self.assertEqual(error.exception.reason, 'invalid_link')
        self.assertTrue(all(call[0].startswith('https://data.rijksmuseum.nl/') for call in source.calls))
        source = RijksFixture(image_link='https://evil.example/image.jpg')
        with self.assertRaises(MuseumImportError): list(rijks.iter_records(source, 'Rembrandt', 1))
        source = RijksFixture()
        source.objects[1]['shows'][0]['id'] = 'https://127.0.0.1/private'
        with self.assertRaises(MuseumImportError): list(rijks.iter_records(source, 'Rembrandt', 1))
        self.assertEqual(len(source.calls), 2)

    def test_missing_images_and_invalid_record_identity_are_distinct(self):
        source = RijksFixture(objects={1:rijks_object(1, image=False)})
        self.assertIsNone(list(rijks.iter_records(source, 'Rembrandt', 1))[0]['image_url'])
        source.objects[1]['id'] = 'https://id.rijksmuseum.nl/2'
        with self.assertRaises(MuseumImportError) as error: list(rijks.iter_records(source, 'Rembrandt', 1))
        self.assertEqual(error.exception.reason, 'invalid_response')


def uuid(number): return f'00000000-0000-0000-0000-{number:012x}'


def getty_object(number=1, image=True):
    return {'id':'https://data.getty.edu/museum/collection/object/' + uuid(number),
            'type':'HumanMadeObject', '_label':'Irises (90.PA.20)',
            'identified_by':[text('Irises', 'http://vocab.getty.edu/aat/300404670'),
                             text('90.PA.20', 'http://vocab.getty.edu/aat/300312355', type='Identifier')],
            'produced_by':{'carried_out_by':[{'_label':'Vincent van Gogh'}],
                           'timespan':{'identified_by':[text('1889')]}},
            'referred_to_by':[text('Oil on canvas', 'http://vocab.getty.edu/aat/300435429'),
                              text('74.3 × 94.3 cm', 'http://vocab.getty.edu/aat/300435430'),
                              text('PRIVATE_THIRD_PARTY_DESCRIPTION', 'http://vocab.getty.edu/aat/300435452', subject_to=rights(COPYRIGHT))],
            'classified_as':[{'_label':'Paintings'}],
            'subject_to':rights(CC0),
            'subject_of':[{'id':'https://www.getty.edu/art/collection/object/103JNH', 'format':'text/html'}],
            'shows':[{'id':'https://data.getty.edu/media/image/' + uuid(1001),
                      'classified_as':[{'id':'http://vocab.getty.edu/aat/300215302'}]}] if image else []}


class GettyFixture:
    def __init__(self, objects=None, licenses=(CC0,), image_link=None):
        self.objects = objects or {1:getty_object()}
        self.licenses = licenses
        self.image_link = image_link or 'https://media.getty.edu/iiif/image/' + uuid(2001)
        self.calls = []
        self.pages = {0:[1]}

    def __call__(self, url, params=None, headers=None):
        self.calls.append((url, deepcopy(params), deepcopy(headers)))
        if url == getty.SEARCH_URL:
            offset = int(re.search(r'OFFSET (\d+)', params['query']).group(1))
            return {'results':{'bindings':[{'object':{'type':'uri', 'value':
                'https://data.getty.edu/museum/collection/object/' + uuid(number)}} for number in self.pages.get(offset, [])]}}
        if '/museum/collection/object/' in url:
            return deepcopy(self.objects[int(url.rsplit('-', 1)[-1], 16)])
        if '/media/image/' in url:
            return {'id':'https://data.getty.edu/media/image/' + uuid(1001), 'type':'VisualItem',
                    'subject_to':rights(*self.licenses),
                    'digitally_shown_by':[{'access_point':[{'id':self.image_link}]}]}
        raise AssertionError('Unexpected fixture URL: ' + url)


class GettyTests(unittest.TestCase):
    def test_metadata_and_separately_verified_cc0_image_are_normalized_without_curatorial_prose(self):
        source = GettyFixture()
        source.objects[1]['shows'].insert(0, {
            'id':source.objects[1]['id'] + '/subjects', 'type':'VisualItem',
            'classified_as':[{'id':'http://vocab.getty.edu/aat/300404126'}]})
        item = list(getty.iter_records(source, 'Van Gogh', 1))[0]
        self.assertEqual(item['title'], 'Irises')
        self.assertEqual(item['metadata']['artist'], 'Vincent van Gogh')
        self.assertEqual(item['metadata']['date'], '1889')
        self.assertEqual(item['metadata']['medium'], 'Oil on canvas')
        self.assertEqual(item['source_url'], 'https://www.getty.edu/art/collection/object/103JNH')
        self.assertEqual(item['image_url'], 'https://media.getty.edu/iiif/image/' + uuid(2001) + '/full/!600,600/0/default.jpg')
        self.assertNotIn('PRIVATE_THIRD_PARTY_DESCRIPTION', json.dumps(item))
        self.assertEqual(source.calls[0][2], {'Accept':'application/sparql-results+json'})
        self.assertEqual(len(source.calls), 3)

    def test_object_metadata_cc0_is_never_used_as_the_image_license(self):
        for licenses in ((), (COPYRIGHT,), (CC0, COPYRIGHT)):
            with self.subTest(licenses=licenses):
                item = list(getty.iter_records(GettyFixture(licenses=licenses), 'Irises', 1))[0]
                self.assertIsNone(item['image_url'])
                self.assertIn('image omitted', item['metadata']['rights'])
                self.assertEqual(item['metadata']['artist'], 'Vincent van Gogh')

    def test_fixed_sparql_literal_escaping_and_limit_offset_pagination(self):
        source = GettyFixture(objects={number:getty_object(number, image=False) for number in (1, 2, 3)})
        source.pages = {0:[1, 2], 2:[3]}
        query = 'Van Gogh" ) } SERVICE <https://evil.example/> #'
        with patch.object(getty, 'PAGE_SIZE', 2): items = list(getty.iter_records(source, query, 3))
        self.assertEqual(len(items), 3)
        searches = [call[1]['query'] for call in source.calls if call[0] == getty.SEARCH_URL]
        self.assertIn(json.dumps(query.casefold(), ensure_ascii=False), searches[0])
        self.assertTrue(searches[0].endswith('ORDER BY ?object LIMIT 2 OFFSET 0'))
        self.assertTrue(searches[1].endswith('ORDER BY ?object LIMIT 1 OFFSET 2'))
        self.assertIn('crm:P108i_was_produced_by/crm:P14_carried_out_by/rdfs:label', searches[0])
        self.assertTrue(all(call[0].startswith('https://data.getty.edu/') for call in source.calls))

    def test_untrusted_object_media_homepage_and_image_links_are_rejected(self):
        source = GettyFixture()
        source.objects[1]['shows'][0]['id'] = 'https://data.getty.edu/private'
        with self.assertRaises(MuseumImportError): list(getty.iter_records(source, 'Irises', 1))
        self.assertEqual(len(source.calls), 2)
        source = GettyFixture(image_link='https://127.0.0.1/image.jpg')
        with self.assertRaises(MuseumImportError): list(getty.iter_records(source, 'Irises', 1))
        source = GettyFixture()
        source.objects[1]['subject_of'][0]['id'] = 'https://evil.example/claimed-homepage'
        with self.assertRaises(MuseumImportError): list(getty.iter_records(source, 'Irises', 1))
        self.assertEqual(len(source.calls), 2)

    def test_missing_images_preserve_metadata_and_response_shape_is_checked(self):
        source = GettyFixture(objects={1:getty_object(1, image=False)})
        self.assertIsNone(list(getty.iter_records(source, 'Irises', 1))[0]['image_url'])
        source.objects[1]['id'] = 'https://data.getty.edu/museum/collection/object/' + uuid(2)
        with self.assertRaises(MuseumImportError) as error: list(getty.iter_records(source, 'Irises', 1))
        self.assertEqual(error.exception.reason, 'invalid_response')
        with self.assertRaises(MuseumImportError): list(getty.iter_records(lambda *args, **kwargs:{}, 'Irises', 1))


class MuseumInputTests(unittest.TestCase):
    def test_null_optional_classifications_and_malformed_ids_cannot_raise_raw_type_errors(self):
        for adapter, fixture, object_key in ((rijks, RijksFixture, 'English title'), (getty, GettyFixture, 'Irises')):
            with self.subTest(provider=adapter.PROVIDER):
                source = fixture()
                source.objects[1]['identified_by'][0]['classified_as'] = None
                self.assertEqual(list(adapter.iter_records(source, 'artist', 1))[0]['title'], object_key)
                source = fixture()
                source.objects[1]['identified_by'][0]['classified_as'] = [{'id':['invalid', 'not hashable']}]
                with self.assertRaises(MuseumImportError) as error: list(adapter.iter_records(source, 'artist', 1))
                self.assertEqual(error.exception.reason, 'invalid_response')

    def test_both_iterators_validate_before_any_network_call(self):
        for adapter in (rijks, getty):
            for query, limit in (('', 1), ('x' * 201, 1), ('hello\nworld', 1), ('art', 0), ('art', 1001), ('art', True)):
                with self.subTest(provider=adapter.PROVIDER, query=query[:10], limit=limit):
                    calls = []
                    with self.assertRaises(MuseumImportError) as error:
                        list(adapter.iter_records(lambda *args, **kwargs:calls.append(args), query, limit))
                    self.assertEqual(error.exception.reason, 'invalid_input')
                    self.assertEqual(calls, [])


if __name__ == '__main__': unittest.main()
