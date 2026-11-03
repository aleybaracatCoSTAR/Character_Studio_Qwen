import test from 'node:test';
import assert from 'node:assert/strict';
import {ListeningFeedback} from '../frontend/live-listening.mjs';

function setup(){
  const label={},partial={},events=[];
  const view=new ListeningFeedback({label,partial,onFinalizing:()=>events.push('finalizing'),
    onListening:()=>events.push('listening'),onError:message=>events.push(message)});
  return {label,partial,events,view};
}
test('baseline hides the experiment and refuses interim events',()=>{
  const {view,label,partial}=setup();view.setMode('baseline');
  assert.equal(label.hidden,true);assert.equal(partial.hidden,true);
  assert.equal(view.accept({type:'listening_state',input_id:1,phase:'listening'}),false);
});
test('experimental partials remain literal temporary text and require an active input',()=>{
  const {view,label,partial}=setup();view.setMode('nemotron-smart-turn');
  assert.equal(label.hidden,false);
  assert.equal(view.accept({type:'listening_partial',input_id:1,text:'early'}),false);
  view.accept({type:'listening_state',input_id:1,phase:'listening'});
  assert.equal(view.accept({type:'listening_partial',input_id:1,text:'<img onerror=synthetic>'}),true);
  assert.equal(partial.textContent,'Hearing: <img onerror=synthetic>');
  assert.equal(partial.hidden,false);
  view.clear();assert.equal(partial.hidden,true);
  assert.equal(view.accept({type:'listening_partial',input_id:1,text:'late'}),false);
});
test('stale input events cannot reopen capture or replace the newer transcript',()=>{
  const {view,partial,events}=setup();view.setMode('nemotron-smart-turn');
  view.accept({type:'listening_state',input_id:2,phase:'listening'});
  view.accept({type:'listening_partial',input_id:2,text:'new words'});
  assert.equal(view.accept({type:'listening_state',input_id:1,phase:'error',message:'old error'}),false);
  assert.equal(view.accept({type:'listening_partial',input_id:1,text:'old words'}),false);
  assert.equal(partial.textContent,'Hearing: new words');assert.deepEqual(events,['listening']);
});
test('endpoint feedback pauses capture and error feedback can release it',()=>{
  const {view,events,partial}=setup();view.setMode('nemotron-smart-turn');
  view.accept({type:'listening_state',input_id:1,phase:'listening'});
  view.accept({type:'listening_partial',input_id:1,text:'pending words'});
  view.accept({type:'listening_state',input_id:1,phase:'finalizing'});
  assert.equal(partial.hidden,true);assert.equal(events.at(-1),'finalizing');
  view.accept({type:'listening_state',input_id:1,phase:'error',message:'Please try again.'});
  assert.equal(events.at(-1),'Please try again.');
});
test('new calls clear temporary words and reset input identifiers',()=>{
  const {view,partial}=setup();view.setMode('nemotron-smart-turn');
  view.accept({type:'listening_state',input_id:9,phase:'listening'});
  view.accept({type:'listening_partial',input_id:9,text:'temporary'});view.reset();
  assert.equal(view.inputId,0);assert.equal(partial.textContent,'');
  assert.equal(view.accept({type:'listening_state',input_id:1,phase:'listening'}),true);
});
test('invalid events and oversized partial text are rejected',()=>{
  const {view}=setup();view.setMode('nemotron-smart-turn');
  for(const value of [0,-1,1.5,'1',Infinity])assert.equal(view.accept({type:'listening_state',input_id:value,phase:'listening'}),false);
  assert.equal(view.accept({type:'listening_state',input_id:1,phase:'unexpected'}),false);
  view.accept({type:'listening_state',input_id:1,phase:'listening'});
  assert.equal(view.accept({type:'listening_partial',input_id:1,text:'x'.repeat(4001)}),false);
});
