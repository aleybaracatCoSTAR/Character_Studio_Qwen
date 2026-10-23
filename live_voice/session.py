import asyncio
import logging
import struct
import time

from .providers import synthetic_reference
from .turns import PhraseChunker, ReplyHistory
from .voice import Delivery, VoiceSettings, spoken_input

logger = logging.getLogger(__name__)


class LiveSession:
    def __init__(self, providers, character, send_json, send_bytes, voice=None, trigger=None):
        self.providers = providers
        self.character = character
        self.send_json = send_json
        self.send_bytes = send_bytes
        self.history = ReplyHistory()
        self.task = None
        self.turn_id = 0
        self.voice = voice or VoiceSettings()
        self.reference = self.voice.reference()
        self.started = 0
        self.reply_pending = False
        self.generation_complete = False
        self.trigger = trigger
        self.trigger_active = False
        self.trigger_cleared = False
        self.trigger_evidence = ""

    async def trigger_state(self, active, evidence="", source="director"):
        if self.trigger_active and not active: self.trigger_cleared = True
        if active: self.trigger_cleared = False
        self.trigger_active = bool(active and self.trigger)
        self.trigger_evidence = evidence if active else ""
        await self.send_json({"type":"trigger_state", "active":self.trigger_active,
                              "emotion":self.trigger.emotion if self.trigger_active else self.voice.emotion,
                              "evidence":evidence,"source":source})

    async def fire_trigger(self):
        if not self.trigger: return
        await self.trigger_state(True, self.trigger.condition)
        await self.begin(text=f"[Stage action: {self.trigger.condition}]", action=True)

    async def interrupt(self):
        old = self.turn_id
        was_active = self.reply_pending
        self.reply_pending = False
        self.generation_complete = False
        heard = self.history.interrupt()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if old and was_active:
            await self.send_json({"type":"interrupted", "turn_id":old, "heard":heard})

    def playback_done(self, turn_id):
        """Listen again only after generation and all browser playback acknowledgements finish."""
        if (turn_id != self.turn_id or not self.reply_pending or not self.generation_complete
                or self.history.current != turn_id
                or self.history.next_ack != len(self.history.phrases)):
            return False
        self.reply_pending = False
        self.history.current = None
        return True

    async def begin(self, pcm=None, text=None, endpoint_ms=0, action=False):
        await self.interrupt()
        self.turn_id += 1
        turn_id = self.turn_id
        self.reply_pending = True
        self.generation_complete = False
        self.started = time.perf_counter() - endpoint_ms / 1000
        self.task = asyncio.create_task(self.run(turn_id, pcm, text, endpoint_ms, action))

    async def run(self, turn_id, pcm, text, endpoint_ms, action=False):
        started = self.started
        metrics = {"endpoint_ms":endpoint_ms}
        try:
            await self.send_json({"type":"turn_start", "turn_id":turn_id})
            now = time.perf_counter()
            if text is None: text = await self.providers.stt.transcribe(pcm)
            metrics["stt_ms"] = round((time.perf_counter() - now) * 1000)
            if not text or not text.strip():
                self.reply_pending = False
                await self.send_json({"type":"empty_transcript", "turn_id":turn_id}); return
            await self.send_json({"type":"transcript", "turn_id":turn_id, "text":text, "source":"action" if action else "speech"})
            if self.trigger and not action:
                check_started = time.perf_counter()
                try:
                    decision = await asyncio.wait_for(
                        self.providers.assess_trigger(self.character,self.trigger,self.history.messages,text,self.trigger_active),4.5)
                    metrics["context_check_ms"] = round((time.perf_counter()-check_started)*1000)
                    if decision.action == "activate":
                        await self.trigger_state(True, decision.evidence, source="context")
                    elif decision.action == "release" and self.trigger_active:
                        await self.trigger_state(False, decision.evidence, source="context")
                    await self.send_json({"type":"trigger_check","ok":True,"action":decision.action,
                                          "evidence":decision.evidence,"ms":metrics["context_check_ms"]})
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    metrics["context_check_ms"] = round((time.perf_counter()-check_started)*1000)
                    logger.warning("Emotional context check unavailable (%s).",type(exc).__name__)
                    await self.send_json({"type":"trigger_check","ok":False,
                                          "message":"Context check unavailable. Current emotion is unchanged; the test action still works."})
            # Snapshot state per answer; live controls affect the next reply.
            active = self.trigger if self.trigger_active else None
            delivery = Delivery(emotion=active.emotion if active else self.voice.emotion, event=self.voice.event,
                                direction=active.delivery if active else self.voice.direction)
            target = self.character
            if active:
                target = self.character.model_copy(update={"beat":self.character.beat + "\n\n" + active.state_note(self.trigger_evidence)})
            elif self.trigger_cleared:
                target = self.character.model_copy(update={"beat":self.character.beat +
                    "\n\nThe earlier trigger has been resolved by the director. Return to your normal emotional state and behaviour. You can remember what happened, but it is no longer an ongoing threat."})
            self.history.begin(turn_id, text)
            queue = asyncio.Queue(maxsize=2)
            expressive = delivery.emotion != "neutral" or bool(delivery.direction.strip())
            chunker = PhraseChunker(target_chars=200 if expressive else 85, sentence_only=expressive)
            answer = []
            llm_started = time.perf_counter()

            async def produce():
                async for delta in self.providers.text(target, self.history.prompt_messages()):
                    if not answer: metrics["llm_first_token_ms"] = round((time.perf_counter() - llm_started) * 1000)
                    answer.append(delta)
                    await self.send_json({"type":"text_delta", "turn_id":turn_id, "text":delta})
                    for phrase in chunker.feed(delta): await queue.put(phrase)
                for phrase in chunker.feed("", final=True): await queue.put(phrase)
                await queue.put(None)

            async def consume():
                sequence = 0
                while True:
                    phrase = await queue.get()
                    if phrase is None: break
                    self.history.add_phrase(sequence, phrase)
                    await self.send_json({"type":"phrase_start", "turn_id":turn_id, "seq":sequence, "text":phrase})
                    tts_started = time.perf_counter()
                    reference_audio = bytearray()
                    speech = spoken_input(phrase, delivery, first_phrase=sequence == 0)
                    async for chunk in self.providers.audio(speech, self.reference):
                        if "tts_first_chunk_ms" not in metrics:
                            metrics["tts_first_chunk_ms"] = round((time.perf_counter() - tts_started) * 1000)
                            metrics["speech_end_to_first_audio_ms"] = round((time.perf_counter() - started) * 1000)
                            await self.send_json({"type":"metrics", "turn_id":turn_id, **metrics})
                        if self.reference is None: reference_audio.extend(chunk)
                        await self.send_bytes(struct.pack("!II", turn_id, sequence) + chunk)
                    # Use the first generated phrase as the synthetic voice reference for this call.
                    if self.reference is None and reference_audio:
                        self.reference = synthetic_reference(bytes(reference_audio), phrase)
                    await self.send_json({"type":"phrase_done", "turn_id":turn_id, "seq":sequence})
                    sequence += 1

            tasks = [asyncio.create_task(produce()), asyncio.create_task(consume())]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done(): task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            if not answer: raise RuntimeError("The text model returned no spoken reply.")
            self.generation_complete = True
            await self.send_json({"type":"response_done", "turn_id":turn_id, "text":"".join(answer)})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.reply_pending = False
            # Keep dialogue, character data and captured audio out of logs.
            logger.warning("Live turn failed (%s); inspect the local model service logs.", type(exc).__name__)
            await self.send_json({"type":"error", "turn_id":turn_id,
                                  "message":"Voice generation failed. Check the LLM and Drama 3 server logs."})

    async def close(self):
        if self.task:
            self.task.cancel(); await asyncio.gather(self.task, return_exceptions=True)
