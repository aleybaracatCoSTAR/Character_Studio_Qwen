export const CHARACTER_LIBRARY_KEY='live-character-library-v1';
const CHARACTER_FIELDS=['name','role','about','goal','environment','voice_style','rules','memories','beat','speaker'];
const TRIGGER_FIELDS=['action','condition','emotion','behaviour','spoken_cue','delivery'];
const BEAT_FIELDS=['title','goal','note','emotion','delivery','min_seconds','max_seconds'];
const copy=value=>JSON.parse(JSON.stringify(value));
const strings=(source,fields)=>Object.fromEntries(fields.map(key=>[key,typeof source?.[key]==='string'?source[key]:'']));

// Persist authored settings only. Microphone audio, uploaded references, encoded
// files and reference transcripts are never part of a library record.
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
  if(value.archive&&typeof value.archive==='object')profile.archive={enabled:value.archive.enabled===true,
    ...(value.archive.scope==='library'?{scope:'library'}:{}),
    entry_ids:[...new Set(Array.isArray(value.archive.entry_ids)?value.archive.entry_ids.filter(id=>typeof id==='string'&&/^[a-f0-9]{32}$/.test(id)):[])].slice(0,50)};
  return profile;
}

export class CharacterLibrary {
  constructor({storage,current,presets=[],key=CHARACTER_LIBRARY_KEY,legacyWrites=true}){
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
    this.addPresets(presets);this.persist();
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
  list(){return [...this.profiles.values()].map(profile=>({id:profile.id,name:profile.character.name}));}
  active(){return copy(this.profiles.get(this.activeId));}
  preset(id=this.activeId){const profile=this.presets.get(id);return profile?copy(profile):null;}
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
