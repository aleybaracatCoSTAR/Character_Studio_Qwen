const STAGES = {actor:'Character actor',director:'Director',trigger:'Emotion check',tts:'Drama 3 voice'};

// Captured requests stay in this page's memory for one call. Rendering uses
// textContent throughout: character text is data, never executable markup.
export class PromptInspector {
  constructor({document,send,writeText=text=>globalThis.navigator.clipboard.writeText(text)}){
    this.document=document;this.send=send;this.writeText=writeText;
    this.connected=false;this.supported=null;this.requests=[];this.selectedId=null;this.epoch=0;
    this.dialog=this.get('model-inspector');
    this.get('inspect-model').addEventListener('click',()=>this.open());
    this.get('close-inspector').addEventListener('click',()=>this.dialog.close());
    this.dialog.addEventListener('close',()=>this.subscribe(false));
    this.get('inspection-request').addEventListener('change',()=>{
      this.selectedId=Number(this.get('inspection-request').value);this.renderRequest();
    });
    this.get('copy-inspection').addEventListener('click',()=>void this.copy());
    this.render();
  }
  get(id){return this.document.getElementById(id);}
  subscribe(enabled){if(this.connected&&this.supported!==false)this.send({type:'inspect',enabled});}
  open(){
    if(!this.dialog.open)this.dialog.showModal();
    this.render();this.subscribe(true);
  }
  setConnected(connected){
    this.connected=connected;
    if(!connected)this.reset();
    else{this.render();if(this.dialog.open)this.subscribe(true);}
  }
  setSupported(supported){
    const previous=this.supported;this.supported=supported;
    if(!supported)this.reset();
    else if(previous!==supported&&this.connected&&this.dialog.open)this.subscribe(true);
  }
  reset(){
    this.epoch++;
    this.requests=[];this.selectedId=null;
    this.get('inspection-status').textContent='';this.render();
  }
  accept(data){
    if(!this.connected||data.type!=='model_inspection'||!Array.isArray(data.requests))return;
    const previousLast=this.requests.at(-1)?.id;
    const followLatest=this.selectedId===null||this.selectedId===previousLast;
    this.requests=data.requests.filter(request=>Number.isInteger(request?.id)&&Number.isInteger(request.turn_id)
      &&request.turn_id>=0&&Object.hasOwn(STAGES,request.stage)&&typeof request.model==='string'
      &&request.payload&&typeof request.payload==='object'&&!Array.isArray(request.payload)).slice(-24);
    if(followLatest||!this.requests.some(request=>request.id===this.selectedId))this.selectedId=this.requests.at(-1)?.id??null;
    this.render();
  }
  selected(){return this.requests.find(request=>request.id===this.selectedId);}
  render(){
    const select=this.get('inspection-request');select.replaceChildren();
    for(const request of this.requests){
      const option=this.document.createElement('option');option.value=String(request.id);
      const phase=request.phase==='between_turns'?' (between turns)':request.phase==='voice_retry'?' (voice retry)':'';
      option.textContent=`${request.turn_id?`Turn ${request.turn_id}`:'Between-turn check'} · ${STAGES[request.stage]}${phase} · #${request.id}`;
      select.append(option);
    }
    if(this.selectedId!==null)select.value=String(this.selectedId);
    const hasRequests=!!this.requests.length;
    this.get('inspection-empty').hidden=hasRequests;
    this.get('inspection-empty').textContent=this.supported===false
      ?'The running server does not support request inspection yet. Pull the latest code, restart the live stack and refresh this page.'
      :this.connected
      ?'No model requests captured yet. Send a message or speak to the character to see the actual request here.'
      :'Start a call or a text test, then send a message. Actual model requests will appear here.';
    this.get('inspection-controls').hidden=!hasRequests;
    this.get('inspection-detail').hidden=!hasRequests;
    this.get('copy-inspection').disabled=!hasRequests;
    this.renderRequest();
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
    const request=this.selected();
    this.get('inspection-model').textContent=request?`${STAGES[request.stage]} · ${request.model}`:'';
    this.get('inspection-json').textContent=request?JSON.stringify(request.payload,null,2):'';
    if(!request)return;
    const payload=request.payload;
    this.get('copy-inspection').disabled=!Object.keys(payload).length;
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
}
