const TIME_LIMITS={speech_end_to_first_audio_ms:3600000,endpoint_ms:15000,stt_ms:60000,
  director_ms:60000,context_check_ms:60000,llm_total_ms:300000,llm_first_token_ms:300000,
  tts_first_chunk_ms:600000,voice_synthesis_ms:600000,voice_verification_ms:120000,
  voice_model_first_audio_ms:120000};
const ENDPOINTS={smart_turn:'Smart Turn',pause_limit:'Pause limit',max_duration:'15-second limit',endpoint_timeout:'Turn check timeout'};
const PHASES={generating:'Generating voice… · Mic paused',checking:'Checking speech… · Mic paused',
  retrying:'Retrying voice… · Mic paused',ready:'Voice ready… · Mic paused'};
const RANK={generating:0,checking:1,ready:2};
const turn=value=>Number.isSafeInteger(value)&&value>0&&value<=0xffffffff;
const counter=(value,maximum)=>Number.isSafeInteger(value)&&value>=0&&value<=maximum?value:null;

export function formatVoiceMetrics(data={}){
  if(!data||typeof data!=='object')return '';
  const result=[],time=key=>counter(data[key],TIME_LIMITS[key]);
  const add=(label,value,suffix='')=>{if(value!==null)result.push(`${label}: ${value} ms${suffix}`);};
  if(data.voice_failed===true)result.push('Voice failed');
  else if(data.voice_retry===true)result.push('Voice retry');
  if(data.voice_pipeline==='drama-stream-v1')result.push('Drama 3 streaming');
  add('Server to audio delivery',time('speech_end_to_first_audio_ms'));
  add('Final pause',time('endpoint_ms'));
  add('Recognition wait',time('stt_ms'),data.streaming_asr===true?' (streaming)':data.recognition_prepared===true?' (started during pause)':'');
  if(typeof data.endpoint_reason==='string'&&Object.hasOwn(ENDPOINTS,data.endpoint_reason))result.push(`Endpoint: ${ENDPOINTS[data.endpoint_reason]}`);
  add('Director',time('director_ms')??time('context_check_ms'));
  add('Reply text',time('llm_total_ms')??time('llm_first_token_ms'));
  const synthesis=time('voice_synthesis_ms'),verification=time('voice_verification_ms');
  add('Voice generation',synthesis);add('Speech checking',verification);
  const attempts=counter(data.voice_attempts,2);
  if(attempts!==null&&attempts>0)result.push(`Voice attempts: ${attempts}`);
  add('Model first bytes',time('voice_model_first_audio_ms'));
  if(synthesis===null&&verification===null)add('Voice preparation',time('tts_first_chunk_ms'));
  return result.join(' · ');
}

// This controller changes only text/status/timing. Actual PCM playback owns
// the Speaking state and microphone release after all audio has finished.
export class VoiceProgress {
  constructor({onState=()=>{},onTiming=()=>{},onText=()=>{}}={}){
    this.onState=onState;this.onTiming=onTiming;this.onText=onText;
    this.turnId=0;this.active=false;this.attempt=0;this.rank=-1;
  }
  begin(turnId){
    if(!turn(turnId)||turnId<this.turnId)return false;
    this.turnId=turnId;this.active=true;this.attempt=0;this.rank=-1;this.onTiming('');return true;
  }
  clear(){this.turnId=0;this.active=false;this.attempt=0;this.rank=-1;this.onTiming('');}
  finish(turnId=this.turnId){
    if(turnId!==this.turnId)return false;
    this.active=false;return true;
  }
  accept(event){
    if(!event||!turn(event.turn_id)||event.turn_id!==this.turnId)return false;
    // Failure metrics can arrive just after the error event. They still belong
    // to this turn, but can never replace a later turn's timings.
    if(event.type==='metrics'){this.onTiming(formatVoiceMetrics(event));return true;}
    if(!this.active)return false;
    if(event.type==='text_done'){
      if(typeof event.text!=='string'||!event.text.trim()||event.text.length>16000)return false;
      this.onText(event.text);return true;
    }
    if(event.type!=='voice_progress'||typeof event.stage!=='string'||!Object.hasOwn(PHASES,event.stage)
        ||!Number.isInteger(event.attempt)||event.attempt<1||event.attempt>2||event.attempt<this.attempt)return false;
    if(event.attempt>this.attempt){this.attempt=event.attempt;this.rank=-1;}
    if(event.stage==='retrying')this.rank=-1;
    else{
      if(RANK[event.stage]<this.rank)return false;
      this.rank=RANK[event.stage];
    }
    this.onState(PHASES[event.stage]);return true;
  }
}
