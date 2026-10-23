import test from 'node:test';
import assert from 'node:assert/strict';
import {PCMPlayback,StreamResampler,pcm16,unpackAudio} from '../frontend/live-audio.mjs';

function packet(turn,seq,samples=[0,1000,-1000]){const buffer=new ArrayBuffer(8+samples.length*2),view=new DataView(buffer);view.setUint32(0,turn);view.setUint32(4,seq);samples.forEach((x,i)=>view.setInt16(8+2*i,x,true));return buffer;}
function fakeContext(){return{currentTime:0,destination:{},sources:[],createBuffer(channels,length,rate){return{duration:length/rate,copyToChannel(){}};},createBufferSource(){const source={connect(){},disconnect(){},start(at){this.at=at;},stop(){this.onended?.();},finish(){this.onended?.();}};this.sources.push(source);return source;}};}

test('PCM encoding clips samples and neutralises NaN',()=>{const view=new DataView(pcm16([-2,-1,0,1,2,NaN]));assert.deepEqual(Array.from({length:6},(_,i)=>view.getInt16(i*2,true)),[-32768,-32768,0,32767,32767,0]);});
test('audio packets preserve turn, phrase and little-endian samples',()=>{const value=unpackAudio(packet(42,2));assert.equal(value.turn,42);assert.equal(value.sequence,2);assert.equal(value.samples[1],1000/32768);assert.throws(()=>unpackAudio(new ArrayBuffer(9)));});
test('resampling across packets agrees with one continuous buffer',()=>{for(const rate of [44100,48000,16000]){const input=Float32Array.from({length:rate},(_,i)=>Math.sin(2*Math.PI*300*i/rate));const expected=new StreamResampler(rate).push(input);const converter=new StreamResampler(rate),parts=[];for(let start=0;start<input.length;start+=137)parts.push(...converter.push(input.slice(start,start+137)));assert.ok(Math.abs(parts.length-16000)<=1);assert.equal(parts.length,expected.length);for(let i=0;i<parts.length;i++)assert.ok(Math.abs(parts[i]-expected[i])<.00001);}});
test('playback acknowledgements wait until the phrase actually finishes',()=>{const context=fakeContext(),acks=[];const player=new PCMPlayback(context,x=>acks.push(x),()=>{},()=>{});player.setTurn(1);player.push(packet(1,0));player.phraseDone(0);assert.equal(acks.length,0);context.sources[0].finish();assert.deepEqual(acks,[{type:'played',turn_id:1,seq:0}]);});
test('interruption discards queued sources without claiming they were played',()=>{const context=fakeContext(),acks=[];const player=new PCMPlayback(context,x=>acks.push(x),()=>{},()=>{});player.setTurn(1);player.push(packet(1,0));player.phraseDone(0);player.clear();assert.equal(acks.length,0);player.setTurn(2);player.push(packet(1,1));assert.equal(context.sources.length,1);});
test('out-of-order ended callbacks still acknowledge phrases in order',()=>{const context=fakeContext(),acks=[];const player=new PCMPlayback(context,x=>acks.push(x.seq),()=>{},()=>{});player.setTurn(7);player.push(packet(7,0));player.push(packet(7,1));player.phraseDone(0);player.phraseDone(1);context.sources[1].finish();assert.deepEqual(acks,[]);context.sources[0].finish();assert.deepEqual(acks,[0,1]);});
test('microphone release waits for final playback and is emitted once',()=>{
  const context=fakeContext(),events=[];let idle=0;
  const player=new PCMPlayback(context,event=>events.push(event),()=>{},()=>idle++);
  player.setTurn(3);player.push(packet(3,0));player.push(packet(3,1));
  player.phraseDone(0);player.phraseDone(1);player.responseDone();
  assert.deepEqual(events,[]);assert.equal(idle,0);
  context.sources[0].finish();assert.equal(events.length,1);assert.equal(idle,0);
  context.sources[1].finish();
  assert.deepEqual(events,[{type:'played',turn_id:3,seq:0},{type:'played',turn_id:3,seq:1},{type:'playback_done',turn_id:3}]);
  assert.equal(idle,1);player.acknowledge();assert.equal(idle,1);
});
test('stopped playback cannot reopen the microphone from an old ended callback',()=>{
  const context=fakeContext(),events=[];let idle=0;
  const player=new PCMPlayback(context,event=>events.push(event),()=>{},()=>idle++);
  player.setTurn(3);player.push(packet(3,0));player.phraseDone(0);player.responseDone();
  player.clear();context.sources[0].finish();assert.deepEqual(events,[]);assert.equal(idle,0);
});
