from pathlib import Path
import json
import tempfile
import unittest
from experiments.drama3.benchmark_latency import benchmark

class DirectLatencyTests(unittest.TestCase):
    def test_warmup_is_excluded_and_fixed_reference_is_reused_without_exporting_audio_payload(self):
        calls=[]
        def generate(url,payload,path):
            calls.append((url,dict(payload)));path.write_bytes(b'test-only')
            value=[9,0.1,0.3,0.2][len(calls)-1]
            return {'first_audio_seconds':value,'total_seconds':value+1,'audio_seconds':4,'realtime_factor':.3,'bytes':8}
        with tempfile.TemporaryDirectory() as folder:
            report=benchmark(url='http://local.test',text='<test> Hello.',voice='female',runs=3,warmup=1,max_tokens=300,directory=folder,generate_fn=generate)
            self.assertEqual(len(calls),4);self.assertTrue(all(call[1]==calls[0][1] for call in calls))
            self.assertIn('ref_audio',calls[0][1]);self.assertEqual(report['summary']['first_audio_median_seconds'],.2)
            self.assertFalse((Path(folder)/'warmup.wav').exists());self.assertTrue((Path(folder)/'run-03.wav').is_file())
            data=(Path(folder)/'results.json').read_text();self.assertNotIn('base64',data);self.assertEqual(json.loads(data)['summary']['measured_runs'],3)
            page=(Path(folder)/'listen.html').read_text();self.assertIn('&lt;test&gt;',page);self.assertNotIn('<test>',page)

    def test_no_reference_control_and_partial_results_survive_a_failed_measured_request(self):
        calls=[]
        def generate(url,payload,path):
            self.assertNotIn('ref_audio',payload);calls.append(payload)
            if len(calls)==2:raise ValueError('Synthetic failure')
            return {'first_audio_seconds':.1,'total_seconds':1,'audio_seconds':3,'realtime_factor':.33,'bytes':8}
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError,'Synthetic failure'):
                benchmark(url='http://local.test',text='Hello.',voice='none',runs=3,warmup=0,max_tokens=300,directory=folder,generate_fn=generate)
            report=json.loads((Path(folder)/'results.json').read_text())
            self.assertEqual(report['summary']['measured_runs'],1);self.assertEqual(report['settings']['requested_runs'],3)
