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
  constructor(context,send,onSpeaking,onIdle){this.context=context;this.send=send;this.onSpeaking=onSpeaking;this.onIdle=onIdle;this.sources=new Set();this.turn=0;this.phrases=new Map();this.nextAck=0;this.nextTime=0;this.finished=false;}
  setTurn(turn){this.clear();this.turn=turn;}
  record(sequence){if(!this.phrases.has(sequence))this.phrases.set(sequence,{pending:0,done:false,acked:false});return this.phrases.get(sequence);}
  push(buffer){
    const packet=unpackAudio(buffer);if(packet.turn!==this.turn)return;
    const record=this.record(packet.sequence),audio=this.context.createBuffer(1,packet.samples.length,44100);
    audio.copyToChannel(packet.samples,0);
    const source=this.context.createBufferSource();source.buffer=audio;source.connect(this.context.destination);
    const holder={source,cancelled:false};this.sources.add(holder);record.pending++;
    source.onended=()=>{this.sources.delete(holder);if(holder.cancelled)return;record.pending--;this.acknowledge();};
    const when=Math.max(this.context.currentTime+.025,this.nextTime);this.nextTime=when+audio.duration;
    source.start(when);this.onSpeaking();
  }
  phraseDone(sequence){this.record(sequence).done=true;this.acknowledge();}
  responseDone(){this.finished=true;this.acknowledge();}
  acknowledge(){
    while(this.phrases.has(this.nextAck)){
      const record=this.phrases.get(this.nextAck);if(!record.done||record.pending||record.acked)break;
      record.acked=true;this.send({type:'played',turn_id:this.turn,seq:this.nextAck});this.nextAck++;
    }
    if(this.finished&&!this.sources.size&&[...this.phrases.values()].every(p=>p.acked)){this.finished=false;this.onIdle();}
  }
  clear(){
    for(const holder of this.sources){holder.cancelled=true;try{holder.source.stop();holder.source.disconnect();}catch(error){}}
    this.sources.clear();this.phrases.clear();this.nextAck=0;this.nextTime=0;this.finished=false;this.turn=0;
  }
}
