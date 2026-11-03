import {StreamResampler,pcm16,PCMPlayback} from './live-audio.mjs';
import {Conversation} from './live-transcript.mjs';
import {StoryEditor} from './live-story.mjs';
import {PromptInspector} from './live-inspector.mjs';
import {VoiceRetry} from './live-voice-retry.mjs';
import {DirectorPanel} from './live-director-panel.mjs';
import {CallFeedback} from './live-feedback.mjs';
import {CharacterLibrary} from './live-characters.mjs';
import {ArchivePanel} from './live-archive.mjs';
import {ListeningFeedback} from './live-listening.mjs';
const $=id=>document.getElementById(id),form=$('character-form');
const archivePage=location.pathname.startsWith('/archive');
const fields=['name','role','about','goal','environment','voice_style','rules','memories','beat','speaker'];
let socket=null,context=null,stream=null,capture=null,playback=null,connecting=false,muted=false,ready=false,currentTurn=0;
let replyPending=false,resumeTimer=null;
let referenceURL=null;
let triggerActive=false;
let sessionReady=false,disconnecting=null;
let characterLibrary=null,profileStorage=null,loadingProfile=false;
let libraryHint='Each character has its own details, voice and story. End the call to create or switch.';
const conversation=new Conversation($('messages'),$('empty-state'));
const callFeedback=new CallFeedback($('feedback'));
const inspector=new PromptInspector({document,send});
const archivePanel=new ArchivePanel({document,onChange:()=>saveActiveProfile()});
const listeningFeedback=new ListeningFeedback({label:$('listening-mode'),partial:$('listening-partial'),
  onFinalizing:()=>{holdMicrophone();state('Finishing recognition… · Mic paused');},
  onListening:()=>{if(replyPending)resumeMicrophone();else state('Listening…');},
  onError:message=>{feedback(message);resumeMicrophone();}});
const directorPanel=new DirectorPanel({button:$('toggle-director'),content:$('director-content'),
  summary:$('director-summary'),empty:$('director-empty')});
const voiceRetry=new VoiceRetry({button:$('retry-voice'),send,
  canRetry:()=>sessionReady&&socket?.readyState===WebSocket.OPEN,
  onRetry:()=>{holdMicrophone();feedback();state('Retrying voice… · Mic paused');},
  onUnavailable:data=>{feedback(data.message);resumeMicrophone();},
  onError:data=>{feedback(data.message);playback?.clear();resumeMicrophone();}});
function directorSettings(){return {enabled:$('director-enabled').checked,notes:$('director-input').value};}
function triggerSettings(){return $('trigger-enabled').checked?{
  action:$('trigger-action').value,condition:$('trigger-condition').value,emotion:$('trigger-emotion').value,
  behaviour:$('trigger-behaviour').value,spoken_cue:$('trigger-cue').value,delivery:$('trigger-delivery').value
}:null;}
const replies=conversation.replies;
let storyView=null;
const storyEditor=new StoryEditor($('beat-editor'),saveStory);
function narrativeSettings(){return {enabled:$('story-enabled').checked,mode:$('story-mode').value,beats:storyEditor.values()};}
function saveStory(){
  $('story-settings').hidden=!$('story-enabled').checked;
  if(!socket)previewStory();
  saveActiveProfile();
}
function previewStory(){
  const settings=narrativeSettings();
  renderStory({enabled:settings.enabled&&settings.beats.length>0,index:0,count:settings.beats.length,
    titles:settings.beats.map(beat=>beat.title),current:settings.beats[0],mode:settings.mode,elapsed:0,preview:true});
}
function renderStory(data){
  storyView=data;directorPanel.updateStory(data);$('story-stage').hidden=!data.enabled;
  if(!data.enabled)return;
  $('story-title').textContent=data.finished?'Story complete':`${data.index+1}/${data.count} · ${data.current.title}`;
  $('story-clock').textContent=data.preview?'Script preview':data.finished?'Finished':`${data.paused?'Held · ':''}${Math.floor(data.elapsed)} s · ${data.current.min_seconds}–${data.current.max_seconds} s`;
  $('story-goal').textContent=data.current.goal;$('story-entry').textContent=data.current.note;
  $('beat-progress').replaceChildren();
  data.titles.forEach((title,index)=>{const item=document.createElement('li');item.textContent=String(index+1);item.title=title;item.className=index===data.index?'current':index<data.index?'past':'';
    if(index===data.index)item.setAttribute('aria-current','step');$('beat-progress').append(item);});
  $('hold-story').textContent=data.paused?'Resume':'Hold';$('next-beat').textContent=data.index===data.count-1?'Finish story':'Next beat →';
  $('story-reason').textContent=data.preview?'Start a call to run the arc.':data.pending?'Beat change queued until this reply finishes playing.':`${data.mode} · ${data.reason}${data.evidence?' · '+data.evidence:''}${data.director_note?' · '+data.director_note:''}`;
  storyControls();
}
function storyControls(){
  const live=sessionReady&&socket?.readyState===WebSocket.OPEN&&storyView?.enabled;
  $('previous-beat').disabled=!live||(!storyView.index&&!storyView.finished);
  $('next-beat').disabled=!live||storyView.finished;
  $('hold-story').disabled=!live||storyView.finished;
  $('cue-beat').disabled=!live||storyView.finished||replyPending;
}
function character(){return Object.fromEntries(fields.map(key=>[key,form.elements[key].value]));}
function capturedProfile(){return {character:character(),voice:{preset:$('voice-preset').value,...delivery()},
  director:directorSettings(),trigger:triggerSettings(),narrative:narrativeSettings(),archive:archivePanel.settings()};}
function libraryNotice(){
  $('character-library-note').textContent=libraryHint+(characterLibrary?.storageError?' Changes are kept for this page, but this browser could not save them.':'');
}
function characterOptions(){
  if(!characterLibrary)return;
  const picker=$('character-picker');picker.replaceChildren();
  for(const profile of characterLibrary.list()){
    const option=document.createElement('option');option.value=profile.id;option.textContent=profile.name||'Untitled character';picker.append(option);
  }
  picker.value=characterLibrary.activeId;
  $('load-story-example').hidden=!characterLibrary.preset()?.narrative.beats.length;
  libraryNotice();
}
function saveActiveProfile(){
  if(!characterLibrary||loadingProfile)return;
  characterLibrary.save(capturedProfile());characterLibrary.writeLegacy();characterOptions();
}
function clearReference(){
  $('voice-reference').value='';$('reference-text').value='';$('reference-audio').pause();$('reference-audio').removeAttribute('src');
  if(referenceURL)URL.revokeObjectURL(referenceURL);referenceURL=null;$('reference-preview').hidden=true;
}
function applyProfile(profile){
  loadingProfile=true;
  try{
    for(const key of fields)form.elements[key].value=profile.character[key]??'';
    for(const [key,id] of [['preset','voice-preset'],['emotion','voice-emotion'],['event','voice-event'],['direction','voice-direction']])$(id).value=profile.voice[key];
    $('director-enabled').checked=profile.director.enabled;$('director-input').value=profile.director.notes;
    $('trigger-enabled').checked=!!profile.trigger;
    for(const [key,id] of [['action','trigger-action'],['condition','trigger-condition'],['emotion','trigger-emotion'],['behaviour','trigger-behaviour'],['spoken_cue','trigger-cue'],['delivery','trigger-delivery']])$(id).value=profile.trigger?.[key]??(key==='emotion'?'anxious':'');
    $('trigger-fields').hidden=!profile.trigger;$('test-trigger').textContent=profile.trigger?.action||'Test reaction';
    storyEditor.load(profile.narrative.beats);$('story-enabled').checked=profile.narrative.enabled;$('story-mode').value=profile.narrative.mode;
    $('story-settings').hidden=!profile.narrative.enabled;
    archivePanel.apply(profile.archive??{enabled:profile.id==='map-keeper',scope:profile.id==='map-keeper'?'library':'selected',entry_ids:[]});
    clearReference();updateVoicePreset();
    $('chat-title').textContent=`Talk to ${profile.character.name.trim()||'your character'}`;
    clearTimeout(resumeTimer);replyPending=false;currentTurn=0;triggerActive=false;
    playback?.clear();conversation.reset();inspector.reset();voiceRetry.clear();directorPanel.resetNote();
    if($('model-inspector').open)$('model-inspector').close();
    $('director-latest').hidden=true;$('director-note').textContent='';$('timing').textContent='';
    $('typed-form').hidden=true;$('typed-message').value='';$('trigger-state').textContent='Start a call to test the reaction.';
    feedback();state('Ready when you are');previewStory();characterLibrary.writeLegacy();characterOptions();controls();
  }finally{loadingProfile=false;}
}
async function loadCharacterLibrary(){
  let presets=[];
  try{
    const response=await fetch('/api/characters/presets');if(!response.ok)throw new Error('Presets unavailable.');
    const data=await response.json();if(!Array.isArray(data))throw new Error('Presets unavailable.');presets=data;
  }catch(error){libraryHint='Your saved characters are available. Built-in examples could not be loaded.';}
  try{profileStorage=localStorage;}catch(error){}
  let savedArchive=false;
  if(archivePage){try{savedArchive=!!profileStorage?.getItem('live-archive-character-library-v1');}catch(error){}}
  characterLibrary=new CharacterLibrary({storage:profileStorage,current:capturedProfile(),presets,
    ...(archivePage?{key:'live-archive-character-library-v1',legacyWrites:false}:{})});
  if(archivePage&&!savedArchive&&characterLibrary.preset('map-keeper'))characterLibrary.switchTo('map-keeper');
  applyProfile(characterLibrary.active());
  if(archivePage)$('archive-settings').open=true;
}
function feedback(message='',kind='general'){callFeedback.show(message,kind);}
function state(message){$('call-state').textContent=message;}
function send(value){if(socket?.readyState===WebSocket.OPEN)socket.send(JSON.stringify(value));}
function delivery(){return {emotion:$('voice-emotion').value,event:$('voice-event').value,direction:$('voice-direction').value};}
async function voiceSettings(){
  const settings={...delivery(),preset:$('voice-preset').value},file=$('voice-reference').files[0];
  if(settings.preset!=='custom')return settings;
  if(!file)throw new Error('Upload a WAV reference, or choose Female, Male or Automatic.');
  if(file.size>2*1024*1024)throw new Error('Choose a voice reference smaller than 2 MB.');
  const transcript=$('reference-text').value.trim();
  if(!transcript)throw new Error('Enter the exact words spoken in the voice reference.');
  const encoded=await new Promise((resolve,reject)=>{
    const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);
    reader.onerror=()=>reject(new Error('Could not read the voice reference.'));reader.readAsDataURL(file);
  });
  return {...settings,reference_audio:`data:audio/wav;base64,${encoded}`,reference_text:transcript};
}
function holdMicrophone(){voiceRetry.clear();clearTimeout(resumeTimer);replyPending=true;controls();}
function resumeMicrophone(){
  clearTimeout(resumeTimer);
  resumeTimer=setTimeout(()=>{
    replyPending=false;
    state(muted?'Microphone muted':stream?'Listening…':'Ready for your next message');controls();
  },350);
}
function controls(){
  const connected=socket?.readyState===WebSocket.OPEN;
  $('character-picker').disabled=!!connected||connecting||!characterLibrary;
  $('new-character').disabled=!!socket||connecting||!!disconnecting||!characterLibrary;
  $('start-call').disabled=connecting||(!connected&&!ready);$('start-call').classList.toggle('connected',!!connected);
  $('start-call').querySelector('span').textContent=connected?'End session':connecting?'Connecting…':'Start call';
  $('mute').hidden=!connected||!stream;$('interrupt').hidden=!connected;
  $('interrupt').disabled=!replyPending;
  $('text-mode').hidden=!!connected;$('text-mode').disabled=connecting||!ready;
  $('send-text').disabled=!connected||!sessionReady;
  for(const element of form.elements)element.disabled=element.dataset.liveSetting?connecting:!!connected||connecting;
  archivePanel.setBusy(!!connected||connecting);
  $('test-trigger').disabled=!connected||!sessionReady||!$('trigger-enabled').checked;
  $('clear-trigger').disabled=!connected||!sessionReady||!triggerActive;
  document.querySelector('.live-badge').classList.toggle('active',!!connected);storyControls();
}
function message(speaker,text,kind='assistant'){return conversation.message(speaker,text,kind);}
async function disconnect(){
  if(disconnecting)return disconnecting;
  const previous=socket,oldContext=context,oldStream=stream,oldCapture=capture,oldPlayback=playback;
  socket=null;context=null;stream=null;capture=null;playback=null;connecting=true;
  inspector.setConnected(false);
  archivePanel.resetSources();
  listeningFeedback.reset();
  voiceRetry.clear();
  callFeedback.clearVoiceNotice();
  clearTimeout(resumeTimer);replyPending=false;sessionReady=false;triggerActive=false;controls();
  $('trigger-state').textContent='Start a call to test the reaction.';
  if(previous){if(previous.readyState===WebSocket.OPEN)previous.send(JSON.stringify({type:'stop'}));previous.close();}
  oldStream?.getTracks().forEach(track=>track.stop());
  if(oldCapture){oldCapture.port.onmessage=null;try{oldCapture.disconnect();}catch(error){}}
  oldPlayback?.clear();
  disconnecting=(async()=>{if(oldContext){try{await oldContext.close();}catch(error){}}})()
    .finally(()=>{disconnecting=null;connecting=false;muted=false;$('mute').textContent='Mute mic';state('Call ended');controls();});
  return disconnecting;
}
function event(data){
  if(data.type==='listening_state'||data.type==='listening_partial'){listeningFeedback.accept(data);return;}
  if(data.type==='model_inspection'){inspector.accept(data);return;}
  if(data.type==='archive_sources'){archivePanel.showSources(data);return;}
  if(data.type==='story_state'){renderStory(data);return;}
  if(data.type==='story_decision'){$('story-reason').textContent=`Director held this beat: ${data.reason}`;return;}
  if(data.type==='director_ack'){feedback(data.message);return;}
  if(data.type==='director_note'){
    if(data.turn_id!==currentTurn)return;
    $('director-note').textContent=data.ok?`Intention: ${data.intention}\nVoice: ${data.emotion} · ${data.delivery}${data.evidence?'\nEmotion evidence: '+data.evidence:''}${data.beat_evidence?'\nBeat evidence: '+data.beat_evidence:''}`:data.message;
    $('director-latest').hidden=false;directorPanel.noteAvailable();state('Thinking… · Mic paused');return;
  }
  if(data.type==='trigger_state'){
    triggerActive=data.active;$('trigger-state').textContent=(data.active?`Reaction active · ${data.emotion}`:'Normal state restored')+(data.evidence?` · ${data.evidence}`:'');controls();return;
  }
  if(data.type==='trigger_check'){
    if(!data.ok)$('trigger-state').textContent=data.message;
    else if(data.action==='hold')$('trigger-state').textContent=(triggerActive?'Reaction continues':'Normal state')+(data.evidence?` · ${data.evidence}`:'');
    state('Thinking… · Mic paused');return;
  }
  if(data.type==='ready'){listeningFeedback.setMode(data.listening_mode);sessionReady=true;inspector.setConnected(true);state(stream?'Listening…':'Ready for a typed message');controls();return;}
  if(data.type==='speech_start'){state('Listening…');return;}
  if(data.type==='listening'){state('Listening…');return;}
  if(data.type==='turn_start'){listeningFeedback.clear();callFeedback.clearVoiceNotice();holdMicrophone();currentTurn=data.turn_id;archivePanel.setTurn(currentTurn);playback.setTurn(currentTurn);state(data.voice_retry?'Retrying voice… · Mic paused':'Understanding… · Mic paused');return;}
  if(data.type==='interrupted'){
    if(data.turn_id===currentTurn){voiceRetry.clear();playback.clear();resumeMicrophone();const reply=replies.get(data.turn_id);if(reply){reply.content.textContent=data.heard||'Reply stopped';reply.node.classList.add('interrupted');}}
    return;
  }
  if(data.type==='error'){voiceRetry.handleError(data,currentTurn);return;}
  if(data.type==='voice_retry_unavailable'){voiceRetry.handleUnavailable(data,currentTurn);return;}
  if(data.type==='empty_transcript'){feedback('I did not catch that. Try again.');resumeMicrophone();return;}
  if(data.turn_id!==currentTurn)return;
  if(data.type==='voice_notice'){feedback(data.message,'voice_notice');return;}
  if(data.type==='phrase_start'){state('Preparing the whole reply… · Mic paused');return;}
  if(data.type==='transcript'){message(data.source==='action'?'Stage action':'You',data.display_text??data.text,'user');state($('director-enabled').checked?'Directing the next reply… · Mic paused':$('trigger-enabled').checked?'Reading the situation… · Mic paused':'Thinking… · Mic paused');}
  if(data.type==='text_delta'){
    if(!replies.has(currentTurn))replies.set(currentTurn,message(character().name,''));
    replies.get(currentTurn).content.textContent+=data.text;$('messages').scrollTop=$('messages').scrollHeight;
  }
  if(data.type==='phrase_done')playback.phraseDone(data.seq);
  if(data.type==='response_done'){
    callFeedback.clearVoiceNotice();
    voiceRetry.clear();
    if(data.partial&&replies.has(currentTurn)){
      const reply=replies.get(currentTurn);reply.content.textContent=data.text;reply.node.classList.add('interrupted');
    }
    playback.responseDone();
  }
  if(data.type==='metrics')$('timing').textContent=`Server estimate to first audio: ${data.speech_end_to_first_audio_ms} ms · Final pause: ${data.endpoint_ms} ms · Recognition wait: ${data.stt_ms} ms${data.streaming_asr?' (Nemotron streaming)':data.recognition_prepared?' (started during pause)':''}${data.endpoint_reason?' · Endpoint: '+data.endpoint_reason:''} · Director: ${data.director_ms??data.context_check_ms??0} ms · Reply text: ${data.llm_total_ms??data.llm_first_token_ms} ms · Complete voice take: ${data.tts_first_chunk_ms} ms`;
}
async function connect(useMic=true){
  if(connecting)return;if(socket){await disconnect();return;}if(!form.reportValidity())return;
  if(archivePanel.pending){feedback('Wait for the archive story to finish saving or loading, then start the call.');return;}
  const outline=narrativeSettings();
  if(outline.enabled&&(!outline.beats.length||outline.beats.some(beat=>!beat.title.trim()||!beat.goal.trim()||!Number.isFinite(beat.min_seconds)||!Number.isFinite(beat.max_seconds)||beat.max_seconds<=beat.min_seconds))){
    feedback('Give every beat a title and goal, with a maximum time greater than its minimum.');return;
  }
  connecting=true;feedback();controls();
  try{
    conversation.reset();inspector.reset();archivePanel.resetSources();listeningFeedback.reset();voiceRetry.clear();directorPanel.resetNote();currentTurn=0;$('timing').textContent='';$('director-latest').hidden=true;
    context=new AudioContext();await context.resume();
    let voice;
    try{voice=await voiceSettings();}catch(error){feedback(error.message);await disconnect();return;}
    playback=new PCMPlayback(context,send,()=>state('Speaking… · Mic paused'),resumeMicrophone);
    if(useMic){
      stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,channelCount:1}});
      await context.audioWorklet.addModule('/static/live-capture-worklet.js');
    }
    const url=new URL('/api/live',location.href);url.protocol=location.protocol==='https:'?'wss:':'ws:';
    const opened=new WebSocket(url);socket=opened;opened.binaryType='arraybuffer';
    opened.onmessage=received=>{if(socket!==opened)return;try{if(typeof received.data==='string')event(JSON.parse(received.data));else playback?.push(received.data);}catch(error){feedback('Audio connection failed. End the call and reconnect.');}};
    opened.onerror=()=>{if(socket===opened)feedback('Cannot connect to the live server. Check its terminal and SSH tunnel.');};
    opened.onclose=async()=>{if(socket===opened){feedback('Live connection closed. Check that all three services are ready.');await disconnect();}};
    await new Promise((resolve,reject)=>{opened.onopen=resolve;opened.addEventListener('error',reject,{once:true});opened.addEventListener('close',()=>reject(new Error('Connection closed')),{once:true});});
    send({type:'start',character:character(),voice,trigger:triggerSettings(),director:directorSettings(),narrative:narrativeSettings(),archive:archivePanel.settings()});
    if(useMic){
      const source=context.createMediaStreamSource(stream),gain=context.createGain();gain.gain.value=0;
      capture=new AudioWorkletNode(context,'live-capture');const resampler=new StreamResampler(context.sampleRate);
      capture.port.onmessage=async chunk=>{
        if(socket!==opened||!sessionReady||muted||replyPending||socket?.readyState!==WebSocket.OPEN)return;
        if(socket.bufferedAmount>262144){feedback('Connection is too slow for live audio. Reconnect after checking the tunnel.');await disconnect();return;}
        const samples=resampler.push(chunk.data);if(samples.length)socket.send(pcm16(samples));
      };
      source.connect(capture);capture.connect(gain);gain.connect(context.destination);
    }
    connecting=false;$('typed-form').hidden=useMic;controls();
  }catch(error){feedback(useMic?'Could not start the call. Check microphone permission, the SSH tunnel and service readiness.':'Could not start the text test. Check the SSH tunnel and service readiness.');await disconnect();}
}
$('start-call').addEventListener('click',()=>void connect(true));
$('text-mode').addEventListener('click',()=>void connect(false));
$('mute').addEventListener('click',()=>{muted=!muted;if(stream)stream.getAudioTracks().forEach(track=>track.enabled=!muted);send({type:'mute'});$('mute').textContent=muted?'Unmute mic':'Mute mic';state(muted?'Microphone muted':replyPending?'Reply in progress · Mic paused':'Listening…');});
$('interrupt').addEventListener('click',()=>{playback?.clear();send({type:'interrupt'});resumeMicrophone();});
$('typed-form').addEventListener('submit',e=>{e.preventDefault();const text=$('typed-message').value.trim();if(text){holdMicrophone();send({type:'text',text});$('typed-message').value='';feedback();}});
$('toggle-profile').addEventListener('click',()=>{form.hidden=!form.hidden;$('toggle-profile').setAttribute('aria-expanded',String(!form.hidden));$('toggle-profile').textContent=form.hidden?'Show details':'Hide details';});
form.addEventListener('input',()=>{const name=form.elements.name.value.trim()||'your character';$('chat-title').textContent=`Talk to ${name}`;});
form.addEventListener('input',saveActiveProfile);
form.addEventListener('change',saveActiveProfile);
$('character-picker').addEventListener('change',()=>{
  if(!characterLibrary)return;
  if(connecting||socket){characterOptions();return;}
  const selected=characterLibrary.switchTo($('character-picker').value,capturedProfile());
  if(selected)applyProfile(selected);
});
$('new-character').addEventListener('click',()=>{
  if(!characterLibrary||connecting||socket||disconnecting)return;
  $('new-character-form').reset();$('new-character-error').textContent='';
  $('new-character-dialog').showModal();
});
$('cancel-new-character').addEventListener('click',()=>$('new-character-dialog').close());
$('new-character-form').addEventListener('input',()=>{$('new-character-error').textContent='';});
$('new-character-form').addEventListener('submit',event=>{
  event.preventDefault();
  if(!characterLibrary||connecting||socket||disconnecting){
    $('new-character-error').textContent='End the current call before creating a character.';return;
  }
  const name=$('new-character-name').value.trim(),role=$('new-character-role').value.trim();
  if(!name||name.length>80||role.length>120){
    $('new-character-error').textContent=!name?'Give your character a name.':'Use a name up to 80 characters and a description up to 120 characters.';return;
  }
  let profile;
  try{
    saveActiveProfile();
    profile=characterLibrary.create({name,role,archiveEnabled:archivePage});
  }catch(error){
    $('new-character-error').textContent='Could not create the character. Try again in this browser.';return;
  }
  applyProfile(profile);$('new-character-dialog').close();
  form.hidden=false;$('toggle-profile').textContent='Hide details';$('toggle-profile').setAttribute('aria-expanded','true');
  form.elements.about.focus();
});
for(const id of ['voice-emotion','voice-event','voice-direction'])$(id).addEventListener('change',()=>{
  const settings=delivery();send({type:'delivery',settings});
  $('delivery-note').textContent=socket?'Delivery updated for the next reply.':'Delivery set for your next call.';
});
function updateVoicePreset(){
  const preset=$('voice-preset').value;
  $('custom-voice-fields').hidden=preset!=='custom';
  $('preset-audio').hidden=!['female','male'].includes(preset);
  $('preset-audio').pause();
  if(['female','male'].includes(preset))$('preset-audio').src=`/api/voice/${preset}`;
  else $('preset-audio').removeAttribute('src');
}
$('voice-preset').addEventListener('change',()=>{
  updateVoicePreset();
});
$('voice-reference').addEventListener('change',()=>{
  if(referenceURL)URL.revokeObjectURL(referenceURL);referenceURL=null;
  const file=$('voice-reference').files[0];
  if(file?.size>2*1024*1024){$('voice-reference').value='';feedback('Choose a voice reference smaller than 2 MB.');}
  else if(file){referenceURL=URL.createObjectURL(file);$('reference-audio').src=referenceURL;feedback();}
  $('reference-preview').hidden=!referenceURL;
});
$('clear-reference').addEventListener('click',clearReference);
for(const id of ['trigger-enabled','trigger-action','trigger-condition','trigger-emotion','trigger-behaviour','trigger-cue','trigger-delivery'])$(id).addEventListener('input',()=>{
  $('trigger-fields').hidden=!$('trigger-enabled').checked;$('test-trigger').textContent=$('trigger-action').value||'Test reaction';controls();
});
$('test-trigger').addEventListener('click',()=>{holdMicrophone();send({type:'trigger'});});
$('clear-trigger').addEventListener('click',()=>send({type:'clear_trigger'}));
$('send-direction').addEventListener('click',()=>{const settings=directorSettings();send({type:'director',settings});
  saveActiveProfile();
  if(!socket)feedback('Direction saved for your next call.');
});
for(const id of ['story-enabled','story-mode'])$(id).addEventListener('change',saveStory);
$('add-beat').addEventListener('click',()=>{storyEditor.add();controls();});
$('load-story-example').addEventListener('click',()=>{
  const settings=characterLibrary?.preset()?.narrative;if(!settings)return;
  storyEditor.load(settings.beats);$('story-enabled').checked=settings.enabled;$('story-mode').value=settings.mode;saveStory();controls();
});
for(const [id,action] of [['next-beat','next'],['previous-beat','previous'],['cue-beat','cue']])$(id).addEventListener('click',()=>send({type:'story',control:{action}}));
$('hold-story').addEventListener('click',()=>send({type:'story',control:{action:storyView?.paused?'resume':'pause'}}));
for(const id of ['director-enabled','director-input'])$(id).addEventListener('change',()=>{
  const settings=directorSettings();send({type:'director',settings});
});
async function status(){
  try{
    const response=await fetch('/api/status'),data=await response.json();ready=data.ready;inspector.setSupported(data.model_inspection===true);
    listeningFeedback.setMode(data.listening?.mode);
    $('status').textContent=data.preview?'Design preview':ready?'Voice services ready':'Services starting…';$('status').className=ready?'ready':'';
    $('mode-note').textContent=data.preview?'Design preview. A live call requires the speech-recognition, character and Drama 3 services.':ready?'Speak and pause to hear a reply. Your mic waits until the answer finishes. Use Stop reply to take over.':data.stt_error||Object.entries(data.services).map(([name,ok])=>`${name.toUpperCase()}: ${ok?'ready':'not ready'}`).join(' · ');
    if(data.busy&&!socket){ready=false;$('mode-note').textContent='Another voice session is active. End that call before starting a new one.';}
    if(data.models)$('director-model').textContent=`${data.profile} · Actor: ${data.models.actor.split('/').pop()} · Director: ${data.models.director.split('/').pop()}${data.director.shared_model?' (shared server)':''}`;
    controls();
  }catch(error){ready=false;$('status').textContent='Server disconnected';controls();}
}
async function init(){
  if(archivePage){
    document.title='Character Studio · Character + archive';
    document.querySelector('.brand span').textContent='Character + archive';
    document.querySelector('.eyebrow').textContent='ARCHIVE CONVERSATION';
  }
  let saved;try{saved=JSON.parse(localStorage.getItem('live-character-v1')||localStorage.getItem('qwen-character-v1'));}catch(error){}
  const initial=saved?.name?saved:await(await fetch('/api/example')).json();
  for(const key of fields)form.elements[key].value=initial[key]||(key==='speaker'?'Chelsie':'');
  try{
    const saved=JSON.parse(localStorage.getItem('live-delivery-v1'));
    for(const [key,id] of [['emotion','voice-emotion'],['event','voice-event']]){
      if([...$(id).options].some(option=>option.value===saved?.[key]))$(id).value=saved[key];
    }
    if(typeof saved?.direction==='string')$('voice-direction').value=saved.direction;
  }catch(error){}
  try{const saved=JSON.parse(localStorage.getItem('live-director-v1'));
    if(typeof saved?.enabled==='boolean')$('director-enabled').checked=saved.enabled;
    if(typeof saved?.notes==='string')$('director-input').value=saved.notes;
  }catch(error){}
  try{const preset=localStorage.getItem('live-voice-preset-v1');
    if([...$('voice-preset').options].some(option=>option.value===preset))$('voice-preset').value=preset;
  }catch(error){}
  updateVoicePreset();
  let trigger,storedTrigger=null;
  try{storedTrigger=localStorage.getItem('live-trigger-v1');trigger=JSON.parse(storedTrigger);}catch(error){}
  $('trigger-enabled').checked=storedTrigger===null?initial.name.toLowerCase()==='anna':!!trigger;
  if(trigger)for(const [key,id] of [['action','trigger-action'],['condition','trigger-condition'],['emotion','trigger-emotion'],['behaviour','trigger-behaviour'],['spoken_cue','trigger-cue'],['delivery','trigger-delivery']]){
    if(typeof trigger[key]==='string')$(id).value=trigger[key];
  }
  $('trigger-fields').hidden=!$('trigger-enabled').checked;$('test-trigger').textContent=$('trigger-action').value||'Test reaction';
  $('chat-title').textContent=`Talk to ${form.elements.name.value}`;
  if(matchMedia('(max-width:650px)').matches){form.hidden=true;$('toggle-profile').textContent='Show details';$('toggle-profile').setAttribute('aria-expanded','false');}
  let script;try{script=JSON.parse(localStorage.getItem('live-story-v1'));}catch(error){}
  if(!script){script=initial.name.toLowerCase()==='anna'?await(await fetch('/api/story/example')).json():{enabled:false,mode:'manual',beats:[]};}
  storyEditor.load(Array.isArray(script.beats)?script.beats:[]);$('story-enabled').checked=!!script.enabled;
  if(['manual','timed','director'].includes(script.mode))$('story-mode').value=script.mode;
  $('story-settings').hidden=!$('story-enabled').checked;previewStory();
  await archivePanel.load();
  await loadCharacterLibrary();
  await status();setInterval(status,5000);
}
window.addEventListener('beforeunload',()=>{if(stream)stream.getTracks().forEach(track=>track.stop());if(referenceURL)URL.revokeObjectURL(referenceURL);socket?.close();});
controls();void init().catch(()=>feedback('Could not load the character. Check the server and refresh.'));
