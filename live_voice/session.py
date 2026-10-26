import asyncio
import logging
import struct
import time

from .providers import OUTPUT_RATE, VoiceTakeIncomplete, synthetic_reference
from .director import DirectorSettings
from .narrative import NarrativeState
from .voice import EMOTION_DIRECTIONS
from .turns import ReplyHistory
from .voice import Delivery, VoiceSettings, spoken_input

logger = logging.getLogger(__name__)


class LiveSession:
    def __init__(self, providers, character, send_json, send_bytes, voice=None, trigger=None, director=None, narrative=None, clock=time.monotonic):
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
        self.director = director or DirectorSettings()
        self.last_plan = None
        self.story = NarrativeState(narrative,clock=clock)
        self.story_task = None
        self.story_director_task = None
        self.last_story_check = -1e9
        self.story_check_key = None
        self.turn_story_revision = 0
        self.remembered_turn = 0
        self.turn_action = False
        self.trigger = trigger
        self.trigger_active = False
        self.trigger_cleared = False
        self.trigger_evidence = ""

    async def story_state(self):
        await self.send_json({"type":"story_state",**self.story.view()})

    async def start_story(self):
        if not self.story.active: return
        await self.story_state()
        self.story_task = asyncio.create_task(self.story_loop())

    def remember_story_turn(self):
        if self.remembered_turn == self.turn_id or self.history.current != self.turn_id: return
        messages=self.history.messages[-2:]
        if len(messages)==2:
            self.story.remember(self.turn_story_revision,"" if self.turn_action else messages[0]['content'],messages[1]['content'])
            self.remembered_turn=self.turn_id

    async def story_boundary(self):
        if not self.story.active or self.reply_pending: return False
        moved=False
        if self.story.pending:
            action=self.story.pending;self.story.pending=None
            moved=self.apply_story_action(action)
        elif self.story.timeout(): moved=True
        if moved: await self.story_state()
        return moved

    def apply_story_action(self,action):
        if action == 'restart': self.story.restart();return True
        return self.story.move(-1 if action=='previous' else 1)

    async def story_control(self,action):
        if not self.story.active: return
        if action in ('pause','resume'):
            self.story.pause(action=='pause');await self.story_state();return
        if action=='cue':
            if not self.reply_pending and not self.story.finished:
                await self.begin(text='[Private story cue: respond naturally to your present situation. '+self.story.beat.note+']',action=True)
            return
        if self.reply_pending:
            self.story.pending=action
            await self.story_state();return
        if self.apply_story_action(action): await self.story_state()

    async def story_loop(self):
        ticks=0
        try:
            while True:
                await asyncio.sleep(1)
                moved=await self.story_boundary();ticks+=1
                if ticks%5==0 and not moved: await self.story_state()
                if (self.story.active and self.story.settings.mode=='director' and self.director.enabled
                        and not self.reply_pending and not self.story.finished and self.story.paused_at is None
                        and self.story.age>=self.story.beat.min_seconds and self.story.heard
                        and self.story.clock()-self.last_story_check>=12
                        and (not self.story_director_task or self.story_director_task.done())):
                    key=(self.story.revision,tuple((m['role'],m['content']) for m in self.story.heard))
                    if key!=self.story_check_key:
                        self.story_check_key=key;self.last_story_check=self.story.clock()
                        self.story_director_task=asyncio.create_task(self.check_story(key))
        except asyncio.CancelledError: raise
        except Exception as exc:
            logger.warning("Story clock stopped (%s).",type(exc).__name__)

    async def check_story(self,key):
        try:
            plan=await asyncio.wait_for(self.providers.direct(self.character,self.history.messages,'',
                self.director,self.voice,self.trigger,self.trigger_active,self.last_plan,False,self.story.context()),
                getattr(self.providers,'director_timeout',6)+0.5)
            if (self.story.revision!=key[0] or self.reply_pending or self.story.paused_at is not None
                    or not self.director.enabled): return
            advanced,rejected=self.story.completion(plan.beat_done,plan.beat_evidence)
            if not advanced and not rejected:
                self.story.update_note(plan.intention)
            if rejected:await self.send_json({'type':'story_decision','accepted':False,'reason':rejected})
            await self.story_state()
        except asyncio.CancelledError:raise
        except Exception as exc:
            logger.warning('Between-turn director unavailable (%s).',type(exc).__name__)
            await self.send_json({'type':'story_decision','accepted':False,
                                  'reason':'Director check unavailable; the clock and performer controls remain available.'})

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
        if was_active: self.remember_story_turn()
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
        self.remember_story_turn()
        self.reply_pending = False
        self.history.current = None
        return True

    async def begin(self, pcm=None, text=None, endpoint_ms=0, action=False):
        if self.story_director_task and not self.story_director_task.done():
            self.story_director_task.cancel();await asyncio.gather(self.story_director_task,return_exceptions=True)
        await self.interrupt()
        await self.story_boundary()
        self.turn_id += 1
        turn_id = self.turn_id
        self.reply_pending = True
        self.generation_complete = False
        self.turn_action = action
        self.turn_story_revision = self.story.revision
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
            plan = None
            if self.director.enabled:
                check_started = time.perf_counter()
                try:
                    plan = await asyncio.wait_for(self.providers.direct(
                        self.character,self.history.messages,text,self.director,self.voice,
                        self.trigger,self.trigger_active,self.last_plan,action,self.story.context()),
                        getattr(self.providers,"director_timeout",6)+0.5)
                    metrics["director_ms"] = round((time.perf_counter()-check_started)*1000)
                    if self.trigger and not action:
                        if plan.trigger_action == "activate":
                            await self.trigger_state(True,plan.evidence,source="director")
                        elif plan.trigger_action == "release" and self.trigger_active:
                            await self.trigger_state(False,plan.evidence,source="director")
                    advanced,rejected=self.story.completion(plan.beat_done,plan.beat_evidence,"" if action else text)
                    if advanced:
                        await self.story_state()
                        self.turn_story_revision=self.story.revision
                        if not self.story.finished:
                            b=self.story.beat
                            plan=plan.model_copy(update={"intention":b.note or b.goal,"emotion":b.emotion,
                                "delivery":b.delivery or EMOTION_DIRECTIONS.get(b.emotion,"Natural conversational delivery.")})
                    if rejected:
                        await self.send_json({"type":"story_decision","accepted":False,"reason":rejected})
                    self.last_plan = plan
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    metrics["director_ms"] = round((time.perf_counter()-check_started)*1000)
                    logger.warning("Director unavailable (%s).",type(exc).__name__)
                    await self.send_json({"type":"director_note","turn_id":turn_id,"ok":False,
                        "message":"Director unavailable. The actor will reply without a new note; current trigger state is unchanged."})
            elif self.trigger and not action:
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
            beat = self.story.beat if not self.story.finished else None
            delivery = Delivery(emotion=active.emotion if active else plan.emotion if plan else beat.emotion if beat else self.voice.emotion,
                                event=self.voice.event if self.voice.event != "none" else plan.event if plan else "none",
                                direction=active.delivery if active else plan.delivery if plan else beat.delivery if beat else self.voice.direction)
            if plan:
                # Report and act the delivery that will really reach TTS, including trigger overrides.
                plan = plan.model_copy(update={"emotion":delivery.emotion,"delivery":delivery.direction,"event":delivery.event})
                await self.send_json({"type":"director_note","turn_id":turn_id,"ok":True,
                                      **plan.model_dump(),"ms":metrics["director_ms"]})
            target = self.character
            if active:
                target = self.character.model_copy(update={"beat":self.character.beat + "\n\n" + active.state_note(self.trigger_evidence)})
            elif self.trigger_cleared:
                target = self.character.model_copy(update={"beat":self.character.beat +
                    "\n\nThe earlier trigger has been resolved by the director. Return to your normal emotional state and behaviour. You can remember what happened, but it is no longer an ongoing threat."})
            if self.director.notes.strip():
                target = target.model_copy(update={"beat":target.beat + "\n\nPrivate performer direction (preserve fixed facts): " + self.director.notes})
            if self.story.active:
                target = target.model_copy(update={"beat":target.beat + "\n\n" + self.story.runtime_note()})
            if plan:
                target = target.model_copy(update={"beat":target.beat + "\n\n" + plan.actor_note()})
            self.history.begin(turn_id, text)
            answer = []
            llm_started = time.perf_counter()
            async for delta in self.providers.text(target, self.history.prompt_messages()):
                if not answer: metrics["llm_first_token_ms"] = round((time.perf_counter() - llm_started) * 1000)
                answer.append(delta)
                await self.send_json({"type":"text_delta", "turn_id":turn_id, "text":delta})
            reply = "".join(answer).strip()
            if not reply: raise RuntimeError("The text model returned no spoken reply.")

            # One utterance gives Drama 3 the whole reply's prosody. Repeated
            # sentence requests can drift even with identical emotion labels.
            # The phrase ID remains 0 for wire/backward compatibility, and is
            # acknowledged only after the entire reply has actually played.
            self.history.add_phrase(0, reply)
            await self.send_json({"type":"phrase_start", "turn_id":turn_id, "seq":0, "text":reply})
            tts_started = time.perf_counter()
            reference_audio = bytearray()
            audio_bytes = audio_chunks = 0
            speech = spoken_input(reply, delivery, first_phrase=True)
            async def voice_notice(message):
                await self.send_json({"type":"voice_notice", "turn_id":turn_id,
                                      "seq":0, "message":message})
            async for chunk in self.providers.audio(speech, self.reference, on_notice=voice_notice):
                if "tts_first_chunk_ms" not in metrics:
                    metrics["tts_first_chunk_ms"] = round((time.perf_counter() - tts_started) * 1000)
                    metrics["speech_end_to_first_audio_ms"] = round((time.perf_counter() - started) * 1000)
                    await self.send_json({"type":"metrics", "turn_id":turn_id, **metrics})
                if self.reference is None: reference_audio.extend(chunk)
                audio_bytes += len(chunk)
                audio_chunks += 1
                await self.send_bytes(struct.pack("!II", turn_id, 0) + chunk)
            if self.reference is None and reference_audio:
                self.reference = synthetic_reference(bytes(reference_audio), reply)
            await self.send_json({"type":"phrase_done", "turn_id":turn_id, "seq":0,
                "audio_seconds":round(audio_bytes / (2 * OUTPUT_RATE), 3), "audio_chunks":audio_chunks,
                "generation_ms":round((time.perf_counter() - tts_started) * 1000)})
            self.generation_complete = True
            await self.send_json({"type":"response_done", "turn_id":turn_id, "text":reply})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Keep dialogue, character data and captured audio out of logs.
            logger.warning("Live turn failed (%s); inspect the local model service logs.", type(exc).__name__)
            self.reply_pending = False
            message = ("The reply could not finish with the chosen emotion and voice. Please try again; the delivery has not been changed."
                       if isinstance(exc, VoiceTakeIncomplete) else
                       "Voice generation failed. Check the LLM and Drama 3 server logs.")
            await self.send_json({"type":"error", "turn_id":turn_id, "message":message})

    async def close(self):
        if self.story_director_task:
            self.story_director_task.cancel();await asyncio.gather(self.story_director_task,return_exceptions=True)
        if self.story_task:
            self.story_task.cancel();await asyncio.gather(self.story_task,return_exceptions=True)
        if self.task:
            self.task.cancel(); await asyncio.gather(self.task, return_exceptions=True)
