export class StreamResampler {
  constructor(inputRate,outputRate=16000){this.ratio=inputRate/outputRate;this.tail=new Float32Array(0);this.position=0;}
  push(input){
    const source=new Float32Array(this.tail.length+input.length);source.set(this.tail);source.set(input,this.tail.length);
    const output=[];
    while(this.position<source.length-1){const i=Math.floor(this.position),part=this.position-i;output.push(source[i]*(1-part)+source[i+1]*part);this.position+=this.ratio;}
    const consumed=Math.min(Math.floor(this.position),source.length);this.tail=source.slice(consumed);this.position-=consumed;
    return Float32Array.from(output);
  }
}

export function pcm16(samples){
  const buffer=new ArrayBuffer(samples.length*2),view=new DataView(buffer);
  for(let i=0;i<samples.length;i++){const sample=Math.max(-1,Math.min(1,Number.isFinite(samples[i])?samples[i]:0));view.setInt16(i*2,sample<0?sample*32768:sample*32767,true);}
  return buffer;
}

export function unpackAudio(buffer){
  if(buffer.byteLength<10||(buffer.byteLength-8)%2)throw new Error('Invalid audio packet.');
  const view=new DataView(buffer),samples=new Float32Array((buffer.byteLength-8)/2);
  for(let i=0;i<samples.length;i++)samples[i]=view.getInt16(8+i*2,true)/32768;
  return {turn:view.getUint32(0),sequence:view.getUint32(4),samples};
}

export class PCMPlayback {
  constructor(context,send,onSpeaking,onIdle){this.context=context;this.send=send;this.onSpeaking=onSpeaking;this.onIdle=onIdle;this.sources=new Set();this.recent=[];this.turn=0;this.phrases=new Map();this.nextAck=0;this.nextTime=0;this.finished=false;this.rate=44100;this.prebufferSamples=Math.ceil(this.rate*.12);this.batchSamples=Math.ceil(this.rate*.06);}
  setRate(rate){
    if(![24000,44100].includes(rate))return false;
    this.clear();this.rate=rate;this.prebufferSamples=Math.ceil(rate*.12);this.batchSamples=Math.ceil(rate*.06);return true;
  }
  setTurn(turn){this.clear();this.turn=turn;}
  record(sequence){if(!this.phrases.has(sequence))this.phrases.set(sequence,{pending:0,done:false,acked:false,buffers:[],samples:0});return this.phrases.get(sequence);}
  push(buffer){
    const packet=unpackAudio(buffer);if(packet.turn!==this.turn)return;
    const record=this.record(packet.sequence);record.buffers.push(packet.samples);record.samples+=packet.samples.length;
    this.flush(packet.sequence);
  }
  flush(sequence,force=false){
    const record=this.record(sequence);
    // Build a small reserve before starting or recovering from an underrun. Tiny
    // network packets should not each become a source with a new 25 ms lead-in.
    const threshold=this.nextTime-this.context.currentTime>.025?this.batchSamples:this.prebufferSamples;
    if(!record.samples||(!force&&record.samples<threshold))return;
    const samples=new Float32Array(record.samples);let offset=0;
    for(const part of record.buffers){samples.set(part,offset);offset+=part.length;}
    record.buffers=[];record.samples=0;
    // Keep the model's native sample rate: buffering must not change voice speed
    // or pitch, and every final PCM sample must reach the audio device.
    const audio=this.context.createBuffer(1,samples.length,this.rate);
    audio.copyToChannel(samples,0);
    const source=this.context.createBufferSource();source.buffer=audio;source.connect(this.context.destination);
    const holder={source,cancelled:false,start:0,end:0};this.sources.add(holder);record.pending++;
    source.onended=()=>{this.sources.delete(holder);if(holder.cancelled)return;this.recent.push(holder);this.pruneReferences();record.pending--;this.acknowledge();};
    const when=Math.max(this.context.currentTime+.025,this.nextTime);this.nextTime=when+audio.duration;
    holder.start=when;holder.end=this.nextTime;
    source.start(when);this.onSpeaking();
  }
  pruneReferences(){this.recent=this.recent.filter(holder=>!holder.cancelled&&holder.end>=this.context.currentTime-.6);}
  referenceWindow(endTime=this.context.currentTime,duration=.6,rate=16000){
    if(!Number.isFinite(endTime)||!Number.isFinite(duration)||!Number.isInteger(rate)||rate<4000||rate>48000)return new Float32Array(0);
    duration=Math.max(0,Math.min(.6,duration));this.pruneReferences();
    const output=new Float32Array(Math.ceil(duration*rate)),start=endTime-duration;
    for(const holder of [...this.recent,...this.sources]){
      if(holder.cancelled||holder.start>=endTime||holder.end<=start)continue;
      const audio=holder.source.buffer,samples=audio.getChannelData?.(0)??audio.samples;
      if(!samples)continue;
      const first=Math.max(0,Math.ceil((holder.start-start)*rate)),last=Math.min(output.length,Math.ceil((holder.end-start)*rate));
      for(let index=first;index<last;index++){
        const position=(start+index/rate-holder.start)*this.rate,base=Math.floor(position);
        if(base<0||base>=samples.length)continue;
        const fraction=position-base;output[index]=samples[base]*(1-fraction)+(samples[Math.min(base+1,samples.length-1)]??0)*fraction;
      }
    }
    return output;
  }
  phraseDone(sequence){this.record(sequence).done=true;this.flush(sequence,true);this.acknowledge();}
  responseDone(){this.finished=true;this.acknowledge();}
  acknowledge(){
    while(this.phrases.has(this.nextAck)){
      const record=this.phrases.get(this.nextAck);if(!record.done||record.pending||record.samples||record.acked)break;
      record.acked=true;this.send({type:'played',turn_id:this.turn,seq:this.nextAck});this.nextAck++;
    }
    if(this.finished&&!this.sources.size&&[...this.phrases.values()].every(p=>p.acked)){
      this.finished=false;this.send({type:'playback_done',turn_id:this.turn});this.onIdle();
    }
  }
  clear(){
    for(const holder of this.sources){holder.cancelled=true;try{holder.source.stop();holder.source.disconnect();}catch(error){}}
    this.sources.clear();this.recent=[];this.phrases.clear();this.nextAck=0;this.nextTime=0;this.finished=false;this.turn=0;
  }
}
