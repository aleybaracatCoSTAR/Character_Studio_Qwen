import {requestText,recentJSON,downloadViaServer,requestFilename} from './live-inspection-export.mjs';

const STAGES = {actor:'Character actor',director:'Director',trigger:'Emotion check',tts:'Drama 3 voice'};
const clone=value=>JSON.parse(JSON.stringify(value));

// Keep one exact selected snapshot for reading, even after the server prunes it.
// Rendering uses textContent: character text is data, never executable markup.
export class PromptInspector {
  constructor({document,send,writeText=text=>globalThis.navigator.clipboard.writeText(text),saveFile}){
    this.document=document;this.send=send;this.writeText=writeText;
    this.saveFile=saveFile??(file=>downloadViaServer(document,file));
    this.connected=false;this.supported=null;this.requests=[];this.selectedId=null;this.selectedRequest=null;this.epoch=0;
    this.dialog=this.get('model-inspector');
    this.get('inspect-model').addEventListener('click',()=>this.open());
    this.get('close-inspector').addEventListener('click',()=>this.dialog.close());
    this.dialog.addEventListener('close',()=>this.subscribe(false));
    this.get('inspection-request').addEventListener('change',()=>{
      const id=Number(this.get('inspection-request').value);
      const request=this.requests.find(item=>item.id===id)??(this.selectedId===id?this.selectedRequest:null);
      if(request)this.select(request);
    });
    this.get('inspection-latest').addEventListener('click',()=>this.showLatest());
    this.get('copy-inspection').addEventListener('click',()=>void this.copy());
    this.get('download-inspection').addEventListener('click',()=>void this.downloadRequest());
    this.get('download-inspection-recent').addEventListener('click',()=>void this.downloadRecent());
    this.renderControls();this.renderRequest();
  }
  get(id){return this.document.getElementById(id);}
  subscribe(enabled){if(this.connected&&this.supported!==false)this.send({type:'inspect',enabled});}
  open(){
    if(!this.dialog.open)this.dialog.showModal();
    this.renderControls();this.subscribe(true);
  }
  setConnected(connected){
    this.connected=connected;
    if(!connected)this.reset();
    else{this.renderControls();if(this.dialog.open)this.subscribe(true);}
  }
  setSupported(supported){
    const previous=this.supported;this.supported=supported;
    if(!supported)this.reset();
    else if(previous!==supported&&this.connected&&this.dialog.open)this.subscribe(true);
  }
  reset(){
    this.epoch++;
    this.requests=[];this.selectedId=null;this.selectedRequest=null;
    this.renderControls();this.renderRequest();
  }
  accept(data){
    if(!this.connected||data.type!=='model_inspection'||!Array.isArray(data.requests))return;
    this.requests=clone(data.requests.filter(request=>Number.isInteger(request?.id)&&Number.isInteger(request.turn_id)
      &&request.turn_id>=0&&Object.hasOwn(STAGES,request.stage)&&typeof request.model==='string'
      &&request.payload&&typeof request.payload==='object'&&!Array.isArray(request.payload)).slice(-24));
    if(!this.selectedRequest&&this.requests.length){this.select(this.requests.at(-1));return;}
    // Do not rebuild details, reset scroll, or erase copy/download feedback when
    // a later director, actor or voice request arrives.
    this.renderControls();
  }
  selected(){return this.selectedRequest;}
  select(request){
    this.selectedRequest=clone(request);this.selectedId=request.id;
    this.renderControls();this.renderRequest();
  }
  showLatest(){
    const latest=this.requests.at(-1);if(latest)this.select(latest);
  }
  option(request,saved=false){
    const option=this.document.createElement('option');option.value=String(request.id);
    const phase=request.phase==='between_turns'?' (between turns)':request.phase==='voice_retry'?' (voice retry)':'';
    option.textContent=`${request.turn_id?`Turn ${request.turn_id}`:'Between-turn check'} · ${STAGES[request.stage]}${phase} · #${request.id}${saved?' · saved selection':''}`;
    return option;
  }
  renderControls(){
    const select=this.get('inspection-request');select.replaceChildren();
    if(this.selectedRequest&&!this.requests.some(request=>request.id===this.selectedId))select.append(this.option(this.selectedRequest,true));
    for(const request of this.requests)select.append(this.option(request));
    if(this.selectedId!==null)select.value=String(this.selectedId);
    const hasSelection=!!this.selectedRequest,hasRecent=!!this.requests.length;
    this.get('inspection-empty').hidden=hasSelection||hasRecent;
    this.get('inspection-empty').textContent=this.supported===false
      ?'The running server does not support request inspection yet. Pull the latest code, restart the live stack and refresh this page.'
      :this.connected
      ?'No model requests captured yet. Send a message or speak to the character to see the actual request here.'
      :'Start a call or a text test, then send a message. Actual model requests will appear here.';
    this.get('inspection-controls').hidden=!hasSelection&&!hasRecent;
    this.get('inspection-detail').hidden=!hasSelection;
    this.get('copy-inspection').disabled=!hasSelection||!Object.keys(this.selectedRequest.payload).length;
    this.get('download-inspection').disabled=!hasSelection;
    this.get('download-inspection-recent').disabled=!hasRecent;
    const latest=this.requests.at(-1),newer=this.requests.filter(request=>this.selectedId!==null&&request.id>this.selectedId).length;
    this.get('inspection-latest').disabled=!latest||latest.id===this.selectedId;
    this.get('inspection-updates').textContent=!hasSelection?'':newer
      ?`${newer} newer ${newer===1?'request available':'requests available'}. Your selection is held for reading.`
      :'Your selected request is held for reading.';
  }
  section(title,value){
    const section=this.document.createElement('section');section.className='inspection-section';
    const heading=this.document.createElement('h3'),pre=this.document.createElement('pre');
    heading.textContent=title;pre.textContent=typeof value==='string'?value:JSON.stringify(value,null,2);
    section.append(heading,pre);this.get('inspection-messages').append(section);
  }
  renderRequest(){
    this.get('inspection-messages').replaceChildren();
    this.get('inspection-status').textContent='';
    this.get('inspection-raw').open=false;
    this.get('inspection-json').scrollTop=0;this.dialog.scrollTop=0;
    const request=this.selected();
    this.get('inspection-model').textContent=request?`${STAGES[request.stage]} · ${request.model}`:'';
    this.get('inspection-json').textContent=request?JSON.stringify(request.payload,null,2):'';
    if(!request)return;
    const payload=request.payload;
    if(request.notice)this.section('Request notice',request.notice);
    if(request.omitted_fields?.length)this.section('Fields omitted from this view',request.omitted_fields);
    if(Array.isArray(payload.messages))for(const [index,message] of payload.messages.entries()){
      const role=typeof message?.role==='string'?message.role:'message';
      this.section(`${index+1} · ${role}`,message?.content??'');
    }
    if(Object.hasOwn(payload,'input'))this.section('Spoken text and delivery cue',payload.input);
    if(Object.hasOwn(payload,'ref_text'))this.section('Voice reference transcript',payload.ref_text);
    if(request.reference_audio)this.section('Voice reference · audio omitted',request.reference_audio);
    const settings=Object.fromEntries(Object.entries(payload).filter(([key])=>!['messages','input','ref_text'].includes(key)));
    this.section('Generation settings',settings);
  }
  async copy(){
    const request=this.selected();if(!request||!Object.keys(request.payload).length)return;
    const id=request.id,epoch=this.epoch;
    try{
      await this.writeText(JSON.stringify(request.payload,null,2));
      if(this.connected&&this.epoch===epoch&&this.selectedId===id)this.get('inspection-status').textContent='Request JSON copied.';
    }catch(error){
      if(this.connected&&this.epoch===epoch&&this.selectedId===id){
        this.get('inspection-status').textContent='Copy was unavailable. Open Request JSON below to select and copy the text.';
        this.get('inspection-raw').open=true;
      }
    }
  }
  async downloadRequest(){
    const request=this.selected();if(!request)return;
    const id=request.id,epoch=this.epoch;
    try{
      await this.saveFile({filename:requestFilename(request,'txt'),content:requestText(request),mime:'text/plain;charset=utf-8'});
      if(this.connected&&this.epoch===epoch&&this.selectedId===id)this.get('inspection-status').textContent='Request download started.';
    }catch(error){
      if(this.connected&&this.epoch===epoch&&this.selectedId===id){
        this.get('inspection-status').textContent=error?.message==='Update and restart the studio server to enable downloads.'
          ?error.message:'Download was unavailable. Use Copy request JSON or select the text in Request JSON below.';
        this.get('inspection-raw').open=true;
      }
    }
  }
  async downloadRecent(){
    if(!this.requests.length)return;
    const epoch=this.epoch;
    try{
      await this.saveFile({filename:'character-studio-model-inputs.json',content:recentJSON(this.requests),mime:'application/json;charset=utf-8'});
      if(this.connected&&this.epoch===epoch)this.get('inspection-status').textContent='Recent requests download started.';
    }catch(error){
      if(this.connected&&this.epoch===epoch)this.get('inspection-status').textContent=error?.message==='Update and restart the studio server to enable downloads.'
        ?error.message:'Download was unavailable. Try again, or use Copy request JSON for the selected request.';
    }
  }
}
