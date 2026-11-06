import {characterProfile} from './live-characters.mjs';
import {downloadViaServer} from './live-inspection-export.mjs';

export const ENGINE_LABELS={baseline:'Current · Qwen + Drama 3','qwen-omni':'Streaming · Qwen3-Omni'};
const validEngine=value=>Object.hasOwn(ENGINE_LABELS,value);
const turn=value=>Number.isSafeInteger(value)&&value>0&&value<=0xffffffff;
const text=(value,maximum)=>typeof value==='string'?value.slice(0,maximum):'';
const copied=value=>JSON.parse(JSON.stringify(value));
const TIME_KEYS=['first_text_ms','first_audio_ms','speech_end_to_first_audio_ms','endpoint_ms','stt_ms','director_ms','archive_ms','complete_ms'];
const boundedTime=value=>Number.isSafeInteger(value)&&value>=0&&value<=3600000?value:null;
const stable=value=>JSON.stringify(value,(_,item)=>item&&typeof item==='object'&&!Array.isArray(item)
  ?Object.fromEntries(Object.keys(item).sort().map(key=>[key,item[key]])):item);

export function comparisonProfile(profile,id='current-character'){
  const value=characterProfile(profile,id);if(!value)return null;
  // Authored fields only: the library normalizer omits uploaded audio, its
  // transcript, connection addresses, credentials and model request headers.
  return stable(value).length<=64000?copied(value):null;
}
export function comparableReplies(first,second){
  return !!first&&!!second&&first.engine!==second.engine&&first.character_id===second.character_id
    &&first.input_kind===second.input_kind&&first.prompt===second.prompt
    &&stable(first.settings)===stable(second.settings)&&stable(first.prior_context)===stable(second.prior_context)
    &&first.status==='complete'&&second.status==='complete'&&first.playback_complete===true&&second.playback_complete===true;
}
export function comparisonMetrics(event){
  return Object.fromEntries(TIME_KEYS.filter(key=>boundedTime(event?.[key])!==null).map(key=>[key,event[key]]));
}

export class EngineComparison {
  constructor({document,onChange=()=>{},saveFile=file=>downloadViaServer(document,file),clock=()=>performance.now()}={}){
    this.document=document;this.onChange=onChange;this.saveFile=saveFile;this.clock=clock;
    this.supported=false;this.engines={};this.selected='baseline';this.busy=false;this.initialized=false;
    this.call=null;this.draft=null;this.results=[];this.characterId='current-character';this.characterName='your character';this.exportEpoch=0;
    this.get('comparison-engine').addEventListener('change',()=>{
      const next=this.get('comparison-engine').value;
      if(this.busy||!validEngine(next)||!this.engines[next]?.available){this.get('comparison-engine').value=this.selected;return;}
      this.selected=next;this.renderControls();this.onChange(next);
    });
    this.get('open-comparison').addEventListener('click',()=>{this.renderResults();this.get('comparison-dialog').showModal();});
    this.get('close-comparison').addEventListener('click',()=>this.get('comparison-dialog').close());
    this.get('download-comparison').addEventListener('click',()=>void this.download());
    this.get('clear-comparison').addEventListener('click',()=>this.clearResults());
    this.renderControls();
  }
  get(id){return this.document.getElementById(id);}
  engine(){return this.selected;}
  startFields(){return this.supported?{engine:this.selected}:{};}
  isReady(){return this.supported?this.engines[this.selected]?.ready===true:null;}
  setCapabilities(value){
    if(!value||typeof value!=='object'||!value.engines){
      this.supported=false;this.engines={};
      // An older server receives the original start message, never a silently
      // substituted streaming test. A running call retains its captured engine.
      if(!this.busy){this.selected='baseline';this.initialized=false;}
      this.renderControls();return;
    }
    this.supported=true;this.engines=Object.fromEntries(Object.keys(ENGINE_LABELS).map(id=>[id,
      {available:value.engines[id]?.available===true,ready:value.engines[id]?.available===true&&value.engines[id]?.ready===true,
       voice_playback:value.engines[id]?.voice_playback==='streaming'?'streaming':'checked'}]));
    if(!this.initialized&&!this.busy){this.selected=validEngine(value.default_engine)&&this.engines[value.default_engine]?.available?value.default_engine:'baseline';this.initialized=true;}
    this.renderControls();
  }
  setBusy(value){this.busy=!!value;this.renderControls();}
  setCharacter(id,name){
    if(typeof id!=='string')return;
    const changed=id!==this.characterId;this.characterId=id;this.characterName=text(name,80)||'your character';
    if(changed)this.exportEpoch++;if(this.get('comparison-dialog').open)this.renderResults();
  }
  startProblem(voice){
    if(this.selected==='qwen-omni'&&voice?.preset==='custom')return 'Streaming Qwen3-Omni uses built-in voices. Choose Female, Male or Automatic before starting; uploaded voice references are not supported.';
    if(this.supported&&!this.isReady())return this.engines[this.selected]?.available
      ?'This voice engine is not ready. Start its GPU stack, wait for it to load, then try again.'
      :'This voice engine is not configured on this server. Start the matching comparison stack first.';
    return '';
  }
  renderControls(){
    this.get('engine-controls').hidden=!this.supported;
    const select=this.get('comparison-engine'),previous=this.selected;select.replaceChildren();
    for(const [id,label] of Object.entries(ENGINE_LABELS)){
      const option=this.document.createElement('option');option.value=id;
      option.textContent=label+(this.supported?this.engines[id]?.ready?' · ready':this.engines[id]?.available?' · not ready':' · not configured':'');
      if(id==='baseline'&&this.engines[id]?.voice_playback==='streaming')option.textContent+=' · live playback';
      option.disabled=id!=='baseline'&&this.engines[id]?.available!==true;select.append(option);
    }
    select.value=previous;select.disabled=this.busy||!this.supported;
    this.get('comparison-engine-note').textContent=this.isReady()?'Same saved character, director and archive settings. End the call to switch.':this.startProblem({})||'';
    this.get('engine-voice-note').hidden=this.selected!=='qwen-omni';
  }
  beginCall({id,profile,input='speech'}){
    const settings=comparisonProfile(profile,id);if(!settings)return false;
    this.setCharacter(id,settings.character.name);this.call={engine:this.selected,id,settings,input:input==='text'?'text':'speech',history:[],turns:0};
    this.draft=null;return true;
  }
  ready(event){const expected=this.call?.engine??this.selected;return event.engine===undefined?expected==='baseline':event.engine===expected;}
  endCall(){
    if(this.draft&&!this.draft.playback_complete){if(this.draft.status!=='failed')this.draft.status='interrupted';this.publish();}
    this.call=null;this.draft=null;
  }
  accept(event){
    if(!this.call||!event||event.engine!==undefined&&event.engine!==this.call.engine)return false;
    if(event.type==='turn_start'){
      if(!turn(event.turn_id)||this.draft&&event.turn_id<this.draft.turn_id)return false;
      if(this.draft?.turn_id===event.turn_id){
        if(event.voice_retry===true){this.draft.voice_retry=true;this.draft.playback_complete=false;this.draft.server_complete=false;this.draft.status='retrying';this.draft.browser_audio_queued_ms=null;this.draft._started=this.clock();return true;}
        return false;
      }
      if(this.draft){if(!this.draft.playback_complete&&this.draft.status!=='failed')this.draft.status='interrupted';this.publish();
        this.call.history.push({prompt:this.draft.prompt,reply:this.draft.reply,status:this.draft.status});this.call.history=this.call.history.slice(-8);}
      this.draft={engine:this.call.engine,character_id:this.call.id,character_name:this.call.settings.character.name,settings:copied(this.call.settings),
        input_kind:this.call.input,turn_id:event.turn_id,turn_index:++this.call.turns,prompt:'',reply:'',heard_text:'',prior_context:copied(this.call.history),
        citations:[],metrics:{},director:null,status:'in_progress',server_complete:false,playback_complete:false,voice_verification:null,voice_retry:false,
        browser_audio_queued_ms:null,_started:this.clock()};return true;
    }
    const draft=this.draft;if(!draft||event.turn_id!==draft.turn_id)return false;
    if(event.type==='transcript'){draft.prompt=text(event.display_text??event.text,4000);if(event.source==='action')draft.input_kind='stage';return true;}
    if(event.type==='text_delta'){draft.reply=(draft.reply+text(event.text,16000)).slice(0,16000);return true;}
    if(event.type==='text_done'){if(typeof event.text==='string')draft.reply=text(event.text,16000);return true;}
    if(event.type==='archive_sources'){
      draft.citations=[...new Set((Array.isArray(event.matches)?event.matches:[]).slice(0,3).map(match=>text(match?.citation_id,200)
        ||[text(match?.collection_id,64),text(match?.source_entry_id??match?.id,128)].filter(Boolean).join(':')).filter(Boolean))];return true;
    }
    if(event.type==='director_note'){
      draft.director=event.ok?Object.fromEntries(['intention','emotion','delivery'].map(key=>[key,text(event[key],600)])):null;return true;
    }
    if(event.type==='metrics'){
      draft.metrics={...draft.metrics,...comparisonMetrics(event)};draft.voice_pipeline=text(event.voice_pipeline,80);
      if(typeof event.voice_verification==='boolean')draft.voice_verification=event.voice_verification;
      if(event.voice_failed===true)draft.status='failed';this.publish();return true;
    }
    if(event.type==='response_done'){
      if(typeof event.text==='string')draft.reply=text(event.text,16000);draft.server_complete=true;
      if(draft.status!=='failed')draft.status=event.partial?'interrupted':draft.playback_complete?'complete':'audio_pending';this.publish();return true;
    }
    if(event.type==='interrupted'||event.type==='error'){
      draft.status=event.type==='error'?'failed':'interrupted';draft.heard_text=text(event.heard,16000);this.publish();return true;
    }
    return false;
  }
  audioQueued(turnId){
    if(!this.draft||this.draft.turn_id!==turnId||this.draft.browser_audio_queued_ms!==null)return false;
    this.draft.browser_audio_queued_ms=Math.max(0,Math.min(3600000,Math.round(this.clock()-this.draft._started)));this.publish();return true;
  }
  playbackComplete(turnId){
    if(!this.draft||this.draft.turn_id!==turnId||!this.draft.server_complete||['failed','interrupted'].includes(this.draft.status))return false;
    this.draft.playback_complete=true;this.draft.status='complete';this.publish();return true;
  }
  publish(){
    const draft=this.draft;if(!draft||!draft.prompt||draft.status==='in_progress'||draft.status==='retrying')return;
    const {_started,...result}=draft;const key=stable([result.character_id,result.engine]);
    this.results=this.results.filter(item=>stable([item.character_id,item.engine])!==key);this.results.push(copied(result));this.results=this.results.slice(-12);
    if(this.get('comparison-dialog').open)this.renderResults();
  }
  pair(){
    return Object.fromEntries(Object.keys(ENGINE_LABELS).map(engine=>[engine,[...this.results].reverse().find(result=>result.character_id===this.characterId&&result.engine===engine)||null]));
  }
  renderResults(){
    const pair=this.pair(),matched=comparableReplies(pair.baseline,pair['qwen-omni']);
    this.get('comparison-summary').textContent=matched?'Same prompt, saved settings and prior context; both replies finished playback. Judge character consistency and factual grounding yourself.':'Use the same character, prompt and settings in two fresh calls. Speech and typed inputs are separate tests. Incomplete playback or different context is not a matched comparison.';
    const grid=this.get('comparison-results');grid.replaceChildren();
    for(const [engine,label] of Object.entries(ENGINE_LABELS)){
      const card=this.document.createElement('section'),heading=this.document.createElement('h3');card.className='comparison-result';heading.textContent=label;card.append(heading);
      const result=pair[engine];
      if(!result){const empty=this.document.createElement('p');empty.className='hint small';empty.textContent='No completed test for this character on this page yet.';card.append(empty);grid.append(card);continue;}
      const info=this.document.createElement('p');info.className='hint small';info.textContent=`${result.character_name} · ${result.input_kind} input · ${result.status.replaceAll('_',' ')}${result.voice_retry?' · voice retry':''}`;card.append(info);
      const rows=[['Server estimate to first text',result.metrics.first_text_ms],['Server estimate to first audio',result.metrics.first_audio_ms??result.metrics.speech_end_to_first_audio_ms],
        ['Recognition',result.metrics.stt_ms],['Archive retrieval',result.metrics.archive_ms],['Director',result.metrics.director_ms],['Server reply complete',result.metrics.complete_ms],
        ['Browser audio queued after turn start',result.browser_audio_queued_ms]];
      const list=this.document.createElement('dl');list.className='comparison-times';
      for(const [name,value] of rows){const term=this.document.createElement('dt'),description=this.document.createElement('dd');term.textContent=name;description.textContent=boundedTime(value)!==null?`${value} ms`:'Not measured';list.append(term,description);}card.append(list);
      for(const [title,value] of [['You',result.prompt],['Character transcript',result.reply],['Archive references',result.citations.join(', ')||'None supplied']]){
        const sub=this.document.createElement('h4'),paragraph=this.document.createElement('p');sub.textContent=title;paragraph.textContent=value;card.append(sub,paragraph);
      }
      grid.append(card);
    }
    this.get('download-comparison').disabled=!Object.values(pair).some(Boolean);this.get('clear-comparison').disabled=!this.results.length;
  }
  clearResults(){this.results=[];this.exportEpoch++;this.get('comparison-export-status').textContent='';this.renderResults();}
  async download(){
    const pair=this.pair();if(!Object.values(pair).some(Boolean))return false;
    const epoch=this.exportEpoch;this.get('download-comparison').disabled=true;
    const data={format:'character-studio-engine-comparison',version:1,character_id:this.characterId,matched:comparableReplies(pair.baseline,pair['qwen-omni']),
      note:'Manual comparison. Results stay in browser memory until export; explicit downloads use a temporary studio-server attachment that expires after two minutes. Timings are server estimates and browser queue timing, not measured speaker onset. Native streaming audio has no full-reply speech verification. Transcripts do not prove audible completeness. Voice audio and connection details are omitted.',results:Object.values(pair).filter(Boolean)};
    try{await this.saveFile({filename:'character-studio-engine-comparison.json',mime:'application/json',content:JSON.stringify(data,null,2)+'\n'});
      if(epoch===this.exportEpoch)this.get('comparison-export-status').textContent='Comparison download started.';return true;
    }catch(error){if(epoch===this.exportEpoch)this.get('comparison-export-status').textContent='The comparison could not be downloaded. Please try again.';return false;}
    finally{if(epoch===this.exportEpoch)this.renderResults();}
  }
}
