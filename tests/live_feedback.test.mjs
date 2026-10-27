import test from 'node:test';
import assert from 'node:assert/strict';
import {CallFeedback} from '../frontend/live-feedback.mjs';

test('a transient incomplete-take retry notice clears after success, next turn or disconnect',()=>{
  const element={textContent:''},feedback=new CallFeedback(element);
  for(const boundary of ['success','new turn','disconnect']){
    feedback.show('Retrying the whole reply with the same emotion and voice.','voice_notice');
    feedback.clearVoiceNotice();assert.equal(element.textContent,'',boundary);
  }
});

test('a final error replacing the notice remains visible at success or connection boundaries',()=>{
  const element={textContent:''},feedback=new CallFeedback(element);
  feedback.show('Retrying the whole reply.','voice_notice');
  feedback.show('The voice take did not finish. Try Retry voice.');
  feedback.clearVoiceNotice();assert.equal(element.textContent,'The voice take did not finish. Try Retry voice.');
  feedback.show('The live connection closed. Check the server.');
  feedback.clearVoiceNotice();assert.equal(element.textContent,'The live connection closed. Check the server.');
});

test('unrelated acknowledgements and deliberate clearing are preserved',()=>{
  const element={textContent:''},feedback=new CallFeedback(element);
  feedback.show('Direction saved.');feedback.clearVoiceNotice();assert.equal(element.textContent,'Direction saved.');
  feedback.show();assert.equal(element.textContent,'');
});
