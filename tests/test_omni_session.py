"""Native streaming keeps director/RAG context and only remembers heard completion."""
import asyncio
import copy
import json
import struct
import tempfile
import unittest

from live_voice.archive import ArchiveSettings
from live_voice.collections import CollectionDatabase
from live_voice.director import DirectorSettings
from live_voice.session import LiveSession
from live_voice.voice import VoiceSettings
from studio.characters import example
from test_archive_session import ArchiveProviders
from test_artwork_session import artwork


class StreamingProviders(ArchiveProviders):
    engine = 'qwen-omni'
    output_rate = 24000
    verify_voice = False
    def __init__(self):
        super().__init__(); self.native=[]; self.gate=None; self.cancelled=False; self.failure=None
    async def stream_response(self,character,history,**kwargs):
        self.native.append({'character':character.model_dump(),'history':copy.deepcopy(history),'kwargs':copy.deepcopy(kwargs)})
        try:
            yield {'type':'text','text':'I can see '}
            yield {'type':'audio','pcm':b'\x01\x00'*2400,'sample_rate':24000}
            if self.gate is not None: await self.gate.wait()
            if self.failure == 'transport': raise RuntimeError('Synthetic disconnected stream')
            if self.failure == 'rate':
                yield {'type':'audio','pcm':b'\x01\x00'*100,'sample_rate':44100}; return
            yield {'type':'text','text':'the bridge.'}
            yield {'type':'audio','pcm':b'\x02\x00'*2400,'sample_rate':24000}
            if self.failure != 'missing_done': yield {'type':'done','metrics':{}}
        finally: self.cancelled=True


class OmniSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.providers=StreamingProviders(); self.events=[]; self.audio=[]; self.first_audio=asyncio.Event()
        self.sessions=[]; self.stores=[]
    async def asyncTearDown(self):
        for session in self.sessions: await session.close()
        for store,folder in self.stores: store.close(); folder.cleanup()
    def session(self,archive=False):
        async def send(value): self.events.append(copy.deepcopy(value))
        async def audio(value): self.audio.append(value); self.first_audio.set()
        options={}
        if archive:
            folder=tempfile.TemporaryDirectory(); store=CollectionDatabase(folder.name); self.stores.append((store,folder))
            store.import_records([artwork(1,'Claude Monet','The Bridge')],collection_id='national-gallery-of-art',label='NGA',source_kind='nga')
            options={'archive':ArchiveSettings(enabled=True,scope='library',collection_ids=['national-gallery-of-art']),
                     'archive_search':store.open_snapshot(['national-gallery-of-art'])}
        session=LiveSession(self.providers,example(),send,audio,voice=VoiceSettings(preset='male'),
                            director=DirectorSettings(enabled=True),**options)
        self.sessions.append(session); return session

    async def test_pcm_reaches_browser_before_completion_and_requires_real_playback_ack(self):
        self.providers.gate=asyncio.Event(); session=self.session()
        await session.begin(text='Hello.')
        await asyncio.wait_for(self.first_audio.wait(),1)
        self.assertFalse(session.task.done()); self.assertFalse(session.generation_complete)
        self.assertFalse(session.history.acknowledge(session.turn_id,0))
        self.assertFalse(any(event['type']=='response_done' for event in self.events))
        self.providers.gate.set(); await asyncio.wait_for(session.task,1)
        self.assertEqual(struct.unpack('!II',self.audio[0][:8]),(session.turn_id,0))
        self.assertEqual(len(self.audio),2)
        self.assertEqual(session.history.messages[-1]['content'],'')
        self.assertTrue(session.history.acknowledge(session.turn_id,0)); self.assertTrue(session.playback_done(session.turn_id))
        self.assertEqual(session.history.messages[-1]['content'],'I can see the bridge.')
        self.assertEqual(self.providers.native[0]['kwargs']['voice'],'Ethan')
        self.assertEqual((self.providers.actor,self.providers.voices),([],[]))
        metrics=[event for event in self.events if event['type']=='metrics'][-1]
        self.assertEqual(metrics['engine'],'qwen-omni'); self.assertFalse(metrics['voice_verification'])
        self.assertLessEqual(metrics['first_audio_ms'],metrics['complete_ms'])

    async def test_native_microphone_recording_and_same_director_archive_references_are_preserved(self):
        session=self.session(archive=True); pcm=b'\x01\x00'*3200
        await session.begin(pcm=pcm,text='Show me Monet.'); await asyncio.wait_for(session.task,1)
        self.assertEqual(self.providers.stt.calls,[])
        request=self.providers.native[0]
        self.assertEqual(request['kwargs']['pcm'],pcm)
        self.assertEqual(self.providers.director[0]['kwargs']['archive'],request['kwargs']['archive'])
        published=next(value for value in self.events if value['type']=='archive_sources')
        self.assertEqual(json.loads(request['kwargs']['archive'][1]['content'])['references'],published['matches'])
        self.assertIn('Backstage direction',request['character']['beat'])
        self.assertEqual(self.providers.director[0]['latest'],'Show me Monet.')

    async def test_stop_cancels_stream_and_omits_unaligned_partial_audio_from_memory(self):
        self.providers.gate=asyncio.Event(); session=self.session()
        await session.begin(text='Hello.'); await asyncio.wait_for(self.first_audio.wait(),1)
        await session.interrupt()
        self.assertTrue(self.providers.cancelled); self.assertFalse(session.reply_pending)
        self.assertEqual(session.history.messages[-1]['content'],'')
        self.assertFalse(any(event['type']=='response_done' for event in self.events))
        self.assertIsNone(session.retryable_voice)

    async def test_incomplete_or_wrong_rate_stream_is_not_a_completed_or_retryable_take(self):
        for failure in ('transport','rate','missing_done'):
            with self.subTest(failure=failure):
                self.events.clear(); self.providers.failure=failure; session=self.session()
                await session.begin(text='Hello.'); await asyncio.wait_for(session.task,1)
                error=self.events[-1]; self.assertEqual(error['type'],'error')
                self.assertFalse(error['can_retry_voice']); self.assertIn('Qwen3-Omni',error['message'])
                self.assertFalse(session.generation_complete)
                self.assertEqual(session.history.messages[-1]['content'],'')

    async def test_typed_and_stage_turns_do_not_invent_microphone_input(self):
        session=self.session(); await session.begin(text='[Private story cue: enter the next beat]',action=True)
        await asyncio.wait_for(session.task,1)
        self.assertIsNone(self.providers.native[0]['kwargs']['pcm'])
        self.assertEqual(self.providers.director[0]['latest'],'[Private story cue: enter the next beat]')
