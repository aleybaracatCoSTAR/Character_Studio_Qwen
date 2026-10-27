import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../frontend/live-capture-worklet.js',import.meta.url),'utf8');

for(const rate of [44100,48000]){
  test(`microphone forwards forty millisecond packets without losing samples at ${rate} Hz`,()=>{
    let Processor;const sent=[];
    vm.runInNewContext(source,{
      sampleRate:rate,Float32Array,
      AudioWorkletProcessor:class{constructor(){this.port={postMessage:value=>sent.push(Array.from(value))};}},
      registerProcessor:(name,implementation)=>{assert.equal(name,'live-capture');Processor=implementation;}
    });
    const capture=new Processor(),size=Math.round(rate*.04);
    const input=Float32Array.from({length:size*3},(_,i)=>(i%97)/97);
    for(let offset=0;offset<input.length;offset+=128){
      const block=input.slice(offset,offset+128);
      assert.equal(capture.process([[block,block]]),true);
    }
    assert.equal(sent.length,3);
    assert.ok(sent.every(packet=>packet.length===size));
    assert.deepEqual(sent.flat(),Array.from(input));
  });
}
