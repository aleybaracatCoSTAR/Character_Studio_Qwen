import test from 'node:test';
import assert from 'node:assert/strict';
import {BargeInController,SpeechBargeDetector} from '../frontend/live-barge-in.mjs';

const voice=length=>Float32Array.from({length},(_,index)=>.06*Math.sin(2*Math.PI*139*index/16000)+.03*Math.sin(2*Math.PI*227*index/16000));
function setup(){
  const sent=[],stopped=[],accepted=[],rejected=[],timers=new Map();let serial=0;
  const controller=new BargeInController({send:value=>sent.push(value),onStop:value=>stopped.push(value),
    onAccepted:value=>accepted.push(value),onRejected:value=>rejected.push(value),
    setTimer:callback=>{timers.set(++serial,callback);return serial;},clearTimer:id=>timers.delete(id)});
  controller.configure({enabled:true,permitted:true});controller.begin(1);
  return {controller,sent,stopped,accepted,rejected,timers};
}

test('short sounds, constant signals and nonfinite samples cannot confirm a sustained interruption',()=>{
  for(const samples of [voice(3200),new Float32Array(4800).fill(.2),new Float32Array(4800).fill(NaN)]){
    assert.equal(new SpeechBargeDetector().feed(samples),null);
  }
  const detector=new SpeechBargeDetector();assert.equal(detector.feed(voice(3520)),null);
  assert.ok(detector.feed(voice(320)) instanceof Float32Array);
});

test('both the local preference and server permission are required before stopping any output',()=>{
  for(const options of [{enabled:false,permitted:true},{enabled:true,permitted:false}]){
    const {controller,sent,stopped}=setup();controller.configure(options);controller.feed(voice(4800));
    assert.deepEqual(sent,[]);assert.deepEqual(stopped,[]);assert.equal(controller.pending,null);
  }
});

test('each reply turn latches one request and a rejected acknowledgement cannot release raw microphone input',()=>{
  const {controller,sent,stopped,accepted,rejected}=setup();controller.feed(voice(4000));
  assert.equal(controller.acknowledge({type:'barge_in_ready',turn_id:1,accepted:false}),true);
  controller.feed(voice(4000));controller.begin(1);controller.feed(voice(4000));
  assert.deepEqual(sent,[{type:'barge_in',turn_id:1}]);assert.deepEqual(stopped,[1]);assert.deepEqual(accepted,[]);assert.deepEqual(rejected,[1]);
  assert.equal(controller.isSuppressed(1),true);
  controller.begin(2);controller.feed(voice(4000));assert.deepEqual(sent.at(-1),{type:'barge_in',turn_id:2});
});

test('the two-second pending buffer limit fails safely without forwarding any of the held speech',()=>{
  const {controller,sent,rejected}=setup();controller.feed(voice(4000));
  for(let index=0;index<7;index++)controller.feed(voice(4000));
  assert.equal(controller.pending.samples.length,32000);controller.feed(voice(320));
  assert.equal(controller.pending,null);assert.deepEqual(rejected,[1]);assert.deepEqual(sent,[{type:'barge_in',turn_id:1}]);
  assert.equal(controller.isSuppressed(1),true);
});

test('an already-queued old timeout cannot reject a later turn while its acknowledgement is pending',()=>{
  const {controller,timers,rejected}=setup();controller.feed(voice(4000));const oldTimeout=[...timers.values()][0];
  controller.begin(2);controller.feed(voice(4000));oldTimeout();
  assert.equal(controller.pending.turnId,2);assert.deepEqual(rejected,[]);
});

test('mute, disable, manual-stop and new-call cancellation discard held audio and reject late acknowledgements',()=>{
  for(const action of ['cancel','disable','reset']){
    const {controller,sent,timers,accepted}=setup();controller.feed(voice(4000));
    if(action==='disable')controller.configure({enabled:false});else controller[action]();
    assert.equal(controller.pending,null);assert.equal(timers.size,0);
    assert.equal(controller.acknowledge({type:'barge_in_ready',turn_id:1,accepted:true}),false);
    assert.deepEqual(accepted,[]);assert.equal(sent.length,1);
  }
});
