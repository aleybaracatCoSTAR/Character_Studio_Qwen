class LiveCapture extends AudioWorkletProcessor {
  constructor(){super();this.size=Math.round(sampleRate*.08);this.buffer=new Float32Array(this.size);this.offset=0;}
  process(inputs){
    const channels=inputs[0];if(!channels?.length)return true;
    for(let i=0;i<channels[0].length;i++){
      let value=0;for(const channel of channels)value+=channel[i]/channels.length;
      this.buffer[this.offset++]=value;
      if(this.offset===this.size){this.port.postMessage(this.buffer,[this.buffer.buffer]);this.buffer=new Float32Array(this.size);this.offset=0;}
    }
    return true;
  }
}
registerProcessor('live-capture',LiveCapture);
