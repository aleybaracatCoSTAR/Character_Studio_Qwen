import json
import unittest

from live_voice.director import DirectorSettings, director_messages
from live_voice.reactions import EmotionalTrigger, assessment_messages
from live_voice.turns import ReplyHistory
from live_voice.voice import VoiceSettings
from studio.characters import example


class ContextProviderTests(unittest.TestCase):
    def context(self):
        history = ReplyHistory()
        history.begin(1, 'My name is Aley. My favorite object is a blue teacup.')
        history.add_phrase(0, 'That red button erases my memories.')
        history.acknowledge(1, 0); history.interrupt()
        for turn in range(2, 26):
            history.begin(turn, f'This is another ordinary round {turn}.')
            history.add_phrase(0, f'I heard round {turn}.')
            history.acknowledge(turn, 0); history.interrupt()
        return history.context_messages(latest='Remember my name, favorite object and the red button?', budget=6000)

    def test_director_keeps_retrieved_quotes_before_more_than_six_recent_pairs(self):
        context = self.context()
        self.assertGreater(len(context), 12)
        messages = director_messages(example(), context, 'Remember my name?', DirectorSettings(), VoiceSettings())
        actual = json.loads(messages[1]['content'])
        self.assertEqual(actual['recent_heard_conversation'], context)
        self.assertIn('blue teacup', str(actual['recent_heard_conversation']))
        self.assertEqual(actual['latest_participant_message'], 'Remember my name?')

    def test_legacy_trigger_controller_keeps_retrieved_trigger_evidence(self):
        context = self.context()
        _, actual = assessment_messages(example(), EmotionalTrigger(), context, 'My finger is on it.', False)
        self.assertEqual(actual['recent_conversation'], context)
        self.assertIn('erases my memories', str(actual['recent_conversation']))

    def test_manual_oversized_context_stays_bounded_for_both_controllers(self):
        history = [{'role':'user', 'content':'A'*12000}, {'role':'assistant','content':'B'*12000}]
        messages = director_messages(example(), history, 'Hello', DirectorSettings(), VoiceSettings())
        director = json.loads(messages[1]['content'])['recent_heard_conversation']
        _, trigger = assessment_messages(example(), EmotionalTrigger(), history, 'Hello', False)
        for context in (director, trigger['recent_conversation']):
            self.assertLessEqual(sum(len(message['content']) for message in context), 6000)
            self.assertEqual(context[-1]['role'], 'assistant')


if __name__ == '__main__':
    unittest.main()
