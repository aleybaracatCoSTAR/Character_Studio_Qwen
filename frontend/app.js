'use strict';
const $ = id => document.getElementById(id);
const profile = $('character-form');
let sessionId = null, ready = false, busy = false, recording = null, pending = null, startingRecording = false;
let urls = [], recorderTimer = null;
const fields = ['name','role','about','goal','environment','voice_style','rules','memories','beat','speaker'];
function character() { return Object.fromEntries(fields.map(key => [key, profile.elements[key].value])); }
function feedback(text = '') { $('feedback').textContent = text; }
function controls() {
  $('send-button').disabled = !ready || busy || !!recording || startingRecording;
  $('record-button').disabled = startingRecording || (!recording && (!ready || busy));
  $('new-chat').disabled = busy || !!recording || startingRecording;
  $('discard-recording').disabled = busy;
  for (const el of profile.elements) el.disabled = busy || !!recording || startingRecording;
  $('message').disabled = busy;
  document.querySelectorAll('.suggestion').forEach(button => button.disabled = !ready || busy);
}
function syncTitle() {
  const name = profile.elements.name.value.trim() || 'your character';
  $('chat-title').textContent = `Meet ${name}`;
  $('message').placeholder = `Say something to ${name}…`;
}
function clearRecording() {
  pending = null;
  $('recording-audio').pause();
  $('recording-audio').removeAttribute('src');
  $('recording-preview').hidden = true;
}
function resetChat(message = '') {
  sessionId = null;
  document.querySelectorAll('audio').forEach(audio => audio.pause());
  urls.forEach(url => URL.revokeObjectURL(url)); urls = [];
  $('messages').querySelectorAll('.message').forEach(el => el.remove());
  $('empty-state').hidden = false;
  clearRecording(); feedback(message);
}
function appendMessage(speaker, text, audioBlob, seconds) {
  $('empty-state').hidden = true;
  const block = document.createElement('div'); block.className = `message ${speaker === 'You' ? 'user' : 'assistant'}`;
  const label = document.createElement('span'); label.className = 'speaker'; label.textContent = speaker;
  const content = document.createElement('div'); content.textContent = text;
  block.append(label, content);
  if (audioBlob) {
    const player = document.createElement('audio'); player.controls = true;
    const url = URL.createObjectURL(audioBlob); urls.push(url); player.src = url;
    player.setAttribute('aria-label', `${speaker}'s audio`); block.append(player);
    if (speaker !== 'You' && $('spoken-reply').checked) player.play().catch(() => feedback('Your reply is ready. Press play to listen.'));
  }
  if (seconds != null) { const meta = document.createElement('div'); meta.className = 'timing'; meta.textContent = `${seconds}s to generate`; block.append(meta); }
  $('messages').append(block); $('messages').scrollTop = $('messages').scrollHeight;
}
function wavBlob(chunks, sampleRate) {
  const count = chunks.reduce((n, chunk) => n + chunk.length, 0);
  const data = new ArrayBuffer(44 + count * 2), view = new DataView(data);
  const str = (at, value) => { for (let i=0;i<value.length;i++) view.setUint8(at+i,value.charCodeAt(i)); };
  str(0,'RIFF'); view.setUint32(4,36+count*2,true); str(8,'WAVE'); str(12,'fmt ');
  view.setUint32(16,16,true); view.setUint16(20,1,true); view.setUint16(22,1,true);
  view.setUint32(24,sampleRate,true); view.setUint32(28,sampleRate*2,true);
  view.setUint16(32,2,true); view.setUint16(34,16,true); str(36,'data'); view.setUint32(40,count*2,true);
  let offset=44;
  for (const chunk of chunks) for (const value of chunk) { const x=Math.max(-1,Math.min(1,value)); view.setInt16(offset,x<0?x*32768:x*32767,true); offset+=2; }
  return new Blob([data],{type:'audio/wav'});
}
async function stopRecording() {
  if (!recording) return;
  const current=recording; recording=null; clearInterval(recorderTimer);
  current.stream.getTracks().forEach(track => track.stop());
  current.node.disconnect(); current.source.disconnect();
  await current.context.close();
  pending=wavBlob(current.chunks,current.context.sampleRate);
  const url=URL.createObjectURL(pending); urls.push(url); $('recording-audio').src=url;
  $('recording-preview').hidden=false;
  $('record-button').setAttribute('aria-pressed','false'); $('record-button').querySelector('span').textContent='Record';
  feedback('Recording ready. Listen back or send it.'); controls();
}
async function startRecording() {
  if (startingRecording) return;
  if (recording) { await stopRecording(); return; }
  startingRecording=true; controls();
  document.querySelectorAll('audio').forEach(audio=>audio.pause());
  let stream, context;
  try {
    stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,channelCount:1}});
    context=new AudioContext(); await context.resume();
    await context.audioWorklet.addModule('/static/recorder-worklet.js');
    const source=context.createMediaStreamSource(stream), node=new AudioWorkletNode(context,'pcm-recorder');
    const gain=context.createGain(); gain.gain.value=0;
    const chunks=[]; let samples=0;
    node.port.onmessage=event=>{
      if (!recording) return;
      const remaining=Math.floor(context.sampleRate*30)-samples;
      const chunk=event.data.slice(0,Math.max(0,remaining)); chunks.push(chunk); samples+=chunk.length;
      if (samples>=context.sampleRate*30) void stopRecording();
    };
    clearRecording(); source.connect(node); node.connect(gain); gain.connect(context.destination);
    recording={stream,context,source,node,chunks};
    $('record-button').setAttribute('aria-pressed','true'); $('record-button').querySelector('span').textContent='Stop';
    const started=Date.now(); recorderTimer=setInterval(()=>{ const secs=Math.floor((Date.now()-started)/1000); feedback(`Listening · ${secs}s / 30s`); if (secs>=30) void stopRecording(); },250);
    feedback('Listening… Press Stop when you finish.'); controls();
  } catch (error) {
    if(stream) stream.getTracks().forEach(track=>track.stop()); if(context) await context.close();
    feedback('Could not record. Allow microphone access on localhost, or type your question.'); controls();
  } finally { startingRecording=false; controls(); }
}
async function request(url, options) {
  const response=await fetch(url, options); const data=await response.json();
  if(!response.ok) throw new Error(typeof data.detail==='string'?data.detail:'Check the character fields and try again.');
  return data;
}
async function updateStatus() {
  try {
    const state=await request('/api/status'); ready=state.ready;
    $('status').textContent=state.ready?'Qwen ready':state.state==='preview'?'Design preview':state.state==='error'?'Model unavailable':'Loading Qwen…';
    $('status').className=state.ready?'ready':state.state==='error'?'error':'';
    $('mode-note').textContent=state.ready?'Qwen3-Omni · Private GPU session · Record up to 30 seconds.':state.state==='preview'?'Design preview. Character editing works here. AI replies need the GPU setup in docs/SURREY.md.':state.error||'Loading model weights onto the GPU. The first start can take several minutes.';
    controls();
  } catch(error) { ready=false; $('status').textContent='Server disconnected'; feedback('Cannot reach the studio server. Check its terminal.'); controls(); }
}
profile.addEventListener('input',()=>{
  syncTitle(); resetChat('Character updated. Your next message starts a fresh conversation.');
  try { localStorage.setItem('qwen-character-v1',JSON.stringify(character())); } catch(error) { $('save-note').textContent='Browser storage is unavailable. Download JSON to save this character.'; }
});
$('new-chat').addEventListener('click',()=>resetChat('New conversation.'));
$('toggle-profile').addEventListener('click',()=>{ profile.hidden=!profile.hidden; $('toggle-profile').setAttribute('aria-expanded',String(!profile.hidden)); $('toggle-profile').textContent=profile.hidden?'Show details':'Hide details'; });
$('record-button').addEventListener('click',()=>void startRecording());
$('discard-recording').addEventListener('click',()=>{clearRecording();feedback();});
document.querySelectorAll('.suggestion').forEach(button=>button.addEventListener('click',()=>{$('message').value=button.textContent;$('message').focus();}));
$('message').addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();if(!$('send-button').disabled)$('chat-form').requestSubmit();}});
$('chat-form').addEventListener('submit',async event=>{
  event.preventDefault(); if(!ready||busy||recording)return; if(!profile.reportValidity())return;
  const text=$('message').value.trim(), clip=pending;
  if(!text&&!clip){feedback('Type a question or record your voice first.');return;}
  busy=true;controls();feedback('Thinking and preparing the reply…');
  const form=new FormData();form.append('payload',JSON.stringify({character:character(),text,session_id:sessionId,speak:$('spoken-reply').checked}));
  if(clip)form.append('audio',clip,'recording.wav');
  try {
    const result=await request('/api/chat',{method:'POST',body:form});sessionId=result.session_id;
    appendMessage('You',text||(clip?'Voice message':''),clip); $('message').value='';clearRecording();
    let sound=null;if(result.audio_base64){const bytes=Uint8Array.from(atob(result.audio_base64),c=>c.charCodeAt(0));sound=new Blob([bytes],{type:result.audio_mime});}
    feedback(result.dropped_turns?'Earlier exchanges were removed to fit the context.':'');
    appendMessage(character().name,result.text,sound,result.seconds);
  } catch(error) {feedback(error.message);} finally {busy=false;controls();}
});
$('prompt-button').addEventListener('click',async()=>{
  if(!profile.reportValidity())return;
  try {const result=await request('/api/prompt',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(character())});$('prompt-text').textContent=result.prompt;$('prompt-dialog').showModal();}catch(error){feedback(error.message);}
});
$('close-prompt').addEventListener('click',()=>$('prompt-dialog').close());
$('export-button').addEventListener('click',()=>{
  const blob=new Blob([JSON.stringify(character(),null,2)+'\n'],{type:'application/json'}),url=URL.createObjectURL(blob);
  const link=document.createElement('a');link.href=url;link.download=(character().name||'character').replace(/[^a-z0-9_-]/gi,'_')+'.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
});
window.addEventListener('beforeunload',()=>{if(recording)recording.stream.getTracks().forEach(track=>track.stop());urls.forEach(url=>URL.revokeObjectURL(url));});
async function init(){
  try {
    let saved=null;try{saved=JSON.parse(localStorage.getItem('qwen-character-v1'));}catch(error){}
    const initial=saved&&typeof saved==='object'&&typeof saved.name==='string'?saved:await request('/api/example');
    for(const key of fields)profile.elements[key].value=initial[key]|| (key==='speaker'?'Chelsie':'');
    syncTitle(); if(window.matchMedia('(max-width:650px)').matches){profile.hidden=true;$('toggle-profile').textContent='Show details';$('toggle-profile').setAttribute('aria-expanded','false');}
    await updateStatus();setInterval(updateStatus,5000);
  }catch(error){feedback('Could not load the example character. Refresh after starting the server.');}
}
controls();void init();
