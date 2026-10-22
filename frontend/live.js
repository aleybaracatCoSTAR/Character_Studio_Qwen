import {StreamResampler,pcm16,PCMPlayback} from './live-audio.mjs';
const $=id=>document.getElementById(id),form=$('character-form');
const fields=['name','role','about','goal','environment','voice_style','rules','memories','beat','speaker'];
let socket=null,context=null,stream=null,capture=null,playback=null,connecting=false,muted=false,ready=false,currentTurn=0;
const replies=new Map();
function character(){return Object.fromEntries(fields.map(key=>[key,form.elements[key].value]));}
function feedback(message=''){$('feedback').textContent=message;}
function state(message){$('call-state').textContent=message;}
function send(value){if(socket?.readyState===WebSocket.OPEN)socket.send(JSON.stringify(value));}
function controls(){
  const connected=socket?.readyState===WebSocket.OPEN;
  $('start-call').disabled=connecting||(!connected&&!ready);$('start-call').classList.toggle('connected',!!connected);
  $('start-call').querySelector('span').textContent=connected?'End session':connecting?'Connecting…':'Start call';
  $('mute').hidden=!connected||!stream;$('interrupt').hidden=!connected;
  $('text-mode').hidden=!!connected;$('text-mode').disabled=connecting||!ready;
  $('send-text').disabled=!connected;
  for(const element of form.elements)element.disabled=!!connected||connecting;
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
  const previous=socket;socket=null;connecting=false;
  if(previous){if(previous.readyState===WebSocket.OPEN)previous.send(JSON.stringify({type:'stop'}));previous.close();}
  if(stream)stream.getTracks().forEach(track=>track.stop());stream=null;
  if(capture){try{capture.disconnect();}catch(error){}capture=null;}
  if(playback)playback.clear();playback=null;
  if(context){try{await context.close();}catch(error){}context=null;}
  muted=false;$('mute').textContent='Mute mic';state('Call ended');controls();
}
function event(data){
  if(data.type==='ready'){state(stream?'Listening…':'Ready for a typed message');controls();return;}
  if(data.type==='speech_start'){if(playback)playback.clear();state('Listening…');return;}
  if(data.type==='listening'){state('Listening…');return;}
  if(data.type==='turn_start'){currentTurn=data.turn_id;playback.setTurn(currentTurn);state('Understanding…');return;}
  if(data.type==='interrupted'){
    if(data.turn_id===currentTurn){playback.clear();const reply=replies.get(data.turn_id);if(reply){reply.content.textContent=data.heard||'Reply interrupted';reply.node.classList.add('interrupted');}}
    return;
  }
  if(data.type==='error'){feedback(data.message);if(playback)playback.clear();state('Ready to try again');return;}
  if(data.type==='empty_transcript'){state('I did not catch that. Try again.');return;}
  if(data.turn_id!==currentTurn)return;
  if(data.type==='transcript'){message('You',data.text,'user');state('Thinking…');}
  if(data.type==='text_delta'){
    if(!replies.has(currentTurn))replies.set(currentTurn,message(character().name,''));
    replies.get(currentTurn).content.textContent+=data.text;$('messages').scrollTop=$('messages').scrollHeight;
  }
  if(data.type==='phrase_done')playback.phraseDone(data.seq);
  if(data.type==='response_done')playback.responseDone();
  if(data.type==='metrics')$('timing').textContent=`Server to first audio: ${data.speech_end_to_first_audio_ms} ms · Recognition: ${data.stt_ms} ms · First text token: ${data.llm_first_token_ms} ms · Voice first chunk: ${data.tts_first_chunk_ms} ms`;
}
async function connect(useMic=true){
  if(connecting)return;if(socket){await disconnect();return;}if(!form.reportValidity())return;
  connecting=true;feedback();controls();
  try{
    context=new AudioContext();await context.resume();
    playback=new PCMPlayback(context,send,()=>state('Speaking…'),()=>state(stream&&!muted?'Listening…':'Ready for your next message'));
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
    send({type:'start',character:character()});
    if(useMic){
      const source=context.createMediaStreamSource(stream),gain=context.createGain();gain.gain.value=0;
      capture=new AudioWorkletNode(context,'live-capture');const resampler=new StreamResampler(context.sampleRate);
      capture.port.onmessage=async chunk=>{
        if(muted||socket?.readyState!==WebSocket.OPEN)return;
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
$('mute').addEventListener('click',()=>{muted=!muted;if(stream)stream.getAudioTracks().forEach(track=>track.enabled=!muted);send({type:'mute'});$('mute').textContent=muted?'Unmute mic':'Mute mic';state(muted?'Microphone muted':'Listening…');});
$('interrupt').addEventListener('click',()=>{playback?.clear();send({type:'interrupt'});state('Listening…');});
$('typed-form').addEventListener('submit',e=>{e.preventDefault();const text=$('typed-message').value.trim();if(text){send({type:'text',text});$('typed-message').value='';feedback();}});
$('toggle-profile').addEventListener('click',()=>{form.hidden=!form.hidden;$('toggle-profile').setAttribute('aria-expanded',String(!form.hidden));$('toggle-profile').textContent=form.hidden?'Show details':'Hide details';});
form.addEventListener('input',()=>{const name=form.elements.name.value.trim()||'your character';$('chat-title').textContent=`Talk to ${name}`;try{localStorage.setItem('live-character-v1',JSON.stringify(character()));}catch(error){}});
async function status(){
  try{
    const response=await fetch('/api/status'),data=await response.json();ready=data.ready;
    $('status').textContent=data.preview?'Design preview':ready?'Voice services ready':'Services starting…';$('status').className=ready?'ready':'';
    $('mode-note').textContent=data.preview?'Design preview. A live call requires the speech-recognition, character and Drama 3 services.':ready?'Speak and pause to hear a reply. Audio starts playing as each phrase arrives.':data.stt_error||Object.entries(data.services).map(([name,ok])=>`${name.toUpperCase()}: ${ok?'ready':'not ready'}`).join(' · ');
    if(data.busy&&!socket){ready=false;$('mode-note').textContent='Another voice session is active. End that call before starting a new one.';}
    controls();
  }catch(error){ready=false;$('status').textContent='Server disconnected';controls();}
}
async function init(){
  let saved;try{saved=JSON.parse(localStorage.getItem('live-character-v1')||localStorage.getItem('qwen-character-v1'));}catch(error){}
  const initial=saved?.name?saved:await(await fetch('/api/example')).json();
  for(const key of fields)form.elements[key].value=initial[key]||(key==='speaker'?'Chelsie':'');
  $('chat-title').textContent=`Talk to ${form.elements.name.value}`;
  if(matchMedia('(max-width:650px)').matches){form.hidden=true;$('toggle-profile').textContent='Show details';$('toggle-profile').setAttribute('aria-expanded','false');}
  await status();setInterval(status,5000);
}
window.addEventListener('beforeunload',()=>{if(stream)stream.getTracks().forEach(track=>track.stop());socket?.close();});
controls();void init().catch(()=>feedback('Could not load the character. Check the server and refresh.'));
