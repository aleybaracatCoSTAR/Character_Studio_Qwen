// Voice recordings stay in this browser's IndexedDB, separate from character
// JSON/localStorage. Only an opaque saved-voice ID belongs to a character.
export const MAX_VOICE_BYTES=2*1024*1024;
export const MAX_SAVED_VOICES=20;
const DB_NAME='character-studio-voices-v1',STORE='voices';
const ID=/^voice-[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/;
export const savedVoiceId=value=>typeof value==='string'&&ID.test(value)?value:'';
const NAME=value=>typeof value==='string'?value.trim().slice(0,80):'';

export async function validateVoiceWav(audio){
  if(!audio||typeof audio.arrayBuffer!=='function'||!Number.isInteger(audio.size)||!audio.size||audio.size>MAX_VOICE_BYTES)
    throw new Error('Choose a PCM16 WAV voice reference up to 2 MB.');
  const bytes=await audio.arrayBuffer(),view=new DataView(bytes);
  const tag=offset=>String.fromCharCode(...new Uint8Array(bytes,offset,4));
  if(bytes.byteLength<44||tag(0)!=='RIFF'||tag(8)!=='WAVE'||view.getUint32(4,true)+8>bytes.byteLength)
    throw new Error('Choose a valid uncompressed PCM16 WAV reference.');
  let format=null,dataBytes=null;
  for(let offset=12;offset+8<=bytes.byteLength;){
    const size=view.getUint32(offset+4,true),start=offset+8;
    if(start+size>bytes.byteLength)throw new Error('The WAV reference is incomplete.');
    if(tag(offset)==='fmt '){
      if(size<16)throw new Error('Choose a valid PCM16 WAV reference.');
      format={encoding:view.getUint16(start,true),channels:view.getUint16(start+2,true),rate:view.getUint32(start+4,true),
        alignment:view.getUint16(start+12,true),bits:view.getUint16(start+14,true)};
    }
    if(tag(offset)==='data'&&dataBytes===null)dataBytes=size;
    offset=start+size+(size%2);
  }
  if(!format||format.encoding!==1||![1,2].includes(format.channels)||format.bits!==16||format.rate<8000||format.rate>48000
      ||format.alignment!==format.channels*2||!dataBytes||dataBytes%format.alignment)
    throw new Error('Use a 2–15 second PCM16 WAV, mono or stereo, at 8–48 kHz.');
  const seconds=dataBytes/(format.rate*format.alignment);
  if(seconds<2||seconds>15)throw new Error('Use a 2–15 second voice reference.');
  return {seconds,bytes};
}

export class VoiceReferenceStore{
  constructor({indexedDB=globalThis.indexedDB,uuid=()=>globalThis.crypto?.randomUUID()}={}){this.indexedDB=indexedDB;this.uuid=uuid;this.opening=null;}
  open(){
    if(this.opening)return this.opening;
    this.opening=new Promise((resolve,reject)=>{
      if(!this.indexedDB){reject(new Error('This browser cannot save voices. You can still upload a WAV for this visit.'));return;}
      let request,settled=false;
      try{request=this.indexedDB.open(DB_NAME,1);}catch(error){reject(new Error('Voice storage is unavailable. You can still upload a WAV for this visit.'));return;}
      request.onupgradeneeded=()=>{if(!request.result.objectStoreNames.contains(STORE))request.result.createObjectStore(STORE,{keyPath:'id'});};
      request.onerror=()=>{settled=true;reject(new Error('Voice storage could not be opened. You can still upload a WAV for this visit.'));};
      request.onblocked=()=>{settled=true;reject(new Error('Close older studio tabs, then refresh to open your saved voices.'));};
      request.onsuccess=()=>{const db=request.result;if(settled){db.close();return;}settled=true;db.onversionchange=()=>db.close();resolve(db);};
    });return this.opening;
  }
  async request(mode,operation){
    const db=await this.open();
    return new Promise((resolve,reject)=>{
      let transaction,result;
      try{transaction=db.transaction(STORE,mode);result=operation(transaction.objectStore(STORE),transaction);}catch(error){reject(new Error('Voice storage is unavailable. Reload the page or upload a WAV for this visit.'));return;}
      transaction.oncomplete=()=>resolve(typeof result==='function'?result():result?.result);
      transaction.onerror=transaction.onabort=()=>reject(new Error('The voice could not be saved or loaded. Check browser storage space.'));
    });
  }
  async list(){
    const rows=await this.request('readonly',store=>store.getAll());
    return (Array.isArray(rows)?rows:[]).filter(row=>savedVoiceId(row?.id)&&NAME(row.name)).slice(0,MAX_SAVED_VOICES)
      .map(row=>({id:row.id,name:NAME(row.name)})).sort((a,b)=>a.name.localeCompare(b.name));
  }
  async get(id){
    if(!savedVoiceId(id))throw new Error('Choose one of your saved voices.');
    const row=await this.request('readonly',store=>store.get(id));
    if(!row||row.id!==id||!NAME(row.name)||typeof row.transcript!=='string'||!row.transcript.trim()||row.transcript.length>500)
      throw new Error('This saved voice is unavailable. Choose another voice or upload its WAV again.');
    await validateVoiceWav(row.audio);
    return {...row,name:NAME(row.name)};
  }
  async save({name,audio,transcript}){
    if(typeof name!=='string'||!name.trim()||name.trim().length>80)throw new Error('Give this voice a name, up to 80 characters.');
    if(typeof transcript!=='string'||!transcript.trim()||transcript.length>500)throw new Error('Enter the exact words spoken in the reference, up to 500 characters.');
    const validated=await validateVoiceWav(audio),id='voice-'+this.uuid();
    if(!savedVoiceId(id))throw new Error('Your browser could not create a saved voice. Refresh and try again.');
    const row={id,name:name.trim(),audio:new Blob([validated.bytes],{type:'audio/wav'}),transcript:transcript.trim()};
    let capacity=false;
    await this.request('readwrite',(store,transaction)=>{
      const count=store.count();count.onsuccess=()=>{
        if(count.result>=MAX_SAVED_VOICES){capacity=true;transaction.abort();return;}
        store.add(row);
      };
    }).catch(error=>{if(capacity)throw new Error('You have 20 saved voices in this browser. Use an existing voice or upload a WAV for this visit.');throw error;});
    return row;
  }
}

// One shared set of controls serves the ordinary character page and gallery.
export class SavedVoices{
  constructor({document,store=new VoiceReferenceStore(),onChange=()=>{},canEdit=()=>true,onPreview=()=>{}}){
    this.document=document;this.store=store;this.onChange=onChange;this.canEdit=canEdit;this.onPreview=onPreview;
    this.selectedId='';this.audio=null;this.loading=false;this.epoch=0;this.available=true;this.records=[];
    this.get('saved-voice').addEventListener('change',()=>{if(this.canEdit())void this.load(this.get('saved-voice').value,true);else this.render();});
    this.get('save-voice').addEventListener('click',()=>{if(this.canEdit())void this.save();});
  }
  get(id){return this.document.getElementById(id);}
  note(message){this.get('saved-voice-status').textContent=message;}
  render(){
    const picker=this.get('saved-voice');picker.replaceChildren();
    const upload=this.document.createElement('option');upload.value='';upload.textContent='Upload a new reference';picker.append(upload);
    for(const row of this.records){const option=this.document.createElement('option');option.value=row.id;option.textContent=row.name;picker.append(option);}
    if(this.selectedId&&!this.records.some(row=>row.id===this.selectedId)){const option=this.document.createElement('option');option.value=this.selectedId;option.textContent='Saved voice unavailable';picker.append(option);}
    picker.value=this.selectedId;
  }
  async init(){
    try{this.records=await this.store.list();this.available=true;this.render();}
    catch(error){this.available=false;this.note(error.message);}
  }
  clear({notify=false}={}){
    this.epoch++;this.selectedId='';this.audio=null;this.loading=false;this.render();
    if(notify){this.note('Choose another saved voice or upload a reference.');this.onChange();}
  }
  useUpload(){
    this.clear();this.get('reference-text').value='';this.get('save-voice-name').value='';
    this.note('Enter this clip’s exact words. Save the voice to use it again.');
  }
  async load(id,notify=false){
    if(!id){this.clear();this.get('voice-reference').value='';this.get('reference-text').value='';this.onPreview(null);if(notify)this.onChange();return false;}
    const selected=savedVoiceId(id);if(!selected)return false;
    const epoch=++this.epoch;this.selectedId=selected;this.audio=null;this.loading=true;this.render();this.note('Loading saved voice…');
    try{
      const row=await this.store.get(selected);
      if(epoch!==this.epoch)return false;
      this.get('voice-reference').value='';this.get('reference-text').value=row.transcript;this.get('save-voice-name').value=row.name;
      this.audio=row.audio;this.loading=false;this.onPreview(row.audio);this.note(`Using ${row.name}. Saved in this browser.`);
      if(notify)this.onChange();return true;
    }catch(error){if(epoch===this.epoch){this.loading=false;this.note(error.message);if(notify)this.onChange();}return false;}
  }
  async save(){
    const epoch=this.epoch;
    const audio=this.get('voice-reference').files?.[0]||this.audio;
    if(!audio){this.note('Upload a WAV reference first, or choose a saved voice.');return false;}
    this.get('save-voice').disabled=true;this.note('Saving voice in this browser…');
    try{
      const row=await this.store.save({name:this.get('save-voice-name').value,audio,transcript:this.get('reference-text').value});
      this.records=await this.store.list();
      if(epoch!==this.epoch)return false;
      this.selectedId=row.id;this.audio=row.audio;this.get('voice-reference').value='';this.render();
      this.onPreview(row.audio);this.note(`Saved ${row.name} in this browser.`);this.onChange();return true;
    }catch(error){this.note(error.message);return false;}
    finally{this.get('save-voice').disabled=!this.canEdit()||!this.available;}
  }
  source(){
    if(this.loading)throw new Error('Your saved voice is still loading. Wait a moment, then start the call.');
    return this.get('voice-reference').files?.[0]||this.audio;
  }
  id(){return this.selectedId;}
  setBusy(busy){for(const id of ['saved-voice','save-voice-name','save-voice'])this.get(id).disabled=!!busy||(id==='save-voice'&&!this.available);}
}
