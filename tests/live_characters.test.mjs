import test from 'node:test';
import assert from 'node:assert/strict';
import {CHARACTER_LIBRARY_KEY,CharacterLibrary,characterProfile} from '../frontend/live-characters.mjs';

function storage(){
  const values=new Map();return {values,getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,value)};
}
function current(){return {
  character:{name:'Karim',role:'Existing saved role',about:'Existing biography — preserve exactly.',goal:'Existing personal goal.',
    environment:'Existing place.',voice_style:'Existing speaking style.',rules:'Existing boundaries.',memories:'Existing memory.',beat:'Existing moment.',speaker:'Ethan'},
  voice:{preset:'custom',emotion:'calm',event:'sigh',direction:'Existing soft delivery.'},
  director:{enabled:false,notes:'Existing private direction.'},
  trigger:{action:'Existing action',condition:'Existing condition.',emotion:'anxious',behaviour:'Existing response.',spoken_cue:'Existing words.',delivery:'Existing urgency.'},
  narrative:{enabled:true,mode:'timed',beats:[{title:'Existing story',goal:'Existing goal.',note:'Existing private note.',emotion:'sad',delivery:'Existing vocal direction.',min_seconds:12,max_seconds:70}]}
};}
function kaden(){return {id:'kaden',character:{...current().character,name:'KADEN',about:'The authored preset biography.'},
  voice:{preset:'male',emotion:'calm',event:'none',direction:'Preset delivery.'},director:{enabled:true,notes:'Preset direction.'},trigger:null,
  narrative:{enabled:true,mode:'manual',beats:Array.from({length:5},(_,index)=>({title:`Preset beat ${index+1}`,goal:`Goal ${index+1}`,note:`Private note ${index+1}`,emotion:'calm',delivery:'Steady.',min_seconds:0,max_seconds:90}))}};}

test('first migration preserves the actual current character and every setting without selecting the added preset',()=>{
  const saved=storage(),original=current();
  saved.setItem('live-character-v1',JSON.stringify(original.character));
  const library=new CharacterLibrary({storage:saved,current:original,presets:[kaden()]});
  assert.equal(library.activeId,'local-character');
  assert.deepEqual(library.active(),{id:'local-character',...original});
  assert.deepEqual(library.list(),[{id:'local-character',name:'Karim'},{id:'kaden',name:'KADEN'}]);
  assert.equal(saved.getItem('live-character-v1'),JSON.stringify(original.character));
  assert.equal(library.preset('local-character'),null);
});

test('switching saves current edits first and restores isolated voice, direction, trigger and narrative settings',()=>{
  const library=new CharacterLibrary({storage:storage(),current:current(),presets:[kaden()]});
  const edited=library.active();edited.character.memories='An edited saved memory.';edited.voice.direction='A new delivery.';
  const selected=library.switchTo('kaden',edited);
  assert.equal(selected.voice.preset,'male');assert.equal(selected.trigger,null);
  assert.equal(selected.narrative.mode,'manual');assert.equal(selected.narrative.beats.length,5);
  const restored=library.switchTo('local-character',selected);
  assert.deepEqual(restored,edited);assert.equal(restored.character.name,'Karim');
  assert.equal(restored.trigger.action,'Existing action');assert.equal(restored.narrative.mode,'timed');
});

test('a reload restores the saved active ID without overwriting Karim with legacy keys from Kaden',()=>{
  const saved=storage();const first=new CharacterLibrary({storage:saved,current:current(),presets:[kaden()]});
  const selected=first.switchTo('kaden',first.active());
  const reloaded=new CharacterLibrary({storage:saved,current:selected,presets:[kaden()]});
  assert.equal(reloaded.activeId,'kaden');assert.equal(reloaded.active().character.name,'KADEN');
  const previous=reloaded.switchTo('local-character');
  assert.equal(previous.character.name,'Karim');assert.equal(previous.character.about,current().character.about);
  assert.deepEqual(previous.voice,current().voice);
});

test('edited seeded characters survive reload and newer preset defaults do not overwrite them',()=>{
  const saved=storage(),library=new CharacterLibrary({storage:saved,current:current(),presets:[kaden()]});
  const selected=library.switchTo('kaden');selected.character.name='Edited Kaden';selected.character.goal='An edited goal.';
  selected.narrative.beats[0].note='Edited first private note.';selected.voice.emotion='excited';library.save(selected);
  const updated=kaden();updated.character.about='New upstream biography.';
  const reloaded=new CharacterLibrary({storage:saved,current:current(),presets:[updated]});
  assert.deepEqual(reloaded.active(),selected);assert.equal(reloaded.preset().character.about,'New upstream biography.');
  assert.equal(reloaded.preset().narrative.beats[0].note,'Private note 1');
});

test('adding presets is nondestructive and does not change the current selection or authored profile',()=>{
  const library=new CharacterLibrary({storage:storage(),current:current()});
  const before=library.active();library.addPresets([kaden()]);
  assert.deepEqual(library.active(),before);assert.equal(library.list().length,2);
  library.addPresets([kaden(),{...kaden(),id:'future-example',character:{...kaden().character,name:'Future example'}}]);
  assert.equal(library.list().length,3);assert.equal(library.activeId,'local-character');
});

test('returned profiles, reset templates and input objects cannot mutate another stored character',()=>{
  const original=current(),preset=kaden(),library=new CharacterLibrary({storage:storage(),current:original,presets:[preset]});
  original.character.about='Changed outside';preset.narrative.beats[0].note='Changed outside';
  const selected=library.active();selected.director.notes='Changed clone';
  const template=library.preset('kaden');template.narrative.beats[0].note='Changed template clone';
  assert.equal(library.active().character.about,current().character.about);
  assert.equal(library.active().director.notes,current().director.notes);
  assert.equal(library.preset('kaden').narrative.beats[0].note,'Private note 1');
});

test('audio references, binary data and reference transcripts are never serialized into the library',()=>{
  const saved=storage(),profile=current();
  profile.voice.reference_audio='data:audio/wav;base64,PRIVATE_AUDIO_BYTES';profile.voice.reference_text='PRIVATE_REFERENCE_TRANSCRIPT';
  profile.voice.ref_audio='data:audio/wav;base64,PRIVATE_RAW_AUDIO';profile.voice.audio=new Uint8Array([1,2,3]);
  profile.reference_audio='PRIVATE_ROOT_AUDIO';profile.character.avatar_audio='PRIVATE_CHARACTER_AUDIO';
  profile.narrative.beats[0].reference_audio='PRIVATE_BEAT_AUDIO';
  const library=new CharacterLibrary({storage:saved,current:profile,presets:[kaden()]});
  const serialized=saved.getItem(CHARACTER_LIBRARY_KEY);
  assert.doesNotMatch(serialized,/PRIVATE_|base64|reference_audio|reference_text|ref_audio|avatar_audio/);
  assert.equal(library.active().voice.preset,'custom');assert.equal(library.active().voice.direction,current().voice.direction);
});

test('saved library loading also strips unexpected reference fields from older records',()=>{
  const saved=storage(),profile={id:'local-character',...current()};profile.voice.reference_audio='PRIVATE_PERSISTED_AUDIO';
  saved.setItem(CHARACTER_LIBRARY_KEY,JSON.stringify({version:1,activeId:'local-character',profiles:[profile]}));
  const library=new CharacterLibrary({storage:saved,current:current()});
  assert.doesNotMatch(saved.getItem(CHARACTER_LIBRARY_KEY),/PRIVATE_PERSISTED_AUDIO/);
  assert.equal(library.active().character.name,'Karim');
});

test('storage read/write failures leave switching usable in memory',()=>{
  const broken={getItem(){throw new Error('Blocked.');},setItem(){throw new Error('Quota.');}};
  const library=new CharacterLibrary({storage:broken,current:current(),presets:[kaden()]});
  assert.equal(library.storageError,true);assert.equal(library.active().character.name,'Karim');
  assert.equal(library.switchTo('kaden',library.active()).character.name,'KADEN');
  assert.equal(library.switchTo('local-character').character.about,current().character.about);
  const unavailable=new CharacterLibrary({storage:null,current:current(),presets:[kaden()]});
  assert.equal(unavailable.storageError,true);assert.equal(unavailable.list().length,2);
});

test('an unavailable preset API can preserve a saved library and the exact current workflow',()=>{
  const saved=storage(),first=new CharacterLibrary({storage:saved,current:current(),presets:[kaden()]});
  const before=first.active();
  const offline=new CharacterLibrary({storage:saved,current:current(),presets:[]});
  assert.deepEqual(offline.active(),before);assert.equal(offline.list().length,2);assert.equal(offline.preset(),null);
});

test('missing active IDs and corrupt libraries recover from real saved data rather than inventing a biography',()=>{
  const saved=storage();saved.setItem(CHARACTER_LIBRARY_KEY,'invalid json');
  const migrated=new CharacterLibrary({storage:saved,current:current(),presets:[kaden()]});
  assert.equal(migrated.active().character.about,current().character.about);
  const snapshot=JSON.parse(saved.getItem(CHARACTER_LIBRARY_KEY));snapshot.activeId='missing';
  saved.setItem(CHARACTER_LIBRARY_KEY,JSON.stringify(snapshot));
  const reloaded=new CharacterLibrary({storage:saved,current:kaden(),presets:[kaden()]});
  assert.equal(reloaded.active().character.name,'Karim');
});

test('unknown selections and invalid presets cannot erase or rename the current character',()=>{
  const library=new CharacterLibrary({storage:storage(),current:current(),presets:[null,{id:'bad',character:{}},{...kaden(),id:'../../elsewhere'}]});
  const before=library.active();assert.equal(library.switchTo('missing',kaden()),null);
  assert.deepEqual(library.active(),before);assert.equal(library.list().length,1);
  assert.equal(characterProfile({character:{name:'Karim'}},'../bad'),null);
});

test('blank in-progress names remain editable drafts without changing the remaining biography',()=>{
  const library=new CharacterLibrary({storage:storage(),current:current()});
  const draft=library.active();draft.character.name='';library.save(draft);
  assert.equal(library.active().character.name,'');assert.equal(library.active().character.about,current().character.about);
});

test('quota failure preserves Karim legacy keys even if smaller Kaden legacy writes would succeed',()=>{
  const original=current(),values=new Map([
    ['live-character-v1',JSON.stringify(original.character)],['live-voice-preset-v1',original.voice.preset],
    ['live-delivery-v1',JSON.stringify(original.voice)],['live-director-v1',JSON.stringify(original.director)],
    ['live-trigger-v1',JSON.stringify(original.trigger)],['live-story-v1',JSON.stringify(original.narrative)]]);
  const before=new Map(values);let quota=true;
  const saved={getItem:key=>values.get(key)??null,setItem:(key,value)=>{
    if(key===CHARACTER_LIBRARY_KEY&&quota)throw new Error('Library exceeds quota.');values.set(key,value);
  }};
  const library=new CharacterLibrary({storage:saved,current:original,presets:[kaden()]});
  assert.equal(library.storageError,true);assert.equal(library.writeLegacy(),false);
  const selected=library.switchTo('kaden',library.active());
  assert.equal(selected.character.name,'KADEN');assert.equal(library.writeLegacy(),false);
  assert.deepEqual(values,before); // No scattered legacy handler may bypass this guard.
  quota=false;assert.equal(library.persist(),true);assert.equal(library.writeLegacy(),true);
  assert.equal(JSON.parse(values.get('live-character-v1')).name,'KADEN');
  const reloaded=new CharacterLibrary({storage:saved,current:kaden(),presets:[kaden()]});
  assert.equal(reloaded.switchTo('local-character').character.about,original.character.about);
});

test('normal legacy compatibility writes only sanitized active settings after the library is saved',()=>{
  const saved=storage(),library=new CharacterLibrary({storage:saved,current:current(),presets:[kaden()]});
  library.switchTo('kaden');assert.equal(library.writeLegacy(),true);
  assert.deepEqual(JSON.parse(saved.getItem('live-character-v1')),kaden().character);
  assert.equal(saved.getItem('live-voice-preset-v1'),'male');
  assert.equal(JSON.parse(saved.getItem('live-trigger-v1')),null);
  assert.deepEqual(JSON.parse(saved.getItem('live-story-v1')),kaden().narrative);
  assert.equal(JSON.parse(saved.getItem(CHARACTER_LIBRARY_KEY)).profiles[0].character.name,'Karim');
});
