import test from 'node:test';
import assert from 'node:assert/strict';
import {VoiceRetry} from '../frontend/live-voice-retry.mjs';

function setup(){
  let click,connected=true;
  const button={hidden:false,disabled:false,addEventListener:(name,handler)=>{assert.equal(name,'click');click=handler;}};
  const sent=[],effects=[];
  const retry=new VoiceRetry({button,send:value=>sent.push(value),
    canRetry:()=>connected,onRetry:()=>effects.push('hold microphone'),
    onUnavailable:event=>effects.push(['show retry unavailable and resume microphone',event.message]),
    onError:error=>effects.push(['show error and stop playback',error.message])});
  return {retry,button,sent,effects,click:()=>click(),disconnect:()=>{connected=false;retry.clear();}};
}

test('retry is hidden until the current turn has a recoverable voice error',()=>{
  const {retry,button,effects}=setup();
  assert.equal(button.hidden,true);assert.equal(button.disabled,true);
  assert.equal(retry.handleError({type:'error',turn_id:7,message:'Voice failed.',can_retry_voice:true},7),true);
  assert.equal(button.hidden,false);assert.equal(button.disabled,false);assert.equal(retry.failedTurn,7);
  assert.deepEqual(effects,[['show error and stop playback','Voice failed.']]);
});

test('retry sends only the failed turn and holds the microphone without creating new dialogue',()=>{
  const {retry,button,sent,effects,click}=setup();
  retry.handleError({turn_id:4,message:'Try again.',can_retry_voice:true},4);effects.length=0;
  click();click();
  assert.deepEqual(sent,[{type:'retry_voice',turn_id:4}]);
  assert.deepEqual(effects,['hold microphone']);assert.equal(button.hidden,true);assert.equal(button.disabled,true);
});

test('a stale or malformed turn error cannot stop newer audio or replace recovery state',()=>{
  const {retry,button,effects}=setup();
  retry.handleError({turn_id:8,message:'Current voice error.',can_retry_voice:true},8);effects.length=0;
  for(const turn_id of [7,9,'8',null]){
    assert.equal(retry.handleError({turn_id,message:'Stale error.',can_retry_voice:true},8),false);
  }
  assert.deepEqual(effects,[]);assert.equal(retry.failedTurn,8);assert.equal(button.hidden,false);
});

test('global setup errors are still shown and cannot offer a voice retry',()=>{
  const {retry,button,effects}=setup();
  assert.equal(retry.handleError({message:'Call settings invalid.',can_retry_voice:true},0),true);
  assert.deepEqual(effects,[['show error and stop playback','Call settings invalid.']]);
  assert.equal(button.hidden,true);assert.equal(retry.failedTurn,null);
});

test('unrecoverable errors hide a previous retry and the same failure may offer it again',()=>{
  const {retry,button}=setup();
  retry.handleError({turn_id:3,message:'Voice failed.',can_retry_voice:true},3);retry.retry();
  retry.handleError({turn_id:3,message:'Voice failed again.',can_retry_voice:true},3);
  assert.equal(button.hidden,false);assert.equal(retry.failedTurn,3);
  retry.handleError({turn_id:3,message:'Nothing to retry.',can_retry_voice:false},3);
  assert.equal(button.hidden,true);assert.equal(retry.failedTurn,null);
});

test('new turn, success, interruption and new-call lifecycle can clear recovery without sending anything',()=>{
  const {retry,button,sent}=setup();
  for(const reason of ['new turn','success','interruption','new call']){
    retry.handleError({turn_id:1,message:reason,can_retry_voice:true},1);
    retry.clear();assert.equal(button.hidden,true);assert.equal(button.disabled,true);
    assert.equal(retry.retry(),false);
  }
  assert.deepEqual(sent,[]);
});

test('disconnect removes recovery and rejects a late recoverable error as an available action',()=>{
  const {retry,button,sent,disconnect}=setup();
  retry.handleError({turn_id:2,message:'Voice failed.',can_retry_voice:true},2);disconnect();
  retry.handleError({turn_id:2,message:'Late error.',can_retry_voice:true},2);
  assert.equal(button.hidden,true);assert.equal(retry.retry(),false);assert.deepEqual(sent,[]);
});

test('a rejected current-turn retry resumes the microphone and removes recovery without stopping unrelated playback',()=>{
  const {retry,button,effects,sent}=setup();
  retry.handleError({turn_id:5,message:'Voice failed.',can_retry_voice:true},5);retry.retry();effects.length=0;
  assert.equal(retry.handleUnavailable({turn_id:5,message:'That voice take is no longer available.'},5),true);
  assert.deepEqual(effects,[['show retry unavailable and resume microphone','That voice take is no longer available.']]);
  assert.equal(button.hidden,true);assert.equal(button.disabled,true);
  assert.deepEqual(sent,[{type:'retry_voice',turn_id:5}]);
});

test('an old rejected retry cannot reopen the microphone for a newer turn',()=>{
  const {retry,effects}=setup();
  for(const turn_id of [4,6,'5',null]){
    assert.equal(retry.handleUnavailable({turn_id,message:'Old retry unavailable.'},5),false);
  }
  assert.deepEqual(effects,[]);
});
