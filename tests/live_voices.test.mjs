import test from 'node:test';
import assert from 'node:assert/strict';
import {VoiceReferenceStore,SavedVoices,validateVoiceWav,MAX_VOICE_BYTES} from '../frontend/live-voices.mjs';
import {CharacterLibrary,characterProfile} from '../frontend/live-characters.mjs';

const uuid=index=>`00000000-0000-4000-8000-${String(index).padStart(12,'0')}`;
function wav({seconds=2,rate=8000,channels=1,bits=16,format=1}={}){
  const bytes=new ArrayBuffer(44+Math.floor(seconds*rate*channels*(bits/8))),view=new DataView(bytes);
  const word=(offset,text)=>{for(let i=0;i<text.length;i++)view.setUint8(offset+i,text.charCodeAt(i));};
  word(0,'RIFF');view.setUint32(4,bytes.byteLength-8,true);word(8,'WAVE');word(12,'fmt ');view.setUint32(16,16,true);
  view.setUint16(20,format,true);view.setUint16(22,channels,true);view.setUint32(24,rate,true);
  view.setUint32(28,rate*channels*bits/8,true);view.setUint16(32,channels*bits/8,true);view.setUint16(34,bits,true);
  word(36,'data');view.setUint32(40,bytes.byteLength-44,true);
  return new Blob([bytes],{type:'audio/wav'});
}

// Minimal asynchronous IndexedDB adapter: transactions finish only after all
// queued requests, including requests created in onsuccess, have settled.
class MemoryIndexedDB{
  constructor(){this.databases=new Map();}
  open(name){
    const request={};queueMicrotask(()=>{
      const fresh=!this.databases.has(name);if(fresh)this.databases.set(name,new Map());
      const rows=this.databases.get(name);
      request.result={objectStoreNames:{contains:()=>!fresh},createObjectStore(){},close(){},transaction(){
        const transaction={pending:0,aborted:false,completed:false,abort(){this.aborted=true;queueMicrotask(()=>this.onabort?.());}};
        const operation=callback=>{
          const result={};transaction.pending++;
          queueMicrotask(()=>{if(!transaction.aborted){try{result.result=callback();result.onsuccess?.();}catch(error){transaction.abort();}}
            transaction.pending--;queueMicrotask(()=>{if(!transaction.aborted&&!transaction.completed&&!transaction.pending){transaction.completed=true;transaction.oncomplete?.();}});});return result;
        };
        transaction.objectStore=()=>({getAll:()=>operation(()=>structuredClone([...rows.values()])),get:id=>operation(()=>structuredClone(rows.get(id))),
          count:()=>operation(()=>rows.size),add:row=>operation(()=>{if(rows.has(row.id))throw new Error('Duplicate key');rows.set(row.id,structuredClone(row));})});return transaction;
      }};
      if(fresh)request.onupgradeneeded?.();request.onsuccess?.();
    });return request;
  }
}
function storeFactory(){const indexedDB=new MemoryIndexedDB();let count=0;return ()=>new VoiceReferenceStore({indexedDB,uuid:()=>uuid(++count)});}
const savedInput=()=>({name:'Robin’s voice',audio:wav(),transcript:'Exactly these words were spoken.'});

class Element{
  constructor(){this.value='';this.files=[];this.children=[];this.listeners={};this.disabled=false;this.textContent='';}
  addEventListener(type,callback){this.listeners[type]=callback;}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=[...children];}
}
function controller(store){
  const nodes=new Map();for(const id of ['saved-voice','save-voice','save-voice-name','saved-voice-status','voice-reference','reference-text'])nodes.set(id,new Element());
  const previews=[],changes=[];let editable=true;
  const voices=new SavedVoices({document:{getElementById:id=>nodes.get(id),createElement:()=>new Element()},store,
    onChange:()=>changes.push(voices.id()),onPreview:audio=>previews.push(audio),canEdit:()=>editable});
  return {voices,nodes,previews,changes,setEdit(value){editable=value;}};
}

test('saved voice WAV validation matches the backend PCM16 duration/channel/rate envelope',async()=>{
  for(const options of [{},{seconds:15,rate:48000},{seconds:3,channels:2,rate:16000}]){
    const audio=wav(options),valid=await validateVoiceWav(audio);assert.equal(valid.seconds,options.seconds??2);assert.equal(valid.bytes.byteLength,audio.size);
  }
  for(const audio of [wav({seconds:1}),wav({seconds:16}),wav({format:3,bits:32}),wav({rate:96000}),wav({channels:3}),
    new Blob(['not a WAV'],{type:'audio/wav'}),new Blob([new Uint8Array(MAX_VOICE_BYTES+1)])]){
    await assert.rejects(validateVoiceWav(audio));
  }
  const bytes=await wav().arrayBuffer();new DataView(bytes).setUint32(40,bytes.byteLength,true);
  await assert.rejects(validateVoiceWav(new Blob([bytes])),/incomplete/);
});

test('recordings and exact transcripts survive a new store instance in the same browser database',async()=>{
  const open=storeFactory(),first=open(),input=savedInput(),saved=await first.save(input),second=open();
  assert.equal((await second.list())[0].name,input.name);
  const loaded=await second.get(saved.id);assert.equal(loaded.transcript,input.transcript);
  assert.deepEqual(new Uint8Array(await loaded.audio.arrayBuffer()),new Uint8Array(await input.audio.arrayBuffer()));
  assert.equal(loaded.audio.type,'audio/wav');
  await assert.rejects(second.get('voice-unknown'),/Choose one/);
  await assert.rejects(second.get('voice-'+uuid(999)),/unavailable/);
});

test('invalid audio/name/transcript cannot become saved voices and quota failure leaves prior clips intact',async()=>{
  const open=storeFactory(),store=open();
  for(const input of [{...savedInput(),name:''},{...savedInput(),transcript:''},{...savedInput(),audio:wav({seconds:1})}])await assert.rejects(store.save(input));
  assert.deepEqual(await store.list(),[]);
  for(let index=0;index<20;index++)await store.save({...savedInput(),name:'Voice '+index});
  await assert.rejects(store.save(savedInput()),/20 saved voices/);assert.equal((await store.list()).length,20);
});

test('unavailable browser persistence is explicit and leaves ordinary uploaded-reference use available',async()=>{
  const {voices,nodes}=controller(new VoiceReferenceStore({indexedDB:null}));
  await voices.init();assert.equal(voices.available,false);assert.match(nodes.get('saved-voice-status').textContent,/cannot save voices/);
  const audio=wav();nodes.get('voice-reference').files=[audio];voices.useUpload();assert.equal(voices.source(),audio);assert.equal(voices.id(),'');
});

test('choosing a saved voice restores its WAV/transcript/preview and clears the unsaved file choice',async()=>{
  const store=storeFactory()(),saved=await store.save(savedInput()),{voices,nodes,previews,changes}=controller(store);
  await voices.init();assert.equal(nodes.get('saved-voice').children[1].textContent,'Robin’s voice');
  nodes.get('voice-reference').value='old.wav';await voices.load(saved.id,true);
  assert.equal(voices.id(),saved.id);assert.equal(nodes.get('voice-reference').value,'');
  assert.equal(nodes.get('reference-text').value,savedInput().transcript);assert.equal(previews.length,1);
  assert.ok(voices.source() instanceof Blob);assert.deepEqual(changes,[saved.id]);
});

test('an older asynchronous voice load cannot overwrite another profile or a changed voice preset',async()=>{
  let release;const pending=new Promise(resolve=>release=resolve),saved={...savedInput(),id:'voice-'+uuid(1)};
  const {voices,nodes,previews}=controller({list:async()=>[{id:saved.id,name:saved.name}],get:()=>pending});
  const loading=voices.load(saved.id);assert.throws(()=>voices.source(),/still loading/);
  voices.clear();nodes.get('reference-text').value='Next character’s exact words.';
  release(saved);assert.equal(await loading,false);assert.equal(voices.id(),'');assert.equal(voices.source(),null);
  assert.equal(nodes.get('reference-text').value,'Next character’s exact words.');assert.deepEqual(previews,[]);
});

test('saving a named upload links its new recording to the profile only after durable storage succeeds',async()=>{
  const store=storeFactory()(),{voices,nodes,changes,previews}=controller(store);
  await voices.init();const input=savedInput();nodes.get('voice-reference').files=[input.audio];nodes.get('save-voice-name').value=input.name;nodes.get('reference-text').value=input.transcript;
  assert.equal(await voices.save(),true);assert.equal((await store.list()).length,1);assert.equal(changes.length,1);
  assert.equal(voices.id(),changes[0]);assert.equal(previews.length,1);assert.match(nodes.get('saved-voice-status').textContent,/Saved.*in this browser/);
  voices.useUpload();assert.equal(voices.id(),'');
});

test('a save finishing after a profile switch cannot attach that recording to the new character',async()=>{
  let release;const pending=new Promise(resolve=>release=resolve),saved={...savedInput(),id:'voice-'+uuid(1)};
  const {voices,nodes,changes,previews}=controller({save:()=>pending,list:async()=>[{id:saved.id,name:saved.name}]});
  nodes.get('voice-reference').files=[saved.audio];nodes.get('save-voice-name').value=saved.name;nodes.get('reference-text').value=saved.transcript;
  const saving=voices.save();voices.clear();release(saved);assert.equal(await saving,false);
  assert.equal(voices.id(),'');assert.deepEqual(changes,[]);assert.deepEqual(previews,[]);
});

test('missing saved recordings remain visibly missing instead of substituting another voice',async()=>{
  const {voices,nodes}=controller({get:async()=>{throw new Error('Saved voice unavailable.');}});
  assert.equal(await voices.load('voice-'+uuid(1)),false);assert.match(nodes.get('saved-voice-status').textContent,/unavailable/);
  assert.equal(voices.source(),null);assert.equal(voices.id(),'voice-'+uuid(1));
});

test('character libraries persist only an opaque saved voice ID, never a recording or reference transcript',()=>{
  const storage={value:null,getItem(){return this.value;},setItem(_,value){this.value=value;}};
  const voice={preset:'custom',saved_voice_id:'voice-'+uuid(1),reference_audio:'PRIVATE_RECORDING',reference_text:'PRIVATE_WORDS'};
  const library=new CharacterLibrary({storage,current:{character:{name:'Robin'},voice}});
  const restored=new CharacterLibrary({storage,current:{character:{name:'Other'}}});
  assert.equal(restored.active().voice.saved_voice_id,voice.saved_voice_id);assert.doesNotMatch(storage.value,/PRIVATE_/);
  const unsafe=characterProfile({character:{name:'Robin'},voice:{saved_voice_id:'data:audio/wav;base64,PRIVATE'}} ,'robin');
  assert.equal(unsafe.voice.saved_voice_id,undefined);assert.equal(library.active().voice.preset,'custom');
});
