import {savedVoiceId} from './live-voices.mjs';
export const CHARACTER_LIBRARY_KEY='live-character-library-v1';
const CHARACTER_FIELDS=['name','role','about','goal','environment','voice_style','rules','memories','beat','speaker'];
const TRIGGER_FIELDS=['action','condition','emotion','behaviour','spoken_cue','delivery'];
const BEAT_FIELDS=['title','goal','note','emotion','delivery','min_seconds','max_seconds'];
const LEGACY_KEYS={'live-character-v1':'recovered-live-character','qwen-character-v1':'recovered-qwen-character'};
const CHARACTER_LIMITS={name:80,role:120,about:2400,goal:500,environment:700,voice_style:500,rules:1000,memories:1200,beat:500};
const copy=value=>JSON.parse(JSON.stringify(value));
const strings=(source,fields)=>Object.fromEntries(fields.map(key=>[key,typeof source?.[key]==='string'?source[key]:'']));

// Persist authored settings only. Microphone audio, uploaded references, encoded
// files and reference transcripts are never part of a library record. An opaque
// saved voice ID may reference a separate browser-local IndexedDB recording.
export function characterProfile(value,id=value?.id){
  if(!value||typeof value!=='object'||typeof id!=='string'||!/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$/.test(id)
      ||!value.character||typeof value.character.name!=='string')return null;
  const voice=value.voice??{},director=value.director??{},narrative=value.narrative??{};
  const preset=['automatic','female','male','custom'].includes(voice.preset)?voice.preset:'automatic';
  const profile = {id,character:strings(value.character,CHARACTER_FIELDS),
    voice:{preset,emotion:typeof voice.emotion==='string'?voice.emotion:'neutral',
      event:typeof voice.event==='string'?voice.event:'none',direction:typeof voice.direction==='string'?voice.direction:''},
    director:{enabled:typeof director.enabled==='boolean'?director.enabled:true,notes:typeof director.notes==='string'?director.notes:''},
    trigger:value.trigger&&typeof value.trigger==='object'?strings(value.trigger,TRIGGER_FIELDS):null,
    narrative:{enabled:!!narrative.enabled,mode:['manual','timed','director'].includes(narrative.mode)?narrative.mode:'manual',
      beats:Array.isArray(narrative.beats)?narrative.beats.slice(0,12).map(beat=>Object.fromEntries(BEAT_FIELDS.map(key=>
        [key,['min_seconds','max_seconds'].includes(key)?(Number.isFinite(beat?.[key])?beat[key]:key==='min_seconds'?15:90)
          :typeof beat?.[key]==='string'?beat[key]:'']))):[]}};
  if(savedVoiceId(voice.saved_voice_id))profile.voice.saved_voice_id=voice.saved_voice_id;
  if(value.archive&&typeof value.archive==='object')profile.archive={enabled:value.archive.enabled===true,
    ...(value.archive.scope==='library'?{scope:'library'}:{}),
    entry_ids:[...new Set(Array.isArray(value.archive.entry_ids)?value.archive.entry_ids.filter(id=>typeof id==='string'&&/^[a-f0-9]{32}$/.test(id)):[])].slice(0,50)};
  const collections=[...new Set(Array.isArray(value.archive?.collection_ids)?value.archive.collection_ids
    .filter(id=>typeof id==='string'&&/^[a-z0-9][a-z0-9-]{0,63}$/.test(id)):[])].slice(0,8);
  if(collections.length)profile.archive={enabled:value.archive.enabled===true,scope:'library',entry_ids:[],collection_ids:collections};
  return profile;
}

function recoveryProfile(value,id){
  if(!value||typeof value!=='object'||Array.isArray(value)||typeof value.name!=='string'
      ||!value.name.trim()||value.name.length>80)return null;
  for(const [key,maximum] of Object.entries(CHARACTER_LIMITS)){
    if(value[key]!==undefined&&(typeof value[key]!=='string'||value[key].length>maximum))return null;
  }
  if(value.speaker!==undefined&&value.speaker!==''&&!['Chelsie','Ethan','Aiden'].includes(value.speaker))return null;
  const character={...strings(value,CHARACTER_FIELDS),speaker:value.speaker||'Chelsie'};
  if(!CHARACTER_FIELDS.some(key=>key!=='name'&&key!=='speaker'&&character[key].trim()))return null;
  // The older Qwen save has character facts only. Other live settings were
  // stored separately and may now belong to a different active character.
  return characterProfile({character,voice:{preset:'automatic',emotion:'neutral',event:'none',direction:''},
    director:{enabled:true,notes:''},trigger:null,narrative:{enabled:false,mode:'manual',beats:[]},
    archive:{enabled:false,entry_ids:[]}},id);
}

const characterFacts=profile=>JSON.stringify(CHARACTER_FIELDS.map(key=>{
  const value=profile.character[key]||(key==='speaker'?'Chelsie':'');
  return value.trim().replace(/\s+/g,' ');
}));

// Only real legacy records are recovery evidence. Never pass the current form
// or the fetched example here: either may already contain the default Anna.
export function legacyRecoveryProfiles(storage){
  const profiles=[];
  for(const [key,id] of Object.entries(LEGACY_KEYS)){
    let value;
    try{value=JSON.parse(storage?.getItem(key)??'null');}catch(error){continue;}
    const profile=recoveryProfile(value,id);if(profile)profiles.push(profile);
  }
  return profiles;
}

export class CharacterLibrary {
  constructor({storage,current,presets=[],key=CHARACTER_LIBRARY_KEY,legacyWrites=true,recoveryProfiles=[]}){
    this.storage=storage;this.storageError=false;this.profiles=new Map();this.presets=new Map();this.activeId=null;
    this.key=key;this.legacyWrites=legacyWrites;
    let saved;
    try{saved=JSON.parse(storage?.getItem(this.key)??'null');}catch(error){this.storageError=true;}
    if(saved?.version===1&&Array.isArray(saved.profiles))for(const value of saved.profiles){
      const profile=characterProfile(value);if(profile&&!this.profiles.has(profile.id))this.profiles.set(profile.id,profile);
    }
    if(this.profiles.size)this.activeId=this.profiles.has(saved.activeId)?saved.activeId:this.profiles.keys().next().value;
    else{
      const migrated=characterProfile(current,'local-character');
      if(!migrated)throw new Error('A current character is needed before opening the character library.');
      this.profiles.set(migrated.id,migrated);this.activeId=migrated.id;
    }
    this.addPresets(presets);
    if(this.key===CHARACTER_LIBRARY_KEY&&this.legacyWrites)this._recover(recoveryProfiles);
    this.persist();
  }
  persist(){
    if(!this.storage){this.storageError=true;return false;}
    try{
      this.storage.setItem(this.key,JSON.stringify({version:1,activeId:this.activeId,profiles:[...this.profiles.values()]}));
      this.storageError=false;return true;
    }catch(error){this.storageError=true;return false;}
  }
  addPresets(presets){
    for(const value of Array.isArray(presets)?presets:[]){
      const profile=characterProfile(value);if(!profile)continue;
      this.presets.set(profile.id,profile);
      if(!this.profiles.has(profile.id))this.profiles.set(profile.id,copy(profile));
    }
  }
  _recover(profiles){
    if(this.key!==CHARACTER_LIBRARY_KEY||!this.legacyWrites)return;
    const sources=new Set(Object.values(LEGACY_KEYS));
    const known=new Set([...this.profiles.values()].map(characterFacts));
    for(const candidate of Array.isArray(profiles)?profiles:[]){
      if(!sources.has(candidate?.id))continue;
      const profile=recoveryProfile(candidate.character,candidate.id);
      if(!profile)continue;
      const facts=characterFacts(profile);if(known.has(facts))continue;
      let id=profile.id,index=2;
      while(this.profiles.has(id))id=profile.id+'-'+index++;
      profile.id=id;this.profiles.set(id,profile);known.add(facts);
    }
  }
  list(){return [...this.profiles.values()].map(profile=>({id:profile.id,name:profile.character.name}));}
  active(){return copy(this.profiles.get(this.activeId));}
  preset(id=this.activeId){const profile=this.presets.get(id);return profile?copy(profile):null;}
  create({name,role='',archiveEnabled=false}={}){
    if(typeof name!=='string'||!name.trim()||name.trim().length>80)
      throw new Error('Enter a character name between 1 and 80 characters.');
    if(typeof role!=='string'||role.trim().length>120)
      throw new Error('Keep the character role within 120 characters.');
    let id;
    // Keep IDs separate from editable names and never replace an existing
    // profile, even if the browser returns an already-used UUID.
    for(let attempt=0;attempt<8;attempt++){
      let uuid;
      try{uuid=globalThis.crypto?.randomUUID();}catch(error){break;}
      if(typeof uuid!=='string'||!/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/i.test(uuid))break;
      const candidate='character-'+uuid.toLowerCase();
      if(!this.profiles.has(candidate)){id=candidate;break;}
    }
    if(!id)throw new Error('Your browser could not create a character. Refresh this secure tab and try again.');
    const profile=characterProfile({
      character:{...strings({},CHARACTER_FIELDS),name:name.trim(),role:role.trim(),speaker:'Chelsie'},
      voice:{preset:'automatic',emotion:'neutral',event:'none',direction:''},
      director:{enabled:true,notes:''},trigger:null,
      narrative:{enabled:false,mode:'manual',beats:[]},
      archive:{enabled:archiveEnabled===true,...(archiveEnabled===true?{scope:'library'}:{}),entry_ids:[]}
    },id);
    this.profiles.set(id,profile);this.activeId=id;this.persist();return this.active();
  }
  save(value){
    const profile=characterProfile(value,this.activeId);if(!profile)return false;
    this.profiles.set(this.activeId,profile);return this.persist();
  }
  writeLegacy(){
    // Never replace the sole surviving legacy copy with a different character
    // until the complete library, including the prior character, was saved.
    if(!this.legacyWrites||this.storageError||!this.storage)return false;
    const profile=this.active();
    try{
      this.storage.setItem('live-character-v1',JSON.stringify(profile.character));
      this.storage.setItem('live-voice-preset-v1',profile.voice.preset);
      this.storage.setItem('live-delivery-v1',JSON.stringify({emotion:profile.voice.emotion,event:profile.voice.event,direction:profile.voice.direction}));
      this.storage.setItem('live-director-v1',JSON.stringify(profile.director));
      this.storage.setItem('live-trigger-v1',JSON.stringify(profile.trigger));
      this.storage.setItem('live-story-v1',JSON.stringify(profile.narrative));
      return true;
    }catch(error){this.storageError=true;return false;}
  }
  switchTo(id,current){
    if(!this.profiles.has(id))return null;
    if(current)this.save(current);
    this.activeId=id;this.persist();return this.active();
  }
}
