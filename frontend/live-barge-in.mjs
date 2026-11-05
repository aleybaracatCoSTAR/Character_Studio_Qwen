import {pcm16} from './live-audio.mjs';
const RATE=16000,FRAME=320,PREROLL=10;
const join=parts=>{const output=new Float32Array(parts.reduce((sum,item)=>sum+item.length,0));let offset=0;for(const part of parts){output.set(part,offset);offset+=part.length;}return output;};
const rms=samples=>Math.sqrt(samples.reduce((sum,value)=>sum+value*value,0)/Math.max(1,samples.length));
const reduced=samples=>{const output=new Float32Array(Math.floor(samples.length/4));for(let i=0;i<output.length;i++)output[i]=(samples[4*i]+samples[4*i+1]+samples[4*i+2]+samples[4*i+3])/4;return output;};

export function echoCorrelation(microphone,reference){
  const mic=reduced(microphone),ref=reduced(reference);
  if(!mic.length||ref.length<mic.length||rms(ref)<.003)return 0;
  const mean=mic.reduce((sum,value)=>sum+value,0)/mic.length;
  let energy=0;for(const value of mic)energy+=(value-mean)**2;
  if(energy<1e-8)return 0;
  const sums=new Float64Array(ref.length+1),squares=new Float64Array(ref.length+1);
  for(let i=0;i<ref.length;i++){sums[i+1]=sums[i]+ref[i];squares[i+1]=squares[i]+ref[i]*ref[i];}
  let best=0;
  for(let offset=0;offset+mic.length<=ref.length;offset++){
    const sum=sums[offset+mic.length]-sums[offset],variance=squares[offset+mic.length]-squares[offset]-sum*sum/mic.length;
    if(variance<1e-8)continue;
    let dot=0;for(let i=0;i<mic.length;i++)dot+=(mic[i]-mean)*ref[offset+i];
    best=Math.max(best,Math.abs(dot)/Math.sqrt(energy*variance));if(best>=.98)break;
  }
  return Math.min(1,best);
}

// A sustained near-end activity heuristic after browser AEC. Strong matches
// with the actual audible output are vetoed; this is not a speech recognizer.
export class SpeechBargeDetector {
  constructor(){this.noise=.002;this.reset();}
  reset(){this.pending=new Float32Array(0);this.preroll=[];this.echoHistory=[];this.candidate=null;this.voiced=0;this.gaps=0;}
  feed(samples,{reference=new Float32Array(0)}={}){
    if(!(samples instanceof Float32Array)||samples.length>8192)return null;
    this.pending=join([this.pending,samples]);let cursor=0;
    while(cursor+FRAME<=this.pending.length){
      const frame=this.pending.slice(cursor,cursor+FRAME);cursor+=FRAME;
      let crossings=0;for(let i=1;i<frame.length;i++)if((frame[i]>=0)!==(frame[i-1]>=0))crossings++;
      const level=rms(frame),ratio=crossings/frame.length;
      const active=Number.isFinite(level)&&level>=Math.max(.012,Math.min(.04,this.noise*3))&&ratio>=.004&&ratio<=.35;
      this.echoHistory.push(frame);if(this.echoHistory.length>5)this.echoHistory.shift();
      const echo=this.echoHistory.length>=4&&echoCorrelation(join(this.echoHistory),reference)>=.78;
      const speech=active&&!echo;
      if(!active&&level<.02)this.noise=this.noise*.96+level*.04;
      if(!this.candidate){
        if(speech){this.candidate=[...this.preroll,frame];this.prefix=this.preroll.length;this.voiced=1;this.gaps=0;}
        else{this.preroll.push(frame);if(this.preroll.length>PREROLL)this.preroll.shift();}
      }else{
        this.candidate.push(frame);this.voiced+=Number(speech);this.gaps=speech?0:this.gaps+1;
        const count=this.candidate.length-this.prefix;
        if(this.voiced>=12){const onset=join([...this.candidate,this.pending.slice(cursor)]);this.reset();return onset;}
        if(this.gaps>3||count>=15){this.preroll=this.candidate.slice(-PREROLL);this.candidate=null;this.voiced=0;this.gaps=0;}
      }
    }
    this.pending=this.pending.slice(cursor);return null;
  }
}

export class BargeInController {
  constructor({send,onStop=()=>{},onAccepted=()=>{},onRejected=()=>{},reference=()=>new Float32Array(0),
      setTimer=setTimeout,clearTimer=clearTimeout}={}){
    this.send=send;this.onStop=onStop;this.onAccepted=onAccepted;this.onRejected=onRejected;this.reference=reference;
    this.setTimer=setTimer;this.clearTimer=clearTimer;this.detector=new SpeechBargeDetector();this.enabled=false;this.permitted=false;this.reset();
  }
  reset(){this.cancel();this.turnId=0;this.suppressed=0;this.latched=false;}
  cancel(){if(this.timer)this.clearTimer(this.timer);this.timer=null;this.pending=null;this.detector.reset();}
  begin(turnId){
    if(!Number.isSafeInteger(turnId)||turnId<=0)return false;
    if(turnId!==this.turnId){this.reset();this.turnId=turnId;}else this.cancel();return true;
  }
  configure({enabled=this.enabled,permitted=this.permitted}={}){
    this.enabled=enabled===true;this.permitted=permitted===true;if(!this.enabled||!this.permitted)this.cancel();
  }
  isSuppressed(turnId){return turnId>0&&turnId===this.suppressed;}
  feed(samples){
    if(!(samples instanceof Float32Array)||!this.enabled||!this.permitted||!this.turnId)return false;
    if(this.pending){
      if(this.pending.samples.length+samples.length>RATE*2){this.reject();return true;}
      this.pending.samples=join([this.pending.samples,samples]);return true;
    }
    if(this.latched)return false;
    const onset=this.detector.feed(samples,{reference:this.reference()});if(!onset)return false;
    this.latched=true;this.suppressed=this.turnId;this.pending={turnId:this.turnId,samples:onset};
    this.onStop(this.turnId);this.send({type:'barge_in',turn_id:this.turnId});
    const turnId=this.turnId;this.timer=this.setTimer(()=>{if(this.pending?.turnId===turnId)this.reject();},1200);return true;
  }
  reject(){
    if(!this.pending)return false;
    const turnId=this.pending.turnId;this.cancel();this.onRejected(turnId);return true;
  }
  acknowledge(event){
    if(event?.type!=='barge_in_ready'||!this.pending||event.turn_id!==this.pending.turnId||typeof event.accepted!=='boolean')return false;
    if(!event.accepted)return this.reject();
    const {turnId,samples}=this.pending;this.cancel();
    for(let offset=0;offset<samples.length;offset+=640)this.send(pcm16(samples.subarray(offset,offset+640)));
    this.onAccepted(turnId);return true;
  }
}
