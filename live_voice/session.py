import asyncio
import copy
import json
import logging
import math
import struct
import time

from .providers import OUTPUT_RATE, VoiceFailure, VoiceTakeIncomplete, VoiceServiceUnavailable, synthetic_reference
from .director import DirectorSettings
from .narrative import NarrativeState
from .recognition import PreparedRecognition
from .inspection import RequestInspection
from .voice import EMOTION_DIRECTIONS, spoken_text_clean
from .voice_progress import observe_voice_progress, voice_timing_metrics
from .turns import ReplyHistory
from .voice import Delivery, VoiceSettings, spoken_input
from .archive import retrieve, grounding_messages
from .collections import collection_grounding_messages
from .artwork_requests import (image_request, image_context, image_reference_ack,
    image_confirmation, image_offer, image_reference_question, image_capability_context)

logger = logging.getLogger(__name__)


def recognition_timings(values):
    """Only bounded listening counters can enter reply metrics."""
    if values is None: return {}
    if not isinstance(values,dict): raise ValueError('Use listening timing counters.')
    result = {'streaming_asr':True}
    for key,maximum in (('stream_finalize_ms',30000),('turn_inference_ms',10000)):
        value = values.get(key)
        if type(value) is int and 0 <= value <= maximum: result[key] = value
    reason = values.get('endpoint_reason')
    if reason in ('smart_turn','pause_limit','max_duration','endpoint_timeout'):
        result['endpoint_reason'] = reason
    probability = values.get('turn_probability')
    if type(probability) in (int,float) and math.isfinite(probability) and 0 <= probability <= 1:
        result['turn_probability'] = probability
    return result


class LiveSession:
    def __init__(self, providers, character, send_json, send_bytes, voice=None, trigger=None, director=None, narrative=None, clock=time.monotonic,
                 archive=None, archive_entries=(), archive_search=None):
        self.providers = providers
        self.character = character
        self.send_json = send_json
        self.send_bytes = send_bytes
        self.history = ReplyHistory()
        self.inspection = RequestInspection(send_json)
        self.prepared_recognition = PreparedRecognition(lambda pcm:self.providers.stt.transcribe(pcm))
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
        self.begin_lock = asyncio.Lock()
        self.closed = False
        self.input_active = False
        self.story_seen_revision = self.story.revision
        self.pending_beat_reply = None
        self.retryable_voice = None
        self.trigger = trigger
        self.trigger_active = False
        self.trigger_cleared = False
        self.trigger_evidence = ""
        self.archive = archive if archive and archive.enabled else None
        self.archive_entries = tuple(archive_entries) if self.archive else ()
        self.archive_search = archive_search if self.archive else None
        self.last_archive_context = None
        self.last_archive_matches = []
        self.pending_image_offer = []
        self.image_display = None

    def image_status(self, value):
        """Accept bounded current-request browser feedback without starting a reply."""
        if not isinstance(value,dict) or self.closed or self.image_display is None: return False
        turn, loaded, total = value.get('turn_id'), value.get('loaded_count'), value.get('total_count')
        status = value.get('status')
        if (type(turn) is not int or turn!=self.turn_id or turn!=self.image_display['turn_id']
                or type(loaded) is not int or type(total) is not int or not 0<=loaded<=total<=3
                or total!=self.image_display['total_count']
                or status not in ('loading','loaded','unavailable')
                or status=='loading' and (loaded!=0 or total==0)
                or status=='loaded' and loaded==0
                or status=='unavailable' and loaded!=0): return False
        previous = self.image_display['status']
        # A delayed loading/error notification cannot erase a successful load.
        if previous=='loaded' and (status!='loaded' or loaded<self.image_display['loaded_count']): return False
        self.image_display.update(status=status,loaded_count=loaded)
        return True

    @staticmethod
    def _image_keys(matches):
        return {(match.get('collection_id','qtm'),match.get('id')) for match in matches if isinstance(match,dict)}

    def _image_state_for(self,matches):
        state = self.image_display
        if state is None: return None
        keys = self._image_keys(matches)
        return state['status'] if keys and keys.issubset(state['keys']) else None

    async def story_state(self):
        if self.story.revision != self.story_seen_revision:
            self.story_seen_revision = self.story.revision
            self.pending_beat_reply = self.story.revision if not self.story.finished else None
        await self.send_json({"type":"story_state",**self.story.view()})

    async def reply_to_beat(self):
        revision = self.pending_beat_reply
        if (revision is None or self.closed or self.reply_pending or self.input_active
                or self.story.finished or revision != self.story.revision): return False
        beat = self.story.beat
        cue = ('[Private story cue: this scene has just changed. Initiate one natural in-character reply '
               'grounded in this moment; do not announce a beat or these instructions. '
               f'Present situation: {beat.note} Your immediate aim: {beat.goal}]')
        return await self.begin(text=cue, action=True, expected_revision=revision)

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

    async def story_boundary(self, speak=True):
        if not self.story.active or self.reply_pending or self.input_active or self.closed: return False
        moved=False
        if self.story.pending:
            action=self.story.pending;self.story.pending=None
            moved=self.apply_story_action(action)
        elif self.story.timeout(): moved=True
        if moved: await self.story_state()
        if speak: await self.reply_to_beat()
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
        if self.reply_pending or self.input_active:
            self.story.pending=action
            await self.story_state();return
        if self.apply_story_action(action):
            await self.story_state()
            await self.reply_to_beat()

    async def story_loop(self):
        ticks=0
        try:
            while True:
                await asyncio.sleep(1)
                moved=await self.story_boundary();ticks+=1
                if ticks%5==0 and not moved: await self.story_state()
                if (self.story.active and self.story.settings.mode=='director' and self.director.enabled
                        and not self.reply_pending and not self.input_active and not self.story.finished and self.story.paused_at is None
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
        with self.inspection.scope(self.turn_id, phase='between_turns'):
            await self._check_story(key)

    async def _check_story(self,key):
        try:
            plan=await asyncio.wait_for(self.providers.direct(self.character,self.history.context_messages(budget=3000),'',
                self.director,self.voice,self.trigger,self.trigger_active,self.last_plan,False,self.story.context(),
                **({'archive':self.last_archive_context} if self.archive else {})),
                getattr(self.providers,'director_timeout',6)+0.5)
            if (self.story.revision!=key[0] or self.reply_pending or self.input_active or self.story.paused_at is not None
                    or not self.director.enabled): return
            advanced,rejected=self.story.completion(plan.beat_done,plan.beat_evidence)
            if not advanced and not rejected:
                self.story.update_note(plan.intention)
            if rejected:await self.send_json({'type':'story_decision','accepted':False,'reason':rejected})
            await self.story_state()
            if advanced: await self.reply_to_beat()
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
        self.retryable_voice = None
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

    async def begin(self, pcm=None, text=None, endpoint_ms=0, action=False, expected_revision=None, recognition_metrics=None):
        async with self.begin_lock:
            if self.closed: return False
            if expected_revision is not None:
                if (self.pending_beat_reply != expected_revision or self.story.revision != expected_revision
                        or self.reply_pending or self.input_active or self.story.finished): return False
                # Reserve the revision before awaiting cancellation or transport.
                self.pending_beat_reply = None
            if recognition_metrics is None:
                return await self._begin(pcm, text, endpoint_ms, action, expected_revision)
            return await self._begin(pcm, text, endpoint_ms, action, expected_revision, recognition_metrics)

    def entry_is_current(self, revision):
        valid = not self.closed and (revision is None or (
            self.story.revision == revision and not self.story.finished and not self.input_active))
        if (not valid and revision is not None and not self.closed
                and self.story.revision == revision and not self.story.finished):
            # A participant started speaking while cancellation was in flight.
            # Preserve this cue for the next idle boundary or their own reply.
            self.pending_beat_reply = revision
        return valid

    async def _begin(self, pcm, text, endpoint_ms, action, expected_revision, recognition_metrics=None):
        listening = recognition_timings(recognition_metrics)
        if (self.story_director_task and self.story_director_task is not asyncio.current_task()
                and not self.story_director_task.done()):
            self.story_director_task.cancel();await asyncio.gather(self.story_director_task,return_exceptions=True)
        if not self.entry_is_current(expected_revision): return False
        await self.interrupt()
        if not self.entry_is_current(expected_revision): return False
        if expected_revision is None: self.input_active = False
        if text is not None: await self.prepared_recognition.clear()
        if not self.entry_is_current(expected_revision): return False
        # An incoming participant turn itself replies in the new beat. Launching
        # a separate cue here would recurse into begin and create two speakers.
        await self.story_boundary(speak=False)
        if not self.entry_is_current(expected_revision): return False
        self.turn_id += 1
        turn_id = self.turn_id
        self.reply_pending = True
        self.generation_complete = False
        self.turn_action = action
        self.turn_story_revision = self.story.revision
        self.started = time.perf_counter() - (endpoint_ms + listening.get('stream_finalize_ms',0)) / 1000
        self.task = asyncio.create_task(self.run(turn_id, pcm, text, endpoint_ms, action,
                                               **({'recognition_metrics':listening} if listening else {})))
        return True

    def prepare_recognition(self, pcm):
        """Prepare a pause without publishing a transcript or starting a reply."""
        if self.reply_pending: return False
        return self.prepared_recognition.prepare(pcm)

    async def run(self, turn_id, pcm, text, endpoint_ms, action=False, recognition_metrics=None):
        with self.inspection.scope(turn_id):
            await self.run_turn(turn_id, pcm, text, endpoint_ms, action,
                                **({'recognition_metrics':recognition_metrics} if recognition_metrics else {}))

    async def run_turn(self, turn_id, pcm, text, endpoint_ms, action=False, recognition_metrics=None):
        started = self.started
        metrics = {"endpoint_ms":endpoint_ms,**recognition_timings(recognition_metrics)}
        try:
            await self.send_json({"type":"turn_start", "turn_id":turn_id})
            now = time.perf_counter()
            if text is None:
                text = await self.prepared_recognition.transcribe(pcm)
                metrics["recognition_prepared"] = self.prepared_recognition.last_reused
            metrics["stt_ms"] = metrics.get('stream_finalize_ms',round((time.perf_counter() - now) * 1000))
            if not text or not text.strip():
                self.reply_pending = False
                await self.send_json({"type":"empty_transcript", "turn_id":turn_id}); return
            # Register finalized participant words before planning can be stopped.
            # The director gets prior context plus latest separately, while the
            # actor later gets the current utterance exactly once from history.
            director_context = self.history.context_messages(latest=text, budget=3000 if self.story.active else 6000)
            trigger_context = (self.history.context_messages(latest=text, budget=6000)
                               if self.trigger and not self.director.enabled else None)
            self.history.begin(turn_id, text, source='stage' if action else 'participant')
            if self.pending_beat_reply == self.story.revision: self.pending_beat_reply = None
            transcript = {"type":"transcript", "turn_id":turn_id, "text":text, "source":"action" if action else "speech"}
            if action and text.startswith('[Private story cue:') and self.story.beat:
                transcript['display_text'] = 'Story cue: ' + self.story.beat.title
            elif action and self.trigger:
                transcript['display_text'] = self.trigger.condition
            await self.send_json(transcript)
            archive_context = None
            if self.archive:
                scope = self.archive.scope
                requested_image = image_request(text) if not action else None
                offered = copy.deepcopy(self.pending_image_offer)
                self.pending_image_offer = []
                confirmed = not action and not requested_image and offered and image_confirmation(text)
                if confirmed: requested_image = {'query':'','followup':True}
                reference_followup = (not action and bool(self.last_archive_matches)
                    and (image_reference_ack(text) or image_confirmation(text) or image_reference_question(text)))
                search_text = requested_image['query'] if requested_image and not requested_image['followup'] else text
                search_history = () if requested_image else director_context
                reuse = bool(requested_image and requested_image['followup'] or reference_followup)
                previous_matches = offered if confirmed else self.last_archive_matches
                if scope == 'library':
                    if self.archive_search is None: raise ValueError('The local archive snapshot is unavailable.')
                    matches = (copy.deepcopy(previous_matches) if reuse
                               else await asyncio.to_thread(self.archive_search.search, search_text, search_history))
                    count = self.archive_search.count
                else:
                    matches = (copy.deepcopy(previous_matches) if reuse
                               else retrieve(self.archive_entries, search_text, search_history))
                    count = len(self.archive_entries)
                archive_context = (collection_grounding_messages(matches) if self.archive.collection_ids
                                   else grounding_messages(matches, scope=scope))
                if self.archive.collection_ids:
                    archive_context[0] = {**archive_context[0], 'content':archive_context[0]['content']+'\n\n'+
                        image_capability_context(matches,state=self._image_state_for(matches))}
                if requested_image:
                    archive_context.append({'role':'system','content':image_context(requested_image,matches)})
                self.last_archive_context = archive_context
                source_event = {'type':'archive_sources','turn_id':turn_id,'scope':scope,
                                'selected_count':count,'matches':matches}
                if self.archive.collection_ids: source_event['collection_ids'] = self.archive.collection_ids
                if requested_image: source_event['image_request'] = True
                await self.send_json(source_event)
                if requested_image:
                    self.image_display = {'turn_id':turn_id,'keys':self._image_keys(matches),
                        'total_count':sum(bool(match.get('image_url')) for match in matches[:3]),
                        'loaded_count':0,'status':'sent'}
                # Only published references become the next visual referent.
                # A brief acknowledgement does not replace the artwork topic.
                if matches or requested_image or not image_reference_ack(text):
                    self.last_archive_matches = copy.deepcopy(list(matches)[:3])
            plan = None
            if self.director.enabled:
                check_started = time.perf_counter()
                try:
                    plan = await asyncio.wait_for(self.providers.direct(
                        self.character,director_context,text,self.director,self.voice,
                        self.trigger,self.trigger_active,self.last_plan,action,self.story.context(),
                        **({'archive':archive_context} if self.archive else {})),
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
                        self.pending_beat_reply = None  # This actor reply serves the newly entered beat.
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
                        self.providers.assess_trigger(self.character,self.trigger,trigger_context,text,self.trigger_active),4.5)
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
            answer = []
            llm_started = time.perf_counter()
            async for delta in self.providers.text(target, self.history.prompt_messages(),
                    **({'archive':archive_context} if self.archive else {})):
                if not answer: metrics["llm_first_token_ms"] = round((time.perf_counter() - llm_started) * 1000)
                answer.append(delta)
                await self.send_json({"type":"text_delta", "turn_id":turn_id, "text":delta})
            metrics["llm_total_ms"] = round((time.perf_counter() - llm_started) * 1000)
            reply = spoken_text_clean("".join(answer)).strip()
            if not reply: raise RuntimeError("The text model returned no spoken reply.")
            await self.send_json({'type':'text_done','turn_id':turn_id,'text':reply})
            if self.archive:
                offered_keys = image_offer(reply,matches)
                self.pending_image_offer = (copy.deepcopy([match for match in matches
                    if (match.get('collection_id','qtm'),match.get('id')) in offered_keys]) if offered_keys else [])

            # One utterance gives Drama 3 the whole reply's prosody. Repeated
            # sentence requests can drift even with identical emotion labels.
            # The phrase ID remains 0 for wire/backward compatibility, and is
            # acknowledged only after the entire reply has actually played.
            self.history.add_phrase(0, reply)
            speech = spoken_input(reply, delivery, first_phrase=True)
            self.retryable_voice = {'turn_id':turn_id, 'revision':self.story.revision, 'reply':reply,
                                    'speech':speech, 'reference':tuple(self.reference) if self.reference else None,
                                    'audio_started':False}
            await self.speak_reply(self.retryable_voice, metrics, started)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.turn_error(turn_id, exc)

    async def speak_reply(self, take, metrics, started):
        turn_id, reply = take['turn_id'], take['reply']
        await self.send_json({"type":"phrase_start", "turn_id":turn_id, "seq":0, "text":reply})
        tts_started = time.perf_counter()
        reference_audio = bytearray()
        audio_bytes = audio_chunks = 0
        async def voice_notice(message):
            await self.send_json({"type":"voice_notice", "turn_id":turn_id, "seq":0, "message":message})
        async def voice_progress(event):
            await self.send_json({'type':'voice_progress','turn_id':turn_id,**event})
        with observe_voice_progress(voice_progress):
            async for chunk in self.providers.audio(take['speech'], take['reference'], on_notice=voice_notice):
                take['audio_started'] = True
                if "tts_first_chunk_ms" not in metrics:
                    metrics.update(voice_timing_metrics(getattr(self.providers,'last_voice_metrics',None)))
                    metrics["tts_first_chunk_ms"] = round((time.perf_counter() - tts_started) * 1000)
                    metrics["speech_end_to_first_audio_ms"] = round((time.perf_counter() - started) * 1000)
                    await self.send_json({"type":"metrics", "turn_id":turn_id, **metrics})
                if take['reference'] is None: reference_audio.extend(chunk)
                audio_bytes += len(chunk); audio_chunks += 1
                await self.send_bytes(struct.pack("!II", turn_id, 0) + chunk)
        if take['reference'] is None and reference_audio:
            self.reference = synthetic_reference(bytes(reference_audio), reply)
            if self.reference is None:
                await voice_notice('Automatic voice will wait for a complete reply lasting 1–30 seconds before keeping a voice reference. This reply has not been cropped.')
        await self.send_json({"type":"phrase_done", "turn_id":turn_id, "seq":0,
            "audio_seconds":round(audio_bytes / (2 * OUTPUT_RATE), 3), "audio_chunks":audio_chunks,
            "generation_ms":round((time.perf_counter() - tts_started) * 1000)})
        self.generation_complete = True
        self.retryable_voice = None
        await self.send_json({"type":"response_done", "turn_id":turn_id, "text":reply})

    async def turn_error(self, turn_id, exc):
        # Keep dialogue, character data and captured audio out of logs.
        voice_failure = exc if isinstance(exc, VoiceFailure) else getattr(exc, 'voice_failure', None)
        if not isinstance(voice_failure, VoiceFailure): voice_failure = None
        diagnostic = voice_failure.diagnostic() if voice_failure else None
        if diagnostic:
            logger.warning('Live voice failed: %s', json.dumps(diagnostic, sort_keys=True))
            timings = voice_timing_metrics(getattr(self.providers,'last_voice_metrics',None))
            if timings: await self.send_json({'type':'metrics','turn_id':turn_id,'voice_failed':True,**timings})
        else:
            logger.warning("Live turn failed (%s); inspect the local model service logs.", type(exc).__name__)
        self.reply_pending = False
        self.generation_complete = False
        take = self.retryable_voice
        can_retry = bool(take and take['turn_id'] == turn_id and not take['audio_started']
                         and self.history.current == turn_id and take['revision'] == self.story.revision)
        if not can_retry: self.retryable_voice = None
        if isinstance(exc, VoiceServiceUnavailable): message = str(exc) + ' Details: ' + exc.safe_summary() + '.'
        elif isinstance(exc, VoiceTakeIncomplete):
            message = ("The voice take did not finish: " + exc.safe_summary() +
                       ". The delivery has not been changed. Use Retry voice to keep this reply's words, emotion and voice settings.")
        elif voice_failure and voice_failure.reason == 'verification_unavailable':
            message = 'The voice was generated, but the speech check could not finish. Check speech recognition before retrying; no unchecked audio was played.'
        elif voice_failure:
            message = "The voice request failed: " + voice_failure.safe_summary() + ". Check the Drama 3 service before retrying."
        elif can_retry:
            message = "The voice service could not generate this reply. Check the Drama 3 log; you can retry its voice without changing the dialogue."
        else:
            message = "The reply could not be generated. Check the actor and director service logs, then try again."
        event = {"type":"error", "turn_id":turn_id, "message":message, "can_retry_voice":can_retry}
        if diagnostic: event['voice_diagnostic'] = diagnostic
        await self.send_json(event)

    async def retry_voice(self, turn_id):
        async with self.begin_lock:
            take = self.retryable_voice
            def ready():
                return (not self.closed and not self.reply_pending and not self.input_active and take
                        and take is self.retryable_voice and turn_id == self.turn_id and take['turn_id'] == turn_id
                        and take['revision'] == self.story.revision and self.history.current == turn_id)
            if not ready(): return False
            if (self.story_director_task and self.story_director_task is not asyncio.current_task()
                    and not self.story_director_task.done()):
                self.story_director_task.cancel(); await asyncio.gather(self.story_director_task, return_exceptions=True)
            if not ready(): return False
            await self.prepared_recognition.clear()
            if not ready(): return False
            self.reply_pending = True
            self.generation_complete = False
            self.task = asyncio.create_task(self.run_voice_retry(take))
            return True

    async def run_voice_retry(self, take):
        turn_id = take['turn_id']
        with self.inspection.scope(turn_id, phase='voice_retry'):
            try:
                await self.send_json({'type':'turn_start', 'turn_id':turn_id, 'voice_retry':True})
                metrics = {'endpoint_ms':0, 'stt_ms':0, 'director_ms':0, 'llm_total_ms':0, 'voice_retry':True}
                await self.speak_reply(take, metrics, time.perf_counter())
            except asyncio.CancelledError: raise
            except Exception as exc: await self.turn_error(turn_id, exc)

    async def close(self):
        self.closed = True
        self.pending_beat_reply = None
        self.retryable_voice = None
        self.archive_entries = ()
        self.last_archive_context = None
        self.last_archive_matches = []
        self.pending_image_offer = []
        self.image_display = None
        if self.story_director_task:
            self.story_director_task.cancel();await asyncio.gather(self.story_director_task,return_exceptions=True)
        if self.story_task:
            self.story_task.cancel();await asyncio.gather(self.story_task,return_exceptions=True)
        if self.task:
            self.task.cancel(); await asyncio.gather(self.task, return_exceptions=True)
        if self.archive_search is not None:
            await asyncio.to_thread(self.archive_search.close)
            self.archive_search = None
        await self.prepared_recognition.close()
        self.inspection.close()
