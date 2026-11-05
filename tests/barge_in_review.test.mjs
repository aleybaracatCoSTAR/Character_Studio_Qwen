import test from 'node:test';
import assert from 'node:assert/strict';
import {BargeInController,SpeechBargeDetector,echoCorrelation} from '../frontend/live-barge-in.mjs';
import {PCMPlayback,pcm16} from '../frontend/live-audio.mjs';

const RATE=16000;
const samples=(length,make)=>Float32Array.from({length},(_,i)=>make(i/RATE,i));
const voice=length=>samples(length,t=>.065*Math.sin(2*Math.PI*117*t)+.025*Math.sin(2*Math.PI*191*t));
const echo=length=>samples(length,t=>.055*Math.sin(2*Math.PI*317*t)+.02*Math.sin(2*Math.PI*431*t));
const join=(...parts)=>{const out=new Float32Array(parts.reduce((sum,p)=>sum+p.length,0));let offset=0;for(const part of parts){out.set(part,offset);offset+=part.length;}return out;};
function detect(detector,input,reference=new Float32Array(0)){
  for(let at=0;at<input.length;at+=640){const found=detector.feed(input.slice(at,at+640),{reference});if(found)return found;}
  return null;
}
function harness(){
  const sent=[],stopped=[],accepted=[],rejected=[],timers=new Map();let next=0;
  const controller=new BargeInController({send:value=>sent.push(value),onStop:id=>stopped.push(id),
    onAccepted:id=>accepted.push(id),onRejected:id=>rejected.push(id),
    setTimer:(fn,ms)=>{const id=++next;timers.set(id,{fn,ms});return id;},clearTimer:id=>timers.delete(id)});
  controller.configure({enabled:true,permitted:true});
  return {controller,sent,stopped,accepted,rejected,timers};
}

test('scaled, inverted, delayed output matches are vetoed instead of interrupting the character itself',()=>{
  const reference=echo(Math.round(.6*RATE));
  const delayed=reference.slice(1280,1280+640);
  for(const scale of [.6,-.6,1]){
    const microphone=Float32Array.from(delayed,value=>value*scale+.003);
    assert.ok(echoCorrelation(microphone,reference)>.97);
  }
  const detector=new SpeechBargeDetector();
  const microphone=Float32Array.from(reference,value=>-value*.6+.003);
  assert.equal(detect(detector,microphone,reference),null);
});

test('a distinct foreground voice can interrupt during comparable-energy double talk',()=>{
  const reference=echo(Math.round(.6*RATE));
  const foreground=voice(Math.round(.4*RATE));
  const microphone=Float32Array.from(foreground,(value,index)=>value+reference[index]*.8);
  assert.ok(echoCorrelation(microphone.slice(0,640),reference)<.78);
  assert.ok(detect(new SpeechBargeDetector(),microphone,reference) instanceof Float32Array);
  assert.ok(detect(new SpeechBargeDetector(),foreground) instanceof Float32Array);
});

test('onset confirmation retains the complete early speech and preceding 200ms rather than its final frame alone',()=>{
  const detector=new SpeechBargeDetector();
  const preroll=samples(3200,(_,i)=>.001+i*.00000001);
  assert.equal(detector.feed(preroll),null);
  const utterance=voice(3840);
  const found=detector.feed(utterance);
  assert.deepEqual(found,join(preroll,utterance));
  // A reset cannot let the previous person's preroll into a new detection.
  detector.reset();assert.deepEqual(detector.feed(utterance),utterance);
});

test('the ack handoff sends each buffered first syllable once and never sends microphone PCM before acceptance',()=>{
  const {controller,sent,stopped,accepted,timers}=harness();controller.begin(12);
  const initial=voice(4000),continuing=voice(1280);
  controller.feed(initial);controller.feed(continuing);
  assert.deepEqual(sent,[{type:'barge_in',turn_id:12}]);assert.deepEqual(stopped,[12]);
  assert.ok(controller.pending);assert.equal(timers.size,1);
  assert.equal(controller.acknowledge({type:'barge_in_ready',turn_id:12,accepted:true}),true);
  assert.deepEqual(accepted,[12]);assert.equal(timers.size,0);
  const packets=sent.slice(1);assert.ok(packets.every(packet=>packet instanceof ArrayBuffer&&packet.byteLength<=1280));
  const bytes=new Uint8Array(packets.reduce((sum,packet)=>sum+packet.byteLength,0));let at=0;
  for(const packet of packets){bytes.set(new Uint8Array(packet),at);at+=packet.byteLength;}
  assert.deepEqual(bytes,new Uint8Array(pcm16(join(initial,continuing))));
  const count=sent.length;
  assert.equal(controller.acknowledge({type:'barge_in_ready',turn_id:12,accepted:true}),false);
  assert.equal(sent.length,count);assert.equal(controller.isSuppressed(12),true);
});

test('a new turn rejects an old acknowledgement and clears its old suppression without flushing old speech',()=>{
  const {controller,sent,accepted,timers}=harness();controller.begin(21);controller.feed(voice(4000));
  assert.equal(controller.isSuppressed(21),true);assert.equal(timers.size,1);
  controller.begin(22);assert.equal(timers.size,0);assert.equal(controller.pending,null);
  assert.equal(controller.isSuppressed(21),false);
  assert.equal(controller.acknowledge({type:'barge_in_ready',turn_id:21,accepted:true}),false);
  assert.deepEqual(accepted,[]);assert.equal(sent.length,1);
  controller.feed(voice(4000));assert.equal(controller.isSuppressed(22),true);
  assert.equal(controller.acknowledge({type:'barge_in_ready',turn_id:21,accepted:true}),false);
  assert.ok(controller.pending);assert.equal(sent.length,2);
});

test('a missing acknowledgement bounds retained audio and keeps old output suppressed after recovery',()=>{
  const {controller,sent,rejected,timers}=harness();controller.begin(31);controller.feed(voice(4000));
  const deadline=[...timers.values()][0];assert.ok(deadline.ms<=2000);deadline.fn();
  assert.equal(controller.pending,null);assert.deepEqual(rejected,[31]);assert.equal(sent.length,1);
  assert.equal(controller.isSuppressed(31),true);
  controller.reset();assert.equal(controller.isSuppressed(31),false);
});

function context(){
  return {currentTime:0,destination:{},sources:[],
    createBuffer(channels,length,rate){return {duration:length/rate,samples:new Float32Array(length),copyToChannel(value){this.samples.set(value);}};},
    createBufferSource(){const source={connect(){},disconnect(){},start(when){this.when=when;},stop(){this.onended?.();},finish(){this.onended?.();}};this.sources.push(source);return source;}};
}
function audioPacket(turn,length){
  const bytes=new ArrayBuffer(8+length*2),view=new DataView(bytes);view.setUint32(0,turn);view.setUint32(4,0);
  for(let i=0;i<length;i++)view.setInt16(8+i*2,Math.round(8000*Math.sin(2*Math.PI*203*i/44100)),true);
  return bytes;
}

test('the echo reference excludes future audio, aligns to actual playback and expires after the acoustic tail',()=>{
  const ctx=context(),sent=[],player=new PCMPlayback(ctx,event=>sent.push(event),()=>{},()=>{});
  player.setTurn(41);player.push(audioPacket(41,44100));
  assert.ok(player.referenceWindow().every(value=>value===0));
  ctx.currentTime=.225;
  const heard=player.referenceWindow(ctx.currentTime,.1);
  const expected=samples(1600,t=>8000/32768*Math.sin(2*Math.PI*203*(.1+t)));
  assert.ok(echoCorrelation(heard,expected)>.99);
  ctx.currentTime=1.025;ctx.sources[0].finish();
  ctx.currentTime=1.125;
  const tail=player.referenceWindow(ctx.currentTime,.2);
  assert.ok(tail.slice(0,1500).some(value=>Math.abs(value)>.01));
  assert.ok(tail.slice(1700).every(value=>value===0));
  assert.deepEqual(sent,[]); // An acoustic reference cannot claim unacknowledged speech was heard.
  ctx.currentTime=1.7;assert.ok(player.referenceWindow().every(value=>value===0));
  player.setTurn(42);player.push(audioPacket(42,44100));ctx.currentTime=1.95;
  assert.ok(player.referenceWindow().some(value=>value!==0));player.clear();
  assert.ok(player.referenceWindow().every(value=>value===0));
});
