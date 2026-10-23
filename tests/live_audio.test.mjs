import test from 'node:test';
import assert from 'node:assert/strict';
import {PCMPlayback,StreamResampler,pcm16,unpackAudio} from '../frontend/live-audio.mjs';

function packet(turn,seq,samples=[0,1000,-1000]){const buffer=new ArrayBuffer(8+samples.length*2),view=new DataView(buffer);view.setUint32(0,turn);view.setUint32(4,seq);samples.forEach((x,i)=>view.setInt16(8+2*i,x,true));return buffer;}
function fakeContext(){return{currentTime:0,destination:{},sources:[],createBuffer(channels,length,rate){return{duration:length/rate,rate,samples:new Float32Array(length),copyToChannel(samples){this.samples.set(samples);}};},createBufferSource(){const source={connect(){},disconnect(){},start(at){this.at=at;},stop(){this.onended?.();},finish(){this.onended?.();}};this.sources.push(source);return source;}};}

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
test('uneven tiny packets build a reserve and play contiguous batches at the native rate',()=>{
  const context=fakeContext(),events=[];
  const player=new PCMPlayback(context,event=>events.push(event),()=>{},()=>{});player.setTurn(8);
  // Twelve 10 ms packets are coalesced into one 120 ms buffer, rather than
  // scheduling twelve independent sources as packets trickle through a tunnel.
  for(let i=0;i<11;i++)player.push(packet(8,0,Array(441).fill(i)));
  assert.equal(context.sources.length,0);
  player.push(packet(8,0,Array(441).fill(11)));
  assert.equal(context.sources.length,1);
  assert.equal(context.sources[0].buffer.rate,44100);
  assert.equal(context.sources[0].buffer.duration,.12);
  for(let i=12;i<18;i++)player.push(packet(8,0,Array(441).fill(i)));
  assert.equal(context.sources.length,2);
  assert.equal(context.sources[1].at,context.sources[0].at+context.sources[0].buffer.duration);
  assert.equal(context.sources[1].buffer.duration,.06);
  const actual=context.sources.flatMap(source=>Array.from(source.buffer.samples));
  const expected=Array.from({length:18},(_,i)=>Array(441).fill(i/32768)).flat();
  assert.deepEqual(actual,expected);assert.deepEqual(events,[]);
});
test('a short final tail is flushed and fully played before releasing the microphone',()=>{
  const context=fakeContext(),events=[];let idle=0;
  const player=new PCMPlayback(context,event=>events.push(event),()=>{},()=>idle++);player.setTurn(9);
  player.push(packet(9,0,Array(5292).fill(100)));
  player.push(packet(9,0,[200,300,400]));
  assert.equal(context.sources.length,1);
  player.phraseDone(0);player.responseDone();
  assert.equal(context.sources.length,2);
  assert.deepEqual(Array.from(context.sources[1].buffer.samples),[200/32768,300/32768,400/32768]);
  context.sources[0].finish();assert.deepEqual(events,[]);assert.equal(idle,0);
  context.sources[1].finish();
  assert.deepEqual(events,[{type:'played',turn_id:9,seq:0},{type:'playback_done',turn_id:9}]);
  assert.equal(idle,1);
});
test('a late final phrase plays even when all earlier scheduled audio has ended',()=>{
  const context=fakeContext(),events=[];let idle=0;
  const player=new PCMPlayback(context,event=>events.push(event),()=>{},()=>idle++);player.setTurn(10);
  player.push(packet(10,0,Array(5292).fill(100)));player.phraseDone(0);
  context.sources[0].finish();context.currentTime=.3;
  assert.deepEqual(events,[{type:'played',turn_id:10,seq:0}]);assert.equal(idle,0);
  player.push(packet(10,1,Array(4410).fill(200)));
  assert.equal(context.sources.length,1); // Rebuffer after the underrun.
  player.push(packet(10,1,Array(882).fill(300)));
  assert.equal(context.sources.length,2);assert.equal(context.sources[1].at,.325);
  player.push(packet(10,1,[400]));player.phraseDone(1);player.responseDone();
  context.sources[1].finish();assert.equal(idle,0);
  context.sources[2].finish();
  assert.deepEqual(events.slice(1),[{type:'played',turn_id:10,seq:1},{type:'playback_done',turn_id:10}]);
  assert.equal(idle,1);
});
test('an entirely short phrase is not left waiting for the prebuffer threshold',()=>{
  const context=fakeContext(),events=[];
  const player=new PCMPlayback(context,event=>events.push(event),()=>{},()=>{});player.setTurn(11);
  player.push(packet(11,0,[120,240]));assert.equal(context.sources.length,0);
  player.phraseDone(0);player.responseDone();assert.equal(context.sources.length,1);
  context.sources[0].finish();
  assert.deepEqual(events,[{type:'played',turn_id:11,seq:0},{type:'playback_done',turn_id:11}]);
});
test('clear discards unflushed samples and stale callbacks across a new turn',()=>{
  const context=fakeContext(),events=[];let idle=0;
  const player=new PCMPlayback(context,event=>events.push(event),()=>{},()=>idle++);player.setTurn(12);
  player.push(packet(12,0,Array(5292).fill(100)));player.push(packet(12,0,[999]));
  const oldSource=context.sources[0];player.setTurn(13);oldSource.finish();
  player.push(packet(12,0,Array(5292).fill(100)));assert.equal(context.sources.length,1);
  player.push(packet(13,0,[200]));player.phraseDone(0);player.responseDone();
  assert.deepEqual(events,[]);assert.equal(idle,0);
  assert.deepEqual(Array.from(context.sources[1].buffer.samples),[200/32768]);
  context.sources[1].finish();
  assert.deepEqual(events,[{type:'played',turn_id:13,seq:0},{type:'playback_done',turn_id:13}]);
});

// A new call starts turn IDs at 1. Its reply must never append to the previous character.
import {Conversation} from '../frontend/live-transcript.mjs';
test('new-call transcript clears old reply records and DOM while preserving the empty state',()=>{
  function element(){return {children:[],hidden:false,textContent:'',append(...items){for(const item of items){item.parent=this;this.children.push(item);}},remove(){this.parent.children.splice(this.parent.children.indexOf(this),1);}};}
  const oldDocument=globalThis.document;
  globalThis.document={createElement:element};
  try{
    const container=element(),empty=element();container.append(empty);
    const chat=new Conversation(container,empty);
    chat.replies.set(1,chat.message('Anna','Press Start.'));
    assert.equal(empty.hidden,true);assert.equal(container.children.length,2);
    chat.reset();assert.equal(chat.replies.size,0);assert.equal(container.children.length,1);assert.equal(empty.hidden,false);
    chat.replies.set(1,chat.message('Ali','I am working on a song.'));
    assert.equal(chat.replies.get(1).node.children[0].textContent,'Ali');
    assert.equal(chat.replies.get(1).content.textContent,'I am working on a song.');
    assert.equal(container.children.length,2);
  }finally{globalThis.document=oldDocument;}
});
