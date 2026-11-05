"""Automatic interruption crosses the real WebSocket/session boundary, without a GPU."""
import asyncio
from copy import deepcopy
import importlib
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from live_voice.app import create_app
from live_voice.archive import ArchiveStore
from live_voice.session import LiveSession
from live_voice.turns import TurnDetector
from studio.characters import example
from test_live_voice import FakeProviders, frames
from test_listening_app import StreamingClient


def receive(socket):
    async def bounded(): return await asyncio.wait_for(socket._send_rx.receive(), 3)
    message = socket.portal.call(bounded)
    if message['type'] == 'websocket.close':
        raise WebSocketDisconnect(message.get('code', 1000))
    if message.get('bytes') is not None:
        return {'type':'pcm', 'bytes':message['bytes']}
    return json.loads(message['text'])


def until(socket, kind):
    events = []
    for _ in range(100):
        event = receive(socket)
        events.append(event)
        if event['type'] == kind: return events
    raise AssertionError(f'{kind} did not arrive within the bounded event list.')


class BargeProviders(FakeProviders):
    def __init__(self, *, block_audio=False, block_actor=False):
        super().__init__()
        self.histories = []
        self.recognized = []
        self.block_audio = block_audio
        self.block_actor = block_actor
        self.audio_started = threading.Event()
        self.audio_cancelled = threading.Event()
        self.actor_started = threading.Event()
        self.actor_cancelled = threading.Event()
        async def transcribe(pcm):
            self.recognized.append(pcm)
            return 'Please stop. I need to ask something else.'
        self.stt.transcribe = transcribe

    async def text(self, character, history, **kwargs):
        self.histories.append(deepcopy(history))
        self.characters.append(character)
        first = len(self.histories) == 1
        self.actor_started.set()
        yield 'This is the first reply.' if first else 'I heard your new question.'
        if self.block_actor and first:
            try: await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.actor_cancelled.set()
                raise

    async def audio(self, text, reference=None, on_notice=None):
        self.spoken.append(text)
        self.references.append(reference)
        self.audio_started.set()
        if self.block_audio and len(self.spoken) == 1:
            try: await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.audio_cancelled.set()
                raise
        yield b'\x01\x00' * 100


class BargeInTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {'LIVE_LISTENING_MODE':'baseline', 'LIVE_PROFILE':'shared'})
        self.environment.start()
        self.directory = tempfile.TemporaryDirectory()
        self.sessions = []
        def capture(*args, **kwargs):
            session = LiveSession(*args, **kwargs)
            self.sessions.append(session)
            return session
        self.capture = patch('live_voice.app.LiveSession', side_effect=capture)
        self.capture.start()

    def tearDown(self):
        self.capture.stop()
        self.directory.cleanup()
        self.environment.stop()

    def app(self, providers, listening=None):
        return create_app(providers=providers, listening_client=listening,
            vad_factory=lambda:TurnDetector(lambda frame:frame[:2] == b'\x01\x00'),
            archive_store=ArchiveStore(Path(self.directory.name)))

    def start(self, socket, *, interruptions=True, omit=False, **settings):
        config = {'type':'start', 'character':example().model_dump(), 'director':{'enabled':False}, **settings}
        if not omit: config['interruptions'] = interruptions
        socket.send_json(config)
        ready = receive(socket)
        self.assertEqual(ready['type'], 'ready')
        self.assertIs(ready['interruptions'], False if omit else interruptions)
        return ready

    def typed_reply(self, socket, text='Tell me something.', *, done=True):
        socket.send_json({'type':'text', 'text':text})
        return until(socket, 'response_done' if done else 'text_delta')

    def barge(self, socket, turn):
        socket.send_json({'type':'barge_in', 'turn_id':turn})
        return until(socket, 'barge_in_ready')

    def barrier(self, socket):
        socket.send_json({'type':'inspect', 'enabled':True})
        return until(socket, 'model_inspection')

    def microphone_turn(self, socket):
        # The first two frames must survive even though neither establishes VAD.
        socket.send_bytes(frames(True, 2))
        self.barrier(socket)
        socket.send_bytes(frames(True, 6))
        until(socket, 'speech_start')
        for _ in range(4): socket.send_bytes(frames(False, 5))
        return until(socket, 'response_done')

    def test_old_clients_default_off_and_pending_raw_microphone_is_still_discarded(self):
        providers = BargeProviders()
        with TestClient(self.app(providers)) as client:
            self.assertIs(client.get('/api/status').json()['voice_interruptions'], True)
            with client.websocket_connect('/api/live', headers={'origin':'http://testserver'}) as socket:
                ready = self.start(socket, omit=True)
                self.assertIn('Stop reply', ready['turn_taking'])
                self.typed_reply(socket)
                rejected = self.barge(socket, 1)
                self.assertEqual(rejected[-1], {'type':'barge_in_ready', 'turn_id':1, 'accepted':False})
                self.assertFalse(any(event['type'] == 'interrupted' for event in rejected))
                socket.send_bytes(frames(True, 10))
                for _ in range(4): socket.send_bytes(frames(False, 5))
                self.barrier(socket)
                self.assertEqual(providers.recognized, [])
                self.assertTrue(self.sessions[0].reply_pending)
                socket.send_json({'type':'stop'})

    def test_live_toggle_is_acknowledged_and_can_disable_again(self):
        providers = BargeProviders()
        with TestClient(self.app(providers)) as client:
            with client.websocket_connect('/api/live', headers={'origin':'http://testserver'}) as socket:
                self.start(socket, omit=True)
                self.typed_reply(socket)
                for enabled in (True, False, True):
                    socket.send_json({'type':'interruptions', 'enabled':enabled})
                    self.assertEqual(receive(socket), {'type':'interruptions_ack', 'enabled':enabled})
                    if not enabled: self.assertFalse(self.barge(socket, 1)[-1]['accepted'])
                self.assertTrue(self.barge(socket, 1)[-1]['accepted'])
                socket.send_json({'type':'stop'})

    def test_active_whole_voice_is_cancelled_and_first_buffered_syllable_enters_the_next_turn(self):
        providers = BargeProviders(block_audio=True)
        with TestClient(self.app(providers)) as client:
            with client.websocket_connect('/api/live', headers={'origin':'http://testserver'}) as socket:
                ready = self.start(socket)
                self.assertIn('Speak to interrupt', ready['turn_taking'])
                socket.send_json({'type':'text', 'text':'Please tell me a long story.'})
                first = until(socket, 'phrase_start')
                self.assertTrue(providers.audio_started.wait(1))
                interrupted = self.barge(socket, 1)
                self.assertTrue(interrupted[-1]['accepted'])
                self.assertTrue(providers.audio_cancelled.wait(1))
                self.assertEqual([event['heard'] for event in interrupted if event['type'] == 'interrupted'], [''])
                self.assertFalse(any(event['type'] == 'pcm' for event in first + interrupted))
                self.assertTrue(self.sessions[0].input_active)
                # Late browser acknowledgements cannot claim the interrupted reply.
                socket.send_json({'type':'played', 'turn_id':1, 'seq':0})
                second = self.microphone_turn(socket)
                self.assertEqual([event['turn_id'] for event in second if event['type'] == 'turn_start'], [2])
                self.assertEqual(providers.recognized, [frames(True, 8) + frames(False, 4)])
                self.assertEqual(providers.histories[1], [
                    {'role':'user', 'content':'Please tell me a long story.'},
                    {'role':'user', 'content':'Please stop. I need to ask something else.'}])
                self.assertEqual(self.sessions[0].history.archive[0]['assistant'], '')
                socket.send_json({'type':'stop'})

    def test_actor_generation_can_be_cancelled_before_any_voice_take_begins(self):
        providers = BargeProviders(block_actor=True)
        with TestClient(self.app(providers)) as client:
            with client.websocket_connect('/api/live', headers={'origin':'http://testserver'}) as socket:
                self.start(socket)
                self.typed_reply(socket, done=False)
                self.assertTrue(self.barge(socket, 1)[-1]['accepted'])
                self.assertTrue(providers.actor_cancelled.wait(1))
                self.assertEqual(providers.spoken, [])
                self.assertFalse(self.sessions[0].reply_pending)
                self.assertTrue(self.sessions[0].input_active)
                socket.send_json({'type':'stop'})

    def test_playback_pending_is_interruptible_without_remembering_unheard_words(self):
        providers = BargeProviders()
        with TestClient(self.app(providers)) as client:
            with client.websocket_connect('/api/live', headers={'origin':'http://testserver'}) as socket:
                self.start(socket)
                first = self.typed_reply(socket)
                self.assertTrue(any(event['type'] == 'pcm' for event in first))
                self.assertTrue(self.sessions[0].generation_complete)
                self.assertTrue(self.barge(socket, 1)[-1]['accepted'])
                self.assertEqual(self.sessions[0].history.messages[-1]['content'], '')
                self.assertIsNone(self.sessions[0].history.current)
                socket.send_json({'type':'stop'})

    def test_played_whole_reply_is_preserved_and_late_idle_barge_removes_echo_cooldown(self):
        providers = BargeProviders()
        app_module = importlib.import_module('live_voice.app')
        with patch.object(app_module, 'time', SimpleNamespace(monotonic=lambda:100.0)):
            with TestClient(self.app(providers)) as client:
                with client.websocket_connect('/api/live', headers={'origin':'http://testserver'}) as socket:
                    self.start(socket)
                    self.typed_reply(socket)
                    socket.send_json({'type':'played', 'turn_id':1, 'seq':0})
                    socket.send_json({'type':'playback_done', 'turn_id':1})
                    self.barrier(socket)
                    self.assertFalse(self.sessions[0].reply_pending)
                    accepted = self.barge(socket, 1)
                    self.assertTrue(accepted[-1]['accepted'])
                    self.assertFalse(any(event['type'] == 'interrupted' for event in accepted))
                    self.microphone_turn(socket)
                    self.assertEqual(providers.histories[1][1],
                        {'role':'assistant', 'content':'This is the first reply.'})
                    self.assertEqual(len(providers.recognized), 1)
                    socket.send_json({'type':'stop'})

    def test_duplicate_and_stale_requests_do_not_touch_the_replacement_reply(self):
        providers = BargeProviders()
        with TestClient(self.app(providers)) as client:
            with client.websocket_connect('/api/live', headers={'origin':'http://testserver'}) as socket:
                self.start(socket)
                self.typed_reply(socket)
                self.assertTrue(self.barge(socket, 1)[-1]['accepted'])
                self.assertFalse(self.barge(socket, 1)[-1]['accepted'])
                socket.send_json({'type':'mute'})
                self.assertFalse(self.barge(socket, 1)[-1]['accepted'])
                self.typed_reply(socket, 'This is a different question.')
                for stale in (1, 3, 0xffffffff):
                    events = self.barge(socket, stale)
                    self.assertFalse(events[-1]['accepted'])
                    self.assertFalse(any(event['type'] == 'interrupted' for event in events))
                    self.assertEqual(self.sessions[0].turn_id, 2)
                    self.assertTrue(self.sessions[0].reply_pending)
                self.assertTrue(self.barge(socket, 2)[-1]['accepted'])
                socket.send_json({'type':'stop'})

    def test_input_already_in_progress_cannot_be_reset_by_a_late_barge_request(self):
        providers = BargeProviders()
        with TestClient(self.app(providers)) as client:
            with client.websocket_connect('/api/live', headers={'origin':'http://testserver'}) as socket:
                self.start(socket)
                self.typed_reply(socket)
                socket.send_json({'type':'interrupt'})
                until(socket, 'interrupted')
                # A deliberate new typed reply removes dependence on wall time.
                self.typed_reply(socket, 'A second reply.')
                self.assertTrue(self.barge(socket, 2)[-1]['accepted'])
                socket.send_bytes(frames(True, 3))
                until(socket, 'speech_start')
                before = deepcopy(self.sessions[0].history.messages)
                self.assertFalse(self.barge(socket, 2)[-1]['accepted'])
                self.assertTrue(self.sessions[0].input_active)
                self.assertEqual(self.sessions[0].history.messages, before)
                socket.send_json({'type':'stop'})

    def test_newer_turn_started_under_begin_lock_makes_waiting_old_request_stale(self):
        providers = BargeProviders()
        with TestClient(self.app(providers)) as client:
            with client.websocket_connect('/api/live', headers={'origin':'http://testserver'}) as socket:
                self.start(socket)
                self.typed_reply(socket)
                session = self.sessions[0]
                async def queue_new_turn():
                    await session.begin_lock.acquire()
                    asyncio.create_task(session.begin(text='A concurrently finalized utterance.'))
                    await asyncio.sleep(0)
                client.portal.call(queue_new_turn)
                socket.send_json({'type':'barge_in', 'turn_id':1})
                client.portal.call(session.begin_lock.release)
                events = until(socket, 'barge_in_ready')
                self.assertFalse(events[-1]['accepted'])
                if not any(event['type'] == 'response_done' and event['turn_id'] == 2 for event in events):
                    events += until(socket, 'response_done')
                self.assertEqual(session.turn_id, 2)
                self.assertTrue(session.reply_pending)
                self.assertEqual(providers.histories[1][-1]['content'], 'A concurrently finalized utterance.')
                socket.send_json({'type':'stop'})

    def test_reserved_floor_blocks_queued_story_and_cancels_background_director(self):
        providers = BargeProviders()
        narrative = {'enabled':True, 'mode':'manual', 'beats':[
            {'title':'First', 'goal':'Ask a question.'}, {'title':'Second', 'goal':'Listen to the answer.'}]}
        cancelled = threading.Event()
        with TestClient(self.app(providers)) as client:
            with client.websocket_connect('/api/live', headers={'origin':'http://testserver'}) as socket:
                self.start(socket, narrative=narrative)
                self.typed_reply(socket)
                socket.send_json({'type':'story', 'control':{'action':'next'}})
                until(socket, 'story_state')
                session = self.sessions[0]
                async def install_background():
                    async def wait():
                        try: await asyncio.Event().wait()
                        finally: cancelled.set()
                    session.story_director_task = asyncio.create_task(wait())
                    await asyncio.sleep(0)
                client.portal.call(install_background)
                self.assertTrue(self.barge(socket, 1)[-1]['accepted'])
                self.assertTrue(cancelled.wait(1))
                self.assertFalse(client.portal.call(session.story_boundary))
                self.assertEqual(session.story.index, 0)
                self.assertEqual(session.story.pending, 'next')
                socket.send_bytes(frames(True, 2))
                self.barrier(socket)
                self.assertTrue(session.input_active)
                self.assertFalse(client.portal.call(session.story_boundary))
                socket.send_json({'type':'stop'})

    def test_reservation_expires_without_any_audio_and_cannot_clear_later_real_speech(self):
        providers = BargeProviders()
        with TestClient(self.app(providers)) as client:
            with client.websocket_connect('/api/live', headers={'origin':'http://testserver'}) as socket:
                self.start(socket)
                self.typed_reply(socket)
                self.assertTrue(self.barge(socket, 1)[-1]['accepted'])
                self.assertTrue(self.sessions[0].input_active)
                async def expire(): await asyncio.sleep(2.05)
                client.portal.call(expire)
                self.assertFalse(self.sessions[0].input_active)
                self.typed_reply(socket, 'Now start another answer.')
                self.assertTrue(self.barge(socket, 2)[-1]['accepted'])
                socket.send_bytes(frames(True, 3))
                until(socket, 'speech_start')
                client.portal.call(expire)
                self.assertTrue(self.sessions[0].input_active)
                socket.send_json({'type':'stop'})

    def test_old_reservation_timer_cannot_release_newer_turns_input(self):
        providers = BargeProviders()
        with TestClient(self.app(providers)) as client:
            with client.websocket_connect('/api/live', headers={'origin':'http://testserver'}) as socket:
                self.start(socket)
                self.typed_reply(socket)
                self.assertTrue(self.barge(socket, 1)[-1]['accepted'])
                socket.send_json({'type':'mute'})
                self.typed_reply(socket, 'A newer turn.')
                self.assertTrue(self.barge(socket, 2)[-1]['accepted'])
                socket.send_bytes(frames(True, 3))
                until(socket, 'speech_start')
                async def expire(): await asyncio.sleep(2.05)
                client.portal.call(expire)
                self.assertTrue(self.sessions[0].input_active)
                self.assertEqual(self.sessions[0].turn_id, 2)
                socket.send_json({'type':'stop'})

    def test_manual_stop_retains_cooldown_and_does_not_reserve_input(self):
        providers = BargeProviders()
        app_module = importlib.import_module('live_voice.app')
        with patch.object(app_module, 'time', SimpleNamespace(monotonic=lambda:100.0)):
            with TestClient(self.app(providers)) as client:
                with client.websocket_connect('/api/live', headers={'origin':'http://testserver'}) as socket:
                    self.start(socket)
                    self.typed_reply(socket)
                    socket.send_json({'type':'interrupt'})
                    until(socket, 'interrupted')
                    self.barrier(socket)
                    self.assertFalse(self.sessions[0].input_active)
                    socket.send_bytes(frames(True, 10))
                    for _ in range(4): socket.send_bytes(frames(False, 5))
                    events = self.barrier(socket)
                    self.assertFalse(any(event['type'] == 'speech_start' for event in events))
                    self.assertEqual(providers.recognized, [])
                    socket.send_json({'type':'stop'})

    def test_malformed_boolean_and_turn_ids_close_cleanly_and_release_call_lock(self):
        commands = ([{'type':'interruptions', 'enabled':value} for value in (None, 1, 'true')]
                    + [{'type':'barge_in', 'turn_id':value} for value in (None, True, 0, -1, 2**32, 1.0, '1')])
        providers = BargeProviders()
        with TestClient(self.app(providers)) as client:
            for command in commands:
                with self.subTest(command=command):
                    with client.websocket_connect('/api/live', headers={'origin':'http://testserver'}) as socket:
                        self.start(socket)
                        socket.send_json(command)
                        self.assertEqual(receive(socket)['type'], 'error')
                        with self.assertRaises(WebSocketDisconnect): receive(socket)
            for value in (None, 1, 'true'):
                with self.subTest(start=value):
                    with client.websocket_connect('/api/live', headers={'origin':'http://testserver'}) as socket:
                        socket.send_json({'type':'start', 'character':example().model_dump(), 'interruptions':value})
                        self.assertEqual(receive(socket)['type'], 'error')
                        with self.assertRaises(WebSocketDisconnect): receive(socket)


class StreamingBargeInTests(unittest.TestCase):
    def test_streaming_listener_reset_preserves_preroll_and_reservation_until_real_onset(self):
        case = BargeInTests()
        case.setUp()
        try:
            with patch.dict(os.environ, {'LIVE_LISTENING_MODE':'nemotron-smart-turn', 'LIVE_PROFILE':'quality-3gpu'}):
                providers = BargeProviders()
                listening = StreamingClient()
                with TestClient(case.app(providers, listening)) as client:
                    with client.websocket_connect('/api/live', headers={'origin':'http://testserver'}) as socket:
                        case.start(socket)
                        case.typed_reply(socket)
                        self.assertTrue(case.barge(socket, 1)[-1]['accepted'])
                        session = case.sessions[0]
                        socket.send_bytes(frames(True, 2))
                        case.barrier(socket)
                        self.assertTrue(session.input_active)
                        self.assertEqual(listening.streams, [])
                        socket.send_bytes(frames(True, 6))
                        until(socket, 'speech_start')
                        for _ in range(3): socket.send_bytes(frames(False, 5))
                        events = until(socket, 'response_done')
                        self.assertEqual([event['turn_id'] for event in events if event['type'] == 'turn_start'], [2])
                        self.assertTrue(listening.opened.wait(1))
                        self.assertEqual(b''.join(listening.streams[0].sent), frames(True, 8) + frames(False, 15))
                        self.assertEqual(providers.recognized, [])
                        self.assertEqual(providers.histories[1][-1]['content'], listening.final_text)
                        socket.send_json({'type':'stop'})
        finally: case.tearDown()


if __name__ == '__main__': unittest.main()
