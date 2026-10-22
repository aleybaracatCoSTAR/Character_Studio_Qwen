"""CPU-only contract checks. The fake generator is never exposed by the app."""
from io import BytesIO
import json
from types import SimpleNamespace
import unittest
import wave

import httpx
import numpy as np
from pydantic import ValidationError

from studio.app import create_app
from studio.audio import read_recording, write_reply, MAX_AUDIO_BYTES
from studio.characters import Character, example, system_prompt
from studio.engine import (Busy, CharacterChanged, MAX_PAIRS, MAX_SESSIONS, NotReady,
                           QwenEngine, SESSION_TTL, UserTurn, generated_ids, messages_for)


def wav(rate=48000, channels=1, seconds=.1, width=2):
    buffer = BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels); writer.setsampwidth(width); writer.setframerate(rate)
        writer.writeframes(b"\x00" * int(rate * seconds) * channels * width)
    return buffer.getvalue()


class AudioTests(unittest.TestCase):
    def test_resamples_stereo_to_mono_16k(self):
        audio = read_recording(wav(channels=2))
        self.assertEqual(audio.shape, (1600,))
        self.assertEqual(audio.dtype, np.float32)
        self.assertTrue(np.isfinite(audio).all())

    def test_rejects_invalid_truncated_and_oversize(self):
        for data in (b"webm", wav()[:-4], b"x" * (MAX_AUDIO_BYTES + 1)):
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                read_recording(data)

    def test_rejects_long_or_unsupported_recordings(self):
        for kwargs in ({"seconds": 30.01, "rate":8000}, {"width":1}, {"channels":3}, {"rate":4000}, {"seconds":0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                read_recording(wav(**kwargs))

    def test_reply_is_24k_pcm16(self):
        data = write_reply(np.array([0, .5, -2, 3], dtype=np.float32))
        with wave.open(BytesIO(data)) as reader:
            self.assertEqual((reader.getframerate(), reader.getsampwidth(), reader.getnchannels()), (24000,2,1))

    def test_rejects_invalid_model_audio(self):
        for array in ([], [float("nan")], [float("inf")]):
            with self.assertRaises(ValueError): write_reply(np.array(array))


class CharacterTests(unittest.TestCase):
    def test_prompt_carries_fixed_facts_and_context(self):
        anna = example(); prompt = system_prompt(anna)
        for value in (anna.name, anna.about, anna.environment, anna.goal, anna.rules, anna.memories, anna.beat):
            self.assertIn(value, prompt)
        self.assertIn("mistaken assumption", prompt)

    def test_blank_name_unknown_fields_and_unsupported_speaker(self):
        for kwargs in ({"name":" "}, {"name":"Anna", "secret":"x"}, {"name":"Anna", "speaker":"cloned"}):
            with self.assertRaises(ValidationError): Character(**kwargs)

    def test_profile_fingerprint_changes_with_a_fact(self):
        anna = example()
        self.assertNotEqual(anna.fingerprint(), anna.model_copy(update={"goal":"Leave the room"}).fingerprint())

    def test_history_keeps_audio_in_template_order(self):
        first, second = np.zeros(160), np.ones(160)
        messages, audios = messages_for(example(), [(UserTurn("earlier", first), "earlier reply")], UserTurn("now", second))
        self.assertEqual([item["role"] for item in messages], ["system","user","assistant","user"])
        self.assertIs(audios[0], first); self.assertIs(audios[1], second)
        self.assertEqual(messages[-1]["content"][0]["type"], "audio")

    def test_decode_removes_prompt_for_both_return_shapes(self):
        ids = np.array([[1,2,3,4]])
        for result in (ids, SimpleNamespace(sequences=ids)):
            self.assertEqual(generated_ids(result,2).tolist(), [[3,4]])


class FakeEngine(QwenEngine):
    def __init__(self):
        super().__init__(preview=False); self.state="ready"; self.calls=[]; self.fail=False

    def _generate(self, character, pairs, current, speak):
        self.calls.append((character, list(pairs), current, speak))
        if self.fail: raise RuntimeError("Synthetic test failure")
        return "A test reply.", write_reply(np.zeros(240)) if speak else None, pairs, 0


class EngineTests(unittest.TestCase):
    def test_preview_does_not_generate_or_load(self):
        engine = QwenEngine(); engine.load()
        self.assertEqual(engine.state,"preview"); self.assertIsNone(engine.model)
        with self.assertRaises(NotReady): engine.reply(example(),UserTurn("hello"))

    def test_busy_rejects_and_does_not_unlock_someone_else(self):
        engine=FakeEngine(); engine.lock.acquire()
        try:
            with self.assertRaises(Busy): engine.reply(example(),UserTurn("hello"))
            self.assertTrue(engine.lock.locked())
        finally: engine.lock.release()

    def test_failure_rolls_back_new_and_existing_history(self):
        engine=FakeEngine(); engine.fail=True
        with self.assertRaises(RuntimeError): engine.reply(example(),UserTurn("hello"))
        self.assertEqual(engine.sessions,{}); self.assertFalse(engine.lock.locked())
        engine.fail=False; result=engine.reply(example(),UserTurn("one")); key=result["session_id"]
        engine.fail=True
        with self.assertRaises(RuntimeError): engine.reply(example(),UserTurn("two"),key)
        self.assertEqual(len(engine.sessions[key].pairs),1)

    def test_character_change_rejected_and_sessions_isolated(self):
        engine=FakeEngine(); anna=example(); key=engine.reply(anna,UserTurn("hello"))["session_id"]
        with self.assertRaises(CharacterChanged): engine.reply(Character(name="Other"),UserTurn("hi"),key)
        other=engine.reply(Character(name="Other"),UserTurn("hi"))["session_id"]
        self.assertNotEqual(key,other); self.assertEqual(engine.calls[-1][1],[])

    def test_context_is_bounded_and_expired_sessions_are_pruned(self):
        engine=FakeEngine(); key=None
        for index in range(10): key=engine.reply(example(),UserTurn(str(index)),key)["session_id"]
        self.assertEqual(len(engine.sessions[key].pairs), MAX_PAIRS)
        engine.sessions[key].touched-=SESSION_TTL+1
        engine.reply(example(),UserTurn("fresh"))
        self.assertNotIn(key,engine.sessions)

    def test_session_count_is_bounded(self):
        engine=FakeEngine()
        for index in range(MAX_SESSIONS+2): engine.reply(example(),UserTurn(str(index)))
        self.assertEqual(len(engine.sessions),MAX_SESSIONS)


class APITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine=FakeEngine()
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(self.engine)),base_url="http://test")

    async def asyncTearDown(self): await self.client.aclose()

    def payload(self, **kwargs): return {"character":example().model_dump(),"text":"hello",**kwargs}

    async def send(self, data=None, **kwargs):
        return await self.client.post("/api/chat",data={"payload":json.dumps(data if data is not None else self.payload())},**kwargs)

    async def test_page_status_and_example(self):
        page=await self.client.get("/"); self.assertEqual(page.status_code,200)
        self.assertIn("Your character",page.text)
        self.assertEqual((await self.client.get("/api/status")).json()["model"],"Qwen/Qwen3-Omni-30B-A3B-Instruct")
        self.assertEqual((await self.client.get("/api/example")).json()["name"],"Anna")

    async def test_prompt_and_text_only_chat(self):
        result=await self.client.post("/api/prompt",json=example().model_dump())
        self.assertIn("Anna",result.json()["prompt"])
        reply=await self.send(self.payload(speak=False))
        self.assertEqual(reply.status_code,200); self.assertIsNone(reply.json()["audio_base64"])
        self.assertEqual(reply.headers["cache-control"],"no-store")

    async def test_audio_input_and_output(self):
        reply=await self.send(self.payload(text=""),files={"audio":("recording.wav",wav(),"audio/wav")})
        self.assertEqual(reply.status_code,200); self.assertEqual(reply.json()["audio_mime"],"audio/wav")
        self.assertEqual(len(self.engine.calls[-1][2].audio),1600)

    async def test_bad_payload_uuid_and_empty_message(self):
        response=await self.client.post("/api/chat",data={"payload":"{broken"})
        self.assertEqual(response.status_code,422)
        self.assertEqual((await self.send(self.payload(session_id="oops"))).status_code,422)
        self.assertEqual((await self.send(self.payload(text="  "))).status_code,400)

    async def test_invalid_recording_is_not_passed_to_model(self):
        reply=await self.send(files={"audio":("recording.wav",b"not wav","audio/wav")})
        self.assertEqual(reply.status_code,400); self.assertEqual(self.engine.calls,[])

    async def test_cross_origin_and_oversized_requests(self):
        reply=await self.send(headers={"Origin":"https://elsewhere.invalid"})
        self.assertEqual(reply.status_code,403)
        reply=await self.client.post("/api/chat",headers={"Content-Length":str(MAX_AUDIO_BYTES+65537)},content=b"x")
        self.assertEqual(reply.status_code,413)

    async def test_unready_busy_changed_and_failed_generation(self):
        self.engine.state="loading"; self.assertEqual((await self.send()).status_code,503)
        self.engine.state="ready"; self.engine.lock.acquire()
        try: self.assertEqual((await self.send()).status_code,409)
        finally: self.engine.lock.release()
        response=await self.send(); key=response.json()["session_id"]
        self.assertEqual((await self.send(self.payload(character={"name":"Other"},session_id=key))).status_code,409)
        self.engine.fail=True; failed=await self.send()
        self.assertEqual(failed.status_code,500); self.assertNotIn("Synthetic",failed.text)


if __name__ == "__main__": unittest.main()
