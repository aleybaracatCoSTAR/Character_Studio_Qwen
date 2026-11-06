import test from 'node:test';
import assert from 'node:assert/strict';
import {PCMPlayback} from '../frontend/live-audio.mjs';
function packet(turn,samples){const buffer=new ArrayBuffer(8+samples.length*2),view=new DataView(buffer);view.setUint32(0,turn);view.setUint32(4,0);samples.forEach((x,i)=>view.setInt16(8+2*i,x,true));return buffer;}
function context(){return {currentTime:0,destination:{},sources:[],createBuffer(_,length,rate){return {samples:new Float32Array(length),duration:length/rate,rate,copyToChannel(samples){this.samples.set(samples);}};},createBufferSource(){const source={connect(){},disconnect(){},start(at){this.at=at;},stop(){this.stopped=true;this.onended?.();},finish(){this.onended?.();}};this.sources.push(source);return source;}};}

test('native 24 kHz output uses derived buffering thresholds without changing duration or samples',()=>{
  const audio=context(),player=new PCMPlayback(audio,()=>{},()=>{},()=>{});assert.equal(player.rate,44100);
  assert.equal(player.setRate(24000),true);assert.equal(player.prebufferSamples,2880);assert.equal(player.batchSamples,1440);player.setTurn(1);
  const samples=Array.from({length:24000},(_,index)=>Math.round(Math.sin(2*Math.PI*400*index/24000)*10000));player.push(packet(1,samples));
  assert.equal(audio.sources[0].buffer.rate,24000);assert.equal(audio.sources[0].buffer.duration,1);
  assert.equal(audio.sources[0].buffer.samples.length,samples.length);
  for(let index=0;index<samples.length;index++)assert.ok(Math.abs(audio.sources[0].buffer.samples[index]-samples[index]/32768)<1e-7);
  audio.currentTime=.025+.05;const reference=player.referenceWindow(audio.currentTime,.03,16000);
  assert.ok(reference.some(value=>Math.abs(value)>.1));
});

test('changing rates clears old scheduled and buffered audio without acknowledging an unheard phrase',()=>{
  const audio=context(),events=[];let idle=0;const player=new PCMPlayback(audio,value=>events.push(value),()=>{},()=>idle++);
  player.setTurn(7);player.push(packet(7,Array(5292).fill(100)));player.push(packet(7,[123]));player.phraseDone(0);player.responseDone();
  const old=audio.sources.slice();assert.equal(player.setRate(24000),true);old.forEach(source=>source.finish());
  assert.ok(old.every(source=>source.stopped));assert.deepEqual(events,[]);assert.equal(idle,0);assert.equal(player.turn,0);assert.equal(player.phrases.size,0);
  assert.ok(player.referenceWindow().every(value=>value===0));player.setTurn(8);player.push(packet(7,Array(2880).fill(200)));assert.equal(audio.sources.length,old.length);
  player.push(packet(8,Array(2880).fill(300)));assert.equal(audio.sources.at(-1).buffer.duration,.12);assert.equal(audio.sources.at(-1).buffer.rate,24000);
});

test('only advertised baseline or native rates are accepted, and baseline buffering remains unchanged',()=>{
  const audio=context(),player=new PCMPlayback(audio,()=>{},()=>{},()=>{});player.setTurn(2);player.push(packet(2,[1,2]));
  for(const rate of [0,-1,16000,48000,24000.1,'24000',null,undefined,NaN,Infinity])assert.equal(player.setRate(rate),false);
  assert.equal(player.turn,2);assert.equal(player.phrases.get(0).samples,2);assert.equal(player.rate,44100);
  assert.equal(player.setRate(44100),true);assert.equal(player.prebufferSamples,5292);assert.equal(player.batchSamples,2646);player.setTurn(3);
  player.push(packet(3,Array(44100).fill(100)));assert.equal(audio.sources[0].buffer.duration,1);assert.equal(audio.sources[0].buffer.rate,44100);
});
