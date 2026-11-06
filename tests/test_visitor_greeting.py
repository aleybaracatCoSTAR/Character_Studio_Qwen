import unittest
from live_voice.visitor import introduced_name, introduction_only, visitor_note
from live_voice.session import LiveSession
from live_voice.director import DirectorSettings
from studio.characters import example
from test_archive_session import ArchiveProviders
from test_omni_session import StreamingProviders

class VisitorNames(unittest.TestCase):
    def test_explicit_names_and_ordinary_speech(self):
        for line,name in [('My name is Aley.','Aley'),('Hello, my name is Jean-Luc.','Jean-Luc'),("You can call me O’Connor.",'O’Connor'),('My name is ليلى.','ليلى'),('My name is Aley and I like painting.','Aley')]:
            self.assertEqual(introduced_name(line),name)
        for line in ["I'm looking for paintings.",'Call me a taxi.','My name is <script>.','Show me a picture.']:
            self.assertIsNone(introduced_name(line))
        self.assertTrue(introduction_only('My name is Aley.','Aley'))
        self.assertFalse(introduction_only('My name is Aley. Show me a painting.','Aley'))

class GreetingSessions(unittest.IsolatedAsyncioTestCase):
    async def test_recognized_microphone_introduction_is_greeted_without_catalogue_lookup(self):
        from live_voice.archive import ArchiveSettings
        class NoSearch:
            count=0
            def search(self,*args,**kwargs): raise AssertionError('A greeting must not search the catalogue.')
            def close(self): pass
        providers=ArchiveProviders()
        async def recognize(pcm): return 'My name is Aley.'
        providers.stt.transcribe=recognize
        events=[]
        async def send(value): events.append(value)
        async def audio(value): pass
        session=LiveSession(providers,example(),send,audio,gallery_greeting=True,
            director=DirectorSettings(enabled=False),archive=ArchiveSettings(enabled=True,scope='library'),archive_search=NoSearch())
        try:
            await session.begin(pcm=b'\x01\x00'*320);await session.task
            self.assertEqual(session.visitor_name,'Aley')
            self.assertIn('Begin this reply with Hello',providers.actor[-1]['character']['beat'])
            self.assertFalse(any(event['type']=='archive_sources' for event in events))
        finally: await session.close()

    async def test_both_voice_engines_receive_greeting_and_remember_name(self):
        for providers in [ArchiveProviders(),StreamingProviders()]:
            events=[]
            async def send(value): events.append(value)
            async def audio(value): pass
            session=LiveSession(providers,example(),send,audio,director=DirectorSettings(enabled=False),gallery_greeting=True)
            try:
                await session.begin(text='My name is Aley.')
                await session.task
                calls=providers.native if hasattr(providers,'native') else providers.actor
                self.assertIn('Begin this reply with Hello',calls[-1]['character']['beat'])
                self.assertIn('Aley',calls[-1]['character']['beat'])
                self.assertTrue(any(event['type']=='visitor_name' and event['name']=='Aley' for event in events))
                await session.begin(text='What should we look at?')
                await session.task
                self.assertIn('Remember their name naturally',calls[-1]['character']['beat'])
                self.assertNotIn('Begin this reply with Hello',calls[-1]['character']['beat'])
            finally: await session.close()

    async def test_regular_character_session_does_not_enable_gallery_names(self):
        providers=ArchiveProviders()
        async def send(value): pass
        session=LiveSession(providers,example(),send,send,director=DirectorSettings(enabled=False))
        try:
            await session.begin(text='My name is Aley.');await session.task
            self.assertIsNone(session.visitor_name)
            self.assertNotIn('Gallery visitor name',providers.actor[-1]['character']['beat'])
        finally: await session.close()
