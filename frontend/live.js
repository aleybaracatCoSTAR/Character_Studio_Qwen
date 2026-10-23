import {StreamResampler,pcm16,PCMPlayback} from './live-audio.mjs';
const $=id=>document.getElementById(id),form=$('character-form');
const fields=['name','role','about','goal','environment','voice_style','rules','memories','beat','speaker'];
let socket=null,context=null,stream=null,capture=null,playback=null,connecting=false,muted=false,ready=false,currentTurn=0;
let replyPending=false,resumeTimer=null;
let referenceURL=null;
let triggerActive=false;
let sessionReady=false;
function triggerSettings(){return $('trigger-enabled').checked?{
  action:$('trigger-action').value,condition:$('trigger-condition').value,emotion:$('trigger-emotion').value,
  behaviour:$('trigger-behaviour').value,spoken_cue:$('trigger-cue').value,delivery:$('trigger-delivery').value
}:null;}
const replies=new Map();
function character(){return Object.fromEntries(fields.map(key=>[key,form.elements[key].value]));}
function feedback(message=''){$('feedback').textContent=message;}
function state(message){$('call-state').textContent=message;}
function send(value){if(socket?.readyState===WebSocket.OPEN)socket.send(JSON.stringify(value));}
function delivery(){return {emotion:$('voice-emotion').value,event:$('voice-event').value,direction:$('voice-direction').value};}
async function voiceSettings(){
  const settings=delivery(),file=$('voice-reference').files[0];
  if(!file)return settings;
  if(file.size>2*1024*1024)throw new Error('Choose a voice reference smaller than 2 MB.');
  const transcript=$('reference-text').value.trim();
  if(!transcript)throw new Error('Enter the exact words spoken in the voice reference.');
  const encoded=await new Promise((resolve,reject)=>{
    const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);
    reader.onerror=()=>reject(new Error('Could not read the voice reference.'));reader.readAsDataURL(file);
  });
  return {...settings,reference_audio:`data:audio/wav;base64,${encoded}`,reference_text:transcript};
}
function holdMicrophone(){clearTimeout(resumeTimer);replyPending=true;controls();}
function resumeMicrophone(){
  clearTimeout(resumeTimer);
  resumeTimer=setTimeout(()=>{
    replyPending=false;
    state(muted?'Microphone muted':stream?'Listening…':'Ready for your next message');controls();
  },350);
}
function controls(){
  const connected=socket?.readyState===WebSocket.OPEN;
  $('start-call').disabled=connecting||(!connected&&!ready);$('start-call').classList.toggle('connected',!!connected);
  $('start-call').querySelector('span').textContent=connected?'End session':connecting?'Connecting…':'Start call';
  $('mute').hidden=!connected||!stream;$('interrupt').hidden=!connected;
  $('interrupt').disabled=!replyPending;
  $('text-mode').hidden=!!connected;$('text-mode').disabled=connecting||!ready;
  $('send-text').disabled=!connected||!sessionReady;
  for(const element of form.elements)element.disabled=element.dataset.liveSetting?connecting:!!connected||connecting;
  $('test-trigger').disabled=!connected||!sessionReady||!$('trigger-enabled').checked;
  $('clear-trigger').disabled=!connected||!sessionReady||!triggerActive;
  document.querySelector('.live-badge').classList.toggle('active',!!connected);
}
function message(speaker,text,kind='assistant'){
  $('empty-state').hidden=true;
  const node=document.createElement('div');node.className=`message ${kind}`;
  const label=document.createElement('span');label.className='speaker';label.textContent=speaker;
  const content=document.createElement('div');content.textContent=text;node.append(label,content);$('messages').append(node);
  $('messages').scrollTop=$('messages').scrollHeight;return {node,content};
}
async function disconnect(){
  clearTimeout(resumeTimer);replyPending=false;sessionReady=false;
  triggerActive=false;$('trigger-state').textContent='Start a call to test the reaction.';
  const previous=socket;socket=null;connecting=false;
  if(previous){if(previous.readyState===WebSocket.OPEN)previous.send(JSON.stringify({type:'stop'}));previous.close();}
  if(stream)stream.getTracks().forEach(track=>track.stop());stream=null;
  if(capture){try{capture.disconnect();}catch(error){}capture=null;}
  if(playback)playback.clear();playback=null;
  if(context){try{await context.close();}catch(error){}context=null;}
  muted=false;$('mute').textContent='Mute mic';state('Call ended');controls();
}
function event(data){
  if(data.type==='trigger_state'){
    triggerActive=data.active;$('trigger-state').textContent=(data.active?`Reaction active · ${data.emotion}`:'Normal state restored')+(data.evidence?` · ${data.evidence}`:'');controls();return;
  }
  if(data.type==='trigger_check'){
    if(!data.ok)$('trigger-state').textContent=data.message;
    else if(data.action==='hold')$('trigger-state').textContent=(triggerActive?'Reaction continues':'Normal state')+(data.evidence?` · ${data.evidence}`:'');
    state('Thinking… · Mic paused');return;
  }
  if(data.type==='ready'){sessionReady=true;state(stream?'Listening…':'Ready for a typed message');controls();return;}
  if(data.type==='speech_start'){state('Listening…');return;}
  if(data.type==='listening'){state('Listening…');return;}
  if(data.type==='turn_start'){holdMicrophone();currentTurn=data.turn_id;playback.setTurn(currentTurn);state('Understanding… · Mic paused');return;}
  if(data.type==='interrupted'){
    if(data.turn_id===currentTurn){playback.clear();resumeMicrophone();const reply=replies.get(data.turn_id);if(reply){reply.content.textContent=data.heard||'Reply stopped';reply.node.classList.add('interrupted');}}
    return;
  }
  if(data.type==='error'){feedback(data.message);if(playback)playback.clear();resumeMicrophone();return;}
  if(data.type==='empty_transcript'){feedback('I did not catch that. Try again.');resumeMicrophone();return;}
  if(data.turn_id!==currentTurn)return;
  if(data.type==='transcript'){message(data.source==='action'?'Stage action':'You',data.text,'user');state($('trigger-enabled').checked?'Reading the situation… · Mic paused':'Thinking… · Mic paused');}
  if(data.type==='text_delta'){
    if(!replies.has(currentTurn))replies.set(currentTurn,message(character().name,''));
    replies.get(currentTurn).content.textContent+=data.text;$('messages').scrollTop=$('messages').scrollHeight;
  }
  if(data.type==='phrase_done')playback.phraseDone(data.seq);
  if(data.type==='response_done')playback.responseDone();
  if(data.type==='metrics')$('timing').textContent=`Server to first audio: ${data.speech_end_to_first_audio_ms} ms · Recognition: ${data.stt_ms} ms · Context: ${data.context_check_ms||0} ms · First text token: ${data.llm_first_token_ms} ms · Voice first chunk: ${data.tts_first_chunk_ms} ms`;
}
async function connect(useMic=true){
  if(connecting)return;if(socket){await disconnect();return;}if(!form.reportValidity())return;
  connecting=true;feedback();controls();
  try{
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
    opened.onmessage=received=>{try{if(typeof received.data==='string')event(JSON.parse(received.data));else playback?.push(received.data);}catch(error){feedback('Audio connection failed. End the call and reconnect.');}};
    opened.onerror=()=>feedback('Cannot connect to the live server. Check its terminal and SSH tunnel.');
    opened.onclose=async()=>{if(socket===opened){feedback('Live connection closed. Check that all three services are ready.');await disconnect();}};
    await new Promise((resolve,reject)=>{opened.onopen=resolve;opened.addEventListener('error',reject,{once:true});opened.addEventListener('close',()=>reject(new Error('Connection closed')),{once:true});});
    send({type:'start',character:character(),voice,trigger:triggerSettings()});
    if(useMic){
      const source=context.createMediaStreamSource(stream),gain=context.createGain();gain.gain.value=0;
      capture=new AudioWorkletNode(context,'live-capture');const resampler=new StreamResampler(context.sampleRate);
      capture.port.onmessage=async chunk=>{
        if(!sessionReady||muted||replyPending||socket?.readyState!==WebSocket.OPEN)return;
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
form.addEventListener('input',()=>{const name=form.elements.name.value.trim()||'your character';$('chat-title').textContent=`Talk to ${name}`;try{localStorage.setItem('live-character-v1',JSON.stringify(character()));}catch(error){}});
for(const id of ['voice-emotion','voice-event','voice-direction'])$(id).addEventListener('change',()=>{
  const settings=delivery();send({type:'delivery',settings});
  $('delivery-note').textContent=socket?'Delivery updated for the next reply.':'Delivery set for your next call.';
  try{localStorage.setItem('live-delivery-v1',JSON.stringify(settings));}catch(error){}
});
$('voice-reference').addEventListener('change',()=>{
  if(referenceURL)URL.revokeObjectURL(referenceURL);referenceURL=null;
  const file=$('voice-reference').files[0];
  if(file?.size>2*1024*1024){$('voice-reference').value='';feedback('Choose a voice reference smaller than 2 MB.');}
  else if(file){referenceURL=URL.createObjectURL(file);$('reference-audio').src=referenceURL;feedback();}
  $('reference-preview').hidden=!referenceURL;
});
$('clear-reference').addEventListener('click',()=>{
  $('voice-reference').value='';$('reference-text').value='';$('reference-audio').removeAttribute('src');
  if(referenceURL)URL.revokeObjectURL(referenceURL);referenceURL=null;$('reference-preview').hidden=true;
});
for(const id of ['trigger-enabled','trigger-action','trigger-condition','trigger-emotion','trigger-behaviour','trigger-cue','trigger-delivery'])$(id).addEventListener('input',()=>{
  $('trigger-fields').hidden=!$('trigger-enabled').checked;$('test-trigger').textContent=$('trigger-action').value||'Test reaction';controls();
  try{localStorage.setItem('live-trigger-v1',JSON.stringify(triggerSettings()));}catch(error){}
});
$('test-trigger').addEventListener('click',()=>{holdMicrophone();send({type:'trigger'});});
$('clear-trigger').addEventListener('click',()=>send({type:'clear_trigger'}));
async function status(){
  try{
    const response=await fetch('/api/status'),data=await response.json();ready=data.ready;
    $('status').textContent=data.preview?'Design preview':ready?'Voice services ready':'Services starting…';$('status').className=ready?'ready':'';
    $('mode-note').textContent=data.preview?'Design preview. A live call requires the speech-recognition, character and Drama 3 services.':ready?'Speak and pause to hear a reply. Your mic waits until the answer finishes. Use Stop reply to take over.':data.stt_error||Object.entries(data.services).map(([name,ok])=>`${name.toUpperCase()}: ${ok?'ready':'not ready'}`).join(' · ');
    if(data.busy&&!socket){ready=false;$('mode-note').textContent='Another voice session is active. End that call before starting a new one.';}
    controls();
  }catch(error){ready=false;$('status').textContent='Server disconnected';controls();}
}
async function init(){
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
  let trigger,storedTrigger=null;
  try{storedTrigger=localStorage.getItem('live-trigger-v1');trigger=JSON.parse(storedTrigger);}catch(error){}
  $('trigger-enabled').checked=storedTrigger===null?initial.name.toLowerCase()==='anna':!!trigger;
  if(trigger)for(const [key,id] of [['action','trigger-action'],['condition','trigger-condition'],['emotion','trigger-emotion'],['behaviour','trigger-behaviour'],['spoken_cue','trigger-cue'],['delivery','trigger-delivery']]){
    if(typeof trigger[key]==='string')$(id).value=trigger[key];
  }
  $('trigger-fields').hidden=!$('trigger-enabled').checked;$('test-trigger').textContent=$('trigger-action').value||'Test reaction';
  $('chat-title').textContent=`Talk to ${form.elements.name.value}`;
  if(matchMedia('(max-width:650px)').matches){form.hidden=true;$('toggle-profile').textContent='Show details';$('toggle-profile').setAttribute('aria-expanded','false');}
  await status();setInterval(status,5000);
}
window.addEventListener('beforeunload',()=>{if(stream)stream.getTracks().forEach(track=>track.stop());if(referenceURL)URL.revokeObjectURL(referenceURL);socket?.close();});
controls();void init().catch(()=>feedback('Could not load the character. Check the server and refresh.'));
