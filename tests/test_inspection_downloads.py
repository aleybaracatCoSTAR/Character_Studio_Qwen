"""Model-input exports are exact UTF-8 attachments with bounded RAM retention."""
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from live_voice.app import create_app
from live_voice.inspection_downloads import ExportDownloads, EXPORT_TTL, MAX_EXPORT_BYTES, MAX_EXPORTS
from test_live_voice import FakeProviders


class Clock:
    def __init__(self): self.now = 0.0
    def __call__(self): return self.now


class ExportDownloadTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.downloads = ExportDownloads(clock=self.clock)

    def test_tokens_are_unguessable_and_records_hold_exact_utf8_bytes(self):
        content = 'Anaïs — القاهرة\n<|speaker:0|> [calm] Complete words…\n'
        token = self.downloads.create('model-input.txt', content, 'text/plain;charset=utf-8')
        self.assertRegex(token,r'^[a-f0-9]{48}$')
        actual = self.downloads.get(token)
        self.assertEqual(actual['filename'],'model-input.txt')
        self.assertEqual(actual['mime'],'text/plain')
        self.assertEqual(actual['content'],content.encode('utf-8'))
        other = self.downloads.create('model-input.txt',content,'text/plain')
        self.assertNotEqual(token,other)
        self.assertIsNone(self.downloads.get('0'*48))

    def test_download_tokens_remain_reusable_only_until_the_two_minute_expiry(self):
        token = self.downloads.create('prompt.txt','A complete prompt.','text/plain')
        self.clock.now = EXPORT_TTL-.001
        self.assertIsNotNone(self.downloads.get(token))
        self.assertIsNotNone(self.downloads.get(token))
        self.clock.now = EXPORT_TTL
        self.assertIsNone(self.downloads.get(token))
        self.assertEqual(self.downloads.files,{})

    def test_create_evicts_oldest_pending_download_and_never_exceeds_four_records(self):
        tokens = []
        for index in range(MAX_EXPORTS+1):
            tokens.append(self.downloads.create(f'prompt-{index}.txt',f'Prompt {index}','text/plain'))
        self.assertEqual(MAX_EXPORTS,4)
        self.assertLessEqual(len(self.downloads.files),MAX_EXPORTS)
        self.assertIsNone(self.downloads.get(tokens[0]))
        self.assertTrue(all(self.downloads.get(token) is not None for token in tokens[1:]))

    def test_expired_records_are_purged_before_a_new_download_and_clear_releases_all(self):
        old = self.downloads.create('old.txt','Old prompt.','text/plain')
        self.clock.now = EXPORT_TTL+1
        current = self.downloads.create('current.json','{"current":true}','application/json')
        self.assertNotIn(old,self.downloads.files)
        self.assertEqual(len(self.downloads.files),1)
        self.downloads.clear()
        self.assertIsNone(self.downloads.get(current))

    def test_filename_mime_and_empty_content_validation_rejects_paths_or_header_injection(self):
        for filename in ('../prompt.txt','/tmp/prompt.txt','folder/prompt.txt','folder\\prompt.txt',
                         'prompt..txt','prompt.txt\r\nX-Test: injected','prompt.txt"','prompt.html',
                         '.hidden.txt','name with spaces.txt','x'*121+'.txt',None,7):
            with self.subTest(filename=filename), self.assertRaises((ValueError,TypeError)):
                self.downloads.create(filename,'PRIVATE_SENTINEL','text/plain')
        for mime in ('text/html','application/javascript','image/svg+xml','application/octet-stream',None,7):
            with self.subTest(mime=mime), self.assertRaises((ValueError,TypeError)):
                self.downloads.create('prompt.txt','PRIVATE_SENTINEL',mime)
        for content in ('',None,{},7):
            with self.subTest(content=content), self.assertRaises((ValueError,TypeError)):
                self.downloads.create('prompt.txt',content,'text/plain')
        with self.assertRaises(ValueError): self.downloads.create('prompt.txt','{}','application/json')
        self.assertEqual(self.downloads.files,{})

    def test_size_limit_counts_utf8_bytes_and_accepts_the_exact_file_boundary(self):
        content = 'x'*MAX_EXPORT_BYTES
        token = self.downloads.create('largest.txt',content,'text/plain')
        self.assertEqual(len(self.downloads.get(token)['content']),MAX_EXPORT_BYTES)
        with self.assertRaises(OverflowError): self.downloads.create('large.txt',content+'x','text/plain')
        # Arabic characters take more than one byte despite a smaller str length.
        with self.assertRaises(OverflowError):
            self.downloads.create('unicode.txt','ع'*(MAX_EXPORT_BYTES//2+1),'text/plain')


class ExportDownloadAPITests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.downloads = ExportDownloads(clock=self.clock)
        self.providers = FakeProviders()
        self.store_patch = patch('live_voice.app.ExportDownloads',return_value=self.downloads)
        self.store_patch.start()
        self.client = TestClient(create_app(preview=True,providers=self.providers))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None,None,None)
        self.store_patch.stop()

    def create(self,filename='prompt.txt',content='Actual captured prompt.',mime='text/plain'):
        return self.client.post('/api/inspection/downloads',headers={'origin':'http://testserver'},
                                json={'filename':filename,'content':content,'mime':mime})

    def test_text_and_json_attachments_preserve_exact_body_and_safe_download_headers(self):
        text = 'Anaïs — القاهرة\nSYSTEM: Preserve the actual prompt.\n[anxious] Please wait…\n'
        value = {'export_format':'character-studio-model-inputs','requests':[{'payload':{'messages':[
            {'role':'system','content':'Anaïs — القاهرة'}, {'role':'user','content':'No missing words.'}]}}]}
        cases = [('prompt.txt',text,'text/plain'),('requests.json',json.dumps(value,ensure_ascii=False,indent=2)+'\n','application/json')]
        for filename,content,mime in cases:
            with self.subTest(filename=filename):
                prepared = self.create(filename,content,mime)
                self.assertEqual(prepared.status_code,200)
                url = prepared.json()['download_url']
                self.assertRegex(url,r'^/api/inspection/downloads/[a-f0-9]{48}$')
                result = self.client.get(url)
                self.assertEqual(result.status_code,200)
                self.assertEqual(result.content,content.encode('utf-8'))
                self.assertEqual(result.text,content)
                self.assertEqual(result.headers['content-disposition'],f'attachment; filename="{filename}"')
                self.assertIn(mime,result.headers['content-type'])
                self.assertIn('charset=utf-8',result.headers['content-type'].lower())
                self.assertEqual(result.headers['cache-control'],'no-store')
                self.assertEqual(result.headers['x-content-type-options'],'nosniff')
                self.assertEqual(prepared.headers['cache-control'],'no-store')
        self.assertEqual(self.providers.spoken,[])

    def test_post_requires_exact_same_origin_including_rejecting_missing_origin(self):
        for origin in (None,'null','https://testserver','http://testserver:8080','http://testserver.evil.invalid',
                       'http://other.invalid','http://testserver/','http://testserver/path'):
            headers = {} if origin is None else {'origin':origin}
            result = self.client.post('/api/inspection/downloads',headers=headers,
                json={'filename':'prompt.txt','mime':'text/plain','content':'PRIVATE_SENTINEL'})
            with self.subTest(origin=origin):
                self.assertEqual(result.status_code,403)
                self.assertNotIn('PRIVATE_SENTINEL',result.text)
        self.assertEqual(self.downloads.files,{})

    def test_get_navigation_can_omit_origin_but_cross_origin_fetch_is_rejected(self):
        url = self.create().json()['download_url']
        self.assertEqual(self.client.get(url).status_code,200)
        self.assertEqual(self.client.get(url,headers={'origin':'http://testserver'}).status_code,200)
        result = self.client.get(url,headers={'origin':'https://other.invalid'})
        self.assertEqual(result.status_code,403)
        self.assertNotIn('Actual captured prompt.',result.text)

    def test_invalid_json_content_types_filenames_and_payload_shapes_return_safe_400(self):
        for body,content_type in ((b'{not JSON PRIVATE_SENTINEL','application/json'),
                                  (b'PRIVATE_SENTINEL','text/plain'),
                                  (b'null','application/json'),(b'[]','application/json')):
            result = self.client.post('/api/inspection/downloads',headers={
                'origin':'http://testserver','content-type':content_type},content=body)
            with self.subTest(body=body):
                self.assertEqual(result.status_code,400)
                self.assertNotIn('PRIVATE_SENTINEL',result.text)
        for values in ({}, {'filename':'../PRIVATE_SENTINEL.txt','content':'PRIVATE_SENTINEL','mime':'text/plain'},
                       {'filename':'prompt.html','content':'PRIVATE_SENTINEL','mime':'text/html'},
                       {'filename':'prompt.txt','content':7,'mime':'text/plain'}):
            result = self.client.post('/api/inspection/downloads',headers={'origin':'http://testserver'},json=values)
            with self.subTest(values=values):
                self.assertEqual(result.status_code,400)
                self.assertNotIn('PRIVATE_SENTINEL',result.text)
        self.assertEqual(self.downloads.files,{})

    def test_oversized_raw_body_and_multibyte_content_return_413_without_retention(self):
        result = self.client.post('/api/inspection/downloads',headers={
            'origin':'http://testserver','content-type':'application/json'},content=b'X'*(MAX_EXPORT_BYTES+1))
        self.assertEqual(result.status_code,413)
        self.assertLess(len(result.content),1024)
        result = self.create(content='ع'*(MAX_EXPORT_BYTES//2+1))
        self.assertEqual(result.status_code,413)
        self.assertEqual(self.downloads.files,{})

    def test_unknown_expired_and_evicted_tokens_return_404(self):
        unknown = self.client.get('/api/inspection/downloads/'+'0'*48)
        self.assertEqual(unknown.status_code,404)
        first = self.create(content='PRIVATE_SENTINEL').json()['download_url']
        for index in range(MAX_EXPORTS): self.create(f'prompt-{index}.txt',f'Prompt {index}')
        self.assertEqual(self.client.get(first).status_code,404)
        current = self.create().json()['download_url']
        self.clock.now = EXPORT_TTL
        expired = self.client.get(current)
        self.assertEqual(expired.status_code,404)
        self.assertNotIn('PRIVATE_SENTINEL',expired.text)
        self.assertEqual(self.downloads.files,{})

    def test_app_shutdown_clears_all_pending_export_bytes(self):
        url = self.create(content='PRIVATE_SENTINEL').json()['download_url']
        self.assertEqual(self.client.get(url).status_code,200)
        self.assertTrue(self.downloads.files)
        self.client.__exit__(None,None,None)
        self.assertEqual(self.downloads.files,{})
        self.assertTrue(self.providers.closed)
        # Re-entering keeps tearDown balanced, without retaining the old export.
        self.client.__enter__()
        self.assertEqual(self.client.get(url).status_code,404)


if __name__ == '__main__': unittest.main()
