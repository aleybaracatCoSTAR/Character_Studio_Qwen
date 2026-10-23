from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import wave

from experiments.drama3.test_voice import build_payload, stream_pcm, listening_page, generate, RATE


class Response(BytesIO):
    def __init__(self, chunks, content_type="audio/pcm"):
        super().__init__(); self.chunks=iter(chunks); self.headers={"Content-Type":content_type}
    def read1(self, size): return next(self.chunks,b"")


class DramaTests(unittest.TestCase):
    def test_requests_raw_stream_not_finished_wav(self):
        data=build_payload("Hello")
        self.assertTrue(data["stream"]); self.assertEqual(data["stream_format"],"audio")
        self.assertEqual(data["response_format"],"pcm")
        self.assertNotIn("ref_audio",data)
        self.assertNotIn("sample_rate",data)

    def test_server_validation_error_includes_its_reason(self):
        body=BytesIO(b'{"error":{"message":"sample_rate is not supported by the current TTS model"}}')
        error=HTTPError('http://local.invalid/v1/audio/speech',400,'Bad Request',{},body)
        with patch('experiments.drama3.test_voice.urlopen',side_effect=error):
            with self.assertRaisesRegex(ValueError,'sample_rate is not supported'):
                generate('http://local.invalid',build_payload('Hello'),'unused.wav')

    def test_reference_requires_transcript_and_preserves_local_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"voice.wav"; path.write_bytes(b"demo")
            with self.assertRaises(ValueError): build_payload("hello",reference=path)
            data=build_payload("hello",reference=path,transcript="Reference words")
            self.assertEqual(data["ref_audio"],"data:audio/wav;base64,ZGVtbw==")
            self.assertEqual(data["ref_text"],"Reference words")

    def test_pcm_across_odd_chunks_becomes_valid_wav(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"output.wav"; times=iter([10.2,10.7])
            result=stream_pcm(Response([b"\x01",b"\x02\x03",b"\x04"]),path,10,clock=lambda:next(times))
            self.assertEqual(result["first_audio_seconds"],.2); self.assertEqual(result["total_seconds"],.7)
            with wave.open(str(path)) as reader:
                self.assertEqual(reader.getframerate(),RATE)
                self.assertEqual(reader.readframes(2),b"\x01\x02\x03\x04")

    def test_invalid_formats_and_empty_audio_do_not_leave_output(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"output.wav"
            for response in (Response([],"text/event-stream"),Response([]),Response([b"x"]),Response([b"RIFFxxxx"])):
                with self.assertRaises(ValueError): stream_pcm(response,path,0,clock=lambda:1)
                self.assertFalse(path.exists()); self.assertFalse(path.with_suffix(".partial.wav").exists())

    def test_listen_page_escapes_text(self):
        with tempfile.TemporaryDirectory() as folder:
            listening_page([{"label":"test","text":"<script>bad</script>","file":"test.wav",
                             "metrics":{"first_audio_seconds":1,"total_seconds":2,"audio_seconds":3}}],Path(folder))
            page=(Path(folder)/"listen.html").read_text()
            self.assertNotIn("<script>",page); self.assertIn("&lt;script&gt;",page)
            self.assertIn("Built with Fish Audio",page)


if __name__=="__main__": unittest.main()
