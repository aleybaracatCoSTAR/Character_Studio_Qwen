import test from 'node:test';
import assert from 'node:assert/strict';
import {CHARACTER_LIBRARY_KEY,CharacterLibrary,characterProfile,legacyRecoveryProfiles} from '../frontend/live-characters.mjs';

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

test('archive profiles persist only selected entry IDs and cannot copy source text or reference bytes',()=>{
  const data={...current(),archive:{enabled:true,entry_ids:['a'.repeat(32),'a'.repeat(32),'b'.repeat(32),'../bad'],text:'Private passage',reference_audio:'secret'}};
  const profile=characterProfile(data,'test');
  assert.deepEqual(profile.archive,{enabled:true,entry_ids:['a'.repeat(32),'b'.repeat(32)]});
  assert.equal(JSON.stringify(profile).includes('Private passage'),false);
  const library=new CharacterLibrary({storage:storage(),current:data,presets:[kaden()]});
  const selected=library.switchTo('kaden');assert.equal(selected.archive,undefined);
  assert.deepEqual(library.switchTo('local-character').archive,profile.archive);
});

test('whole-library archive scope persists without storing corpus text in the browser',()=>{
  const saved=storage(),data={...current(),archive:{enabled:true,scope:'library',entry_ids:['b'.repeat(32)],text:'Private source text'}};
  const library=new CharacterLibrary({storage:saved,current:data,presets:[kaden()]});
  const reloaded=new CharacterLibrary({storage:saved,current:current(),presets:[kaden()]});
  assert.deepEqual(reloaded.active().archive,{enabled:true,scope:'library',entry_ids:['b'.repeat(32)]});
  assert.equal(JSON.stringify(reloaded.active()).includes('Private source text'),false);
  library.switchTo('kaden');assert.equal(library.active().archive,undefined);
});

test('the separate archive view cannot overwrite the main library or its surviving legacy character',()=>{
  const saved=storage(),main=new CharacterLibrary({storage:saved,current:current(),presets:[kaden()]});
  main.writeLegacy();const before=saved.getItem(CHARACTER_LIBRARY_KEY),legacy=saved.getItem('live-character-v1');
  const archive=new CharacterLibrary({storage:saved,current:current(),presets:[kaden()],key:'live-archive-character-library-v1',legacyWrites:false});
  archive.switchTo('kaden');assert.equal(archive.writeLegacy(),false);
  assert.equal(saved.getItem(CHARACTER_LIBRARY_KEY),before);assert.equal(saved.getItem('live-character-v1'),legacy);
  const reloaded=new CharacterLibrary({storage:saved,current:current(),presets:[kaden()],key:'live-archive-character-library-v1',legacyWrites:false});
  assert.equal(reloaded.activeId,'kaden');assert.equal(main.activeId,'local-character');
});

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

test('creating a character starts a clean independent profile and preserves the saved current character and presets',()=>{
  const saved=storage(),library=new CharacterLibrary({storage:saved,current:current(),presets:[kaden()]});
  const previous=library.active();previous.character.memories='An edit saved immediately before creation.';library.save(previous);
  const authoredPreset=library.preset('kaden'),created=library.create({name:'  Noor  ',role:'  Night-shift archivist  '});
  assert.match(created.id,/^character-[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/);
  assert.equal(library.activeId,created.id);assert.equal(created.character.name,'Noor');assert.equal(created.character.role,'Night-shift archivist');
  assert.deepEqual(created.character,{name:'Noor',role:'Night-shift archivist',about:'',goal:'',environment:'',voice_style:'',rules:'',memories:'',beat:'',speaker:'Chelsie'});
  assert.deepEqual(created.voice,{preset:'automatic',emotion:'neutral',event:'none',direction:''});
  assert.deepEqual(created.director,{enabled:true,notes:''});assert.equal(created.trigger,null);
  assert.deepEqual(created.narrative,{enabled:false,mode:'manual',beats:[]});assert.deepEqual(created.archive,{enabled:false,entry_ids:[]});
  assert.equal(library.preset(),null);assert.deepEqual(library.preset('kaden'),authoredPreset);
  assert.deepEqual(library.switchTo('local-character'),previous);assert.equal(library.list().length,3);
});

test('characters with the same name have unique IDs and the newly active character survives reload',()=>{
  const saved=storage(),library=new CharacterLibrary({storage:saved,current:current(),presets:[kaden()]});
  const first=library.create({name:'Robin'}),second=library.create({name:'Robin'});
  assert.notEqual(first.id,second.id);assert.equal(library.list().length,4);
  const restored=new CharacterLibrary({storage:saved,current:current(),presets:[kaden()]});
  assert.equal(restored.activeId,second.id);assert.deepEqual(restored.active(),second);
  assert.deepEqual(restored.switchTo(first.id),first);assert.equal(restored.switchTo('local-character').character.name,'Karim');
});

test('creating a later character cannot inherit edited voice, biography, director, story or archive fields',()=>{
  const saved=storage(),library=new CharacterLibrary({storage:saved,current:current()});
  const first=library.create({name:'First',archiveEnabled:true});
  first.character.about='Authored first biography.';first.character.environment='A specific room.';first.character.speaker='Ethan';
  first.voice={preset:'male',emotion:'angry',event:'sigh',direction:'Speak sharply.'};first.director={enabled:false,notes:'First direction.'};
  first.trigger=current().trigger;first.narrative=current().narrative;first.archive.entry_ids=['a'.repeat(32)];library.save(first);
  const second=library.create({name:'Second'});assert.equal(second.character.about,'');assert.equal(second.character.environment,'');assert.equal(second.character.speaker,'Chelsie');
  assert.deepEqual(second.voice,{preset:'automatic',emotion:'neutral',event:'none',direction:''});
  assert.deepEqual(second.director,{enabled:true,notes:''});assert.equal(second.trigger,null);assert.deepEqual(second.narrative.beats,[]);
  assert.deepEqual(second.archive,{enabled:false,entry_ids:[]});
  const restored=new CharacterLibrary({storage:saved,current:current()});assert.deepEqual(restored.switchTo(first.id),first);
  second.character.name='Mutated outside';second.archive.entry_ids.push('b'.repeat(32));
  assert.equal(library.active().character.name,'Second');assert.deepEqual(library.active().archive.entry_ids,[]);
});

test('archive-enabled creation explicitly consults the library without inheriting selected IDs or source text',()=>{
  const initial={...current(),archive:{enabled:true,scope:'library',entry_ids:['a'.repeat(32)],text:'Do not inherit source material.'}};
  const library=new CharacterLibrary({storage:storage(),current:initial});
  const created=library.create({name:'Map visitor',archiveEnabled:true});
  assert.deepEqual(created.archive,{enabled:true,scope:'library',entry_ids:[]});
  assert.equal(JSON.stringify(created).includes('Do not inherit'),false);
  assert.deepEqual(library.create({name:'Ordinary character',archiveEnabled:'true'}).archive,{enabled:false,entry_ids:[]});
});

test('invalid names or roles do not mutate the list, active ID, settings or saved library',()=>{
  const saved=storage(),library=new CharacterLibrary({storage:saved,current:current(),presets:[kaden()]});
  const before=library.active(),items=library.list(),serialized=saved.getItem(CHARACTER_LIBRARY_KEY);
  for(const name of [undefined,null,0,[],{},'','   ','x'.repeat(81)]){
    assert.throws(()=>library.create({name}),/character name/);
    assert.deepEqual(library.active(),before);assert.deepEqual(library.list(),items);assert.equal(saved.getItem(CHARACTER_LIBRARY_KEY),serialized);
  }
  for(const role of [null,7,{},'x'.repeat(121)])assert.throws(()=>library.create({name:'Valid name',role}),/character role/);
  assert.throws(()=>library.create(),/character name/);assert.deepEqual(library.active(),before);assert.equal(saved.getItem(CHARACTER_LIBRARY_KEY),serialized);
  const boundary=library.create({name:'x'.repeat(80),role:'r'.repeat(120)});
  assert.equal(boundary.character.name.length,80);assert.equal(boundary.character.role.length,120);
});

test('UUID collisions never overwrite an existing character and exhausted generation leaves the library intact',t=>{
  const firstId='11111111-1111-4111-8111-111111111111',secondId='22222222-2222-4222-8222-222222222222';
  const values=[firstId,firstId,secondId];const generator=t.mock.method(globalThis.crypto,'randomUUID',()=>values.shift()??firstId);
  const saved=storage(),library=new CharacterLibrary({storage:saved,current:current()});
  const first=library.create({name:'First'}),second=library.create({name:'Second'});
  assert.notEqual(first.id,second.id);assert.equal(generator.mock.callCount(),3);
  const snapshot=saved.getItem(CHARACTER_LIBRARY_KEY),before=library.active();
  assert.throws(()=>library.create({name:'Never overwrite first'}),/could not create/);
  assert.deepEqual(library.active(),before);assert.equal(saved.getItem(CHARACTER_LIBRARY_KEY),snapshot);
  assert.deepEqual(library.switchTo(first.id),first);assert.equal(library.list().length,3);
});

test('unavailable or malformed secure IDs fail before mutation and do not expose internal failures',t=>{
  const saved=storage(),library=new CharacterLibrary({storage:saved,current:current()});
  const before=library.active(),serialized=saved.getItem(CHARACTER_LIBRARY_KEY);
  const generator=t.mock.method(globalThis.crypto,'randomUUID',()=>{throw new Error('PRIVATE_BROWSER_DETAIL');});
  assert.throws(()=>library.create({name:'Valid'}),error=>/could not create/.test(error.message)&&!error.message.includes('PRIVATE'));
  generator.mock.mockImplementation(()=> '../unsafe');assert.throws(()=>library.create({name:'Valid'}),/could not create/);
  assert.deepEqual(library.active(),before);assert.equal(saved.getItem(CHARACTER_LIBRARY_KEY),serialized);
});

test('creation stays usable in memory during quota failure while preserving the surviving legacy character',()=>{
  const saved=storage(),library=new CharacterLibrary({storage:saved,current:current(),presets:[kaden()]});
  library.writeLegacy();const legacy=saved.getItem('live-character-v1'),originalLibrary=saved.getItem(CHARACTER_LIBRARY_KEY);
  const setItem=saved.setItem;saved.setItem=(key,value)=>{if(key===CHARACTER_LIBRARY_KEY)throw new Error('Quota.');setItem(key,value);};
  const created=library.create({name:'In-memory draft'});
  assert.equal(library.storageError,true);assert.equal(library.activeId,created.id);assert.equal(library.writeLegacy(),false);
  assert.equal(saved.getItem('live-character-v1'),legacy);assert.equal(saved.getItem(CHARACTER_LIBRARY_KEY),originalLibrary);
  assert.equal(library.switchTo('local-character').character.about,current().character.about);
  assert.deepEqual(library.switchTo(created.id),created);
});

function anna(){return {...current(),character:{...current().character,name:'Anna',about:'An existing saved Anna definition.'}};}
function snapshot(saved){return JSON.parse(saved.getItem(CHARACTER_LIBRARY_KEY));}

test('a valid existing library recovers a surviving legacy Karim before active legacy writes can replace it',()=>{
  const saved=storage(),original=anna();new CharacterLibrary({storage:saved,current:original,presets:[kaden()]});
  const before=snapshot(saved),facts={...current().character,about:'Original Karim biography.\nKeep this exact second line.'};
  saved.setItem('live-character-v1',JSON.stringify({...facts,reference_audio:'PRIVATE_LEGACY_AUDIO',reference_text:'PRIVATE_REFERENCE_WORDS'}));
  saved.setItem('live-delivery-v1',JSON.stringify({emotion:'angry',direction:'An unrelated active voice.'}));
  saved.setItem('live-trigger-v1',JSON.stringify(current().trigger));saved.setItem('live-story-v1',JSON.stringify(current().narrative));
  const library=new CharacterLibrary({storage:saved,current:original,presets:[kaden()],recoveryProfiles:legacyRecoveryProfiles(saved)});
  assert.equal(library.activeId,before.activeId);assert.deepEqual(library.active(),before.profiles[0]);
  for(const prior of before.profiles)assert.deepEqual(snapshot(saved).profiles.find(value=>value.id===prior.id),prior);
  const recovered=snapshot(saved).profiles.find(value=>value.id==='recovered-live-character');
  assert.deepEqual(recovered.character,facts);assert.deepEqual(recovered.voice,{preset:'automatic',emotion:'neutral',event:'none',direction:''});
  assert.deepEqual(recovered.director,{enabled:true,notes:''});assert.equal(recovered.trigger,null);
  assert.deepEqual(recovered.narrative,{enabled:false,mode:'manual',beats:[]});assert.deepEqual(recovered.archive,{enabled:false,entry_ids:[]});
  assert.doesNotMatch(saved.getItem(CHARACTER_LIBRARY_KEY),/PRIVATE_|An unrelated active voice/);
  library.writeLegacy();assert.equal(JSON.parse(saved.getItem('live-character-v1')).name,'Anna');
  assert.deepEqual(snapshot(saved).profiles.find(value=>value.id===recovered.id).character,facts);
});

test('both actual raw keys are parsed independently so a corrupt live record cannot hide a valid Qwen character',()=>{
  const saved=storage();new CharacterLibrary({storage:saved,current:anna()});
  saved.setItem('live-character-v1','malformed json');saved.setItem('qwen-character-v1',JSON.stringify(current().character));
  const candidates=legacyRecoveryProfiles(saved);assert.equal(candidates.length,1);assert.equal(candidates[0].id,'recovered-qwen-character');
  const library=new CharacterLibrary({storage:saved,current:anna(),recoveryProfiles:candidates});
  assert.equal(library.active().character.name,'Anna');assert.equal(library.list().length,2);
  assert.deepEqual(library.switchTo('recovered-qwen-character').character,current().character);
});

test('recovery deduplicates normalized character whitespace while preserving original saved text and stable IDs across reloads',()=>{
  const saved=storage();new CharacterLibrary({storage:saved,current:anna(),presets:[kaden()]});
  const original={...current().character,about:'Original  Karim\nbiography.',role:'Singer  and songwriter'};
  const duplicate={...original,name:'  Karim ',about:'Original Karim biography.',role:'Singer\tand songwriter'};
  saved.setItem('live-character-v1',JSON.stringify(original));saved.setItem('qwen-character-v1',JSON.stringify(duplicate));
  for(let attempt=0;attempt<3;attempt++){
    const library=new CharacterLibrary({storage:saved,current:anna(),presets:[kaden()],recoveryProfiles:legacyRecoveryProfiles(saved)});
    assert.equal(library.activeId,'local-character');assert.equal(library.list().length,3);
    assert.deepEqual(snapshot(saved).profiles.find(value=>value.id==='recovered-live-character').character,original);
    library.writeLegacy();
  }
});

test('stable recovery IDs resolve collisions without overwriting earlier profiles and remain idempotent',()=>{
  const saved=storage(),main={id:'original-active',...anna()},collision={id:'recovered-live-character',...current(),character:{...current().character,name:'Morgan'}},
    second={...collision,id:'recovered-live-character-2',character:{...collision.character,name:'Eli'}};
  saved.setItem(CHARACTER_LIBRARY_KEY,JSON.stringify({version:1,activeId:main.id,profiles:[main,collision,second]}));
  saved.setItem('live-character-v1',JSON.stringify(current().character));
  for(let attempt=0;attempt<2;attempt++){
    const library=new CharacterLibrary({storage:saved,current:anna(),recoveryProfiles:legacyRecoveryProfiles(saved)});
    assert.equal(library.activeId,main.id);assert.equal(library.list().length,4);
    const profiles=snapshot(saved).profiles;
    assert.deepEqual(profiles.find(value=>value.id===collision.id),collision);assert.deepEqual(profiles.find(value=>value.id===second.id),second);
    assert.deepEqual(profiles.find(value=>value.id==='recovered-live-character-3').character,current().character);
  }
});

test('missing, corrupt, unsupported or empty legacy records cannot recover a form fallback or fabricate a biography',()=>{
  for(const raw of [null,'bad json','null','[]','{}',JSON.stringify({name:'Karim'}),JSON.stringify({name:' ',role:'Singer'}),
    JSON.stringify({name:'Karim',role:'   '}),JSON.stringify({name:'Karim',about:{private:'No string coercion'}}),
    JSON.stringify({name:'Karim',role:'Singer',speaker:'Unsupported'}),JSON.stringify({name:'Karim',about:'x'.repeat(2401)}),JSON.stringify(current())]){
    const saved=storage();new CharacterLibrary({storage:saved,current:anna()});if(raw!==null)saved.setItem('live-character-v1',raw);
    assert.deepEqual(legacyRecoveryProfiles(saved),[]);
    const library=new CharacterLibrary({storage:saved,current:current(),recoveryProfiles:legacyRecoveryProfiles(saved)});
    assert.equal(library.list().length,1);assert.equal(library.active().character.name,'Anna');
  }
});

test('existing matching character facts suppress recovery even when legacy auxiliary settings differ',()=>{
  const saved=storage(),authored={...current(),character:{...current().character,about:'Existing\n biography.'}};
  new CharacterLibrary({storage:saved,current:authored});saved.setItem('qwen-character-v1',JSON.stringify({...authored.character,about:'Existing biography.'}));
  const library=new CharacterLibrary({storage:saved,current:anna(),recoveryProfiles:legacyRecoveryProfiles(saved)});
  assert.deepEqual(library.active(),{id:'local-character',...authored});assert.equal(library.list().length,1);
  assert.equal(library.active().voice.preset,'custom');assert.equal(library.active().narrative.enabled,true);
});

test('archive and custom libraries reject recovery candidates without writing main or legacy records',()=>{
  const saved=storage();new CharacterLibrary({storage:saved,current:anna()});saved.setItem('live-character-v1',JSON.stringify(current().character));
  const main=saved.getItem(CHARACTER_LIBRARY_KEY),legacy=saved.getItem('live-character-v1'),candidates=legacyRecoveryProfiles(saved);
  for(const key of ['live-archive-character-library-v1','another-library']){
    const library=new CharacterLibrary({storage:saved,current:anna(),key,legacyWrites:false,recoveryProfiles:candidates});
    assert.equal(library.list().length,1);assert.equal(library.active().character.name,'Anna');
    assert.equal(saved.getItem(CHARACTER_LIBRARY_KEY),main);assert.equal(saved.getItem('live-character-v1'),legacy);
  }
});

test('failed recovery persistence preserves the original legacy save and permits a later successful recovery',()=>{
  const saved=storage();new CharacterLibrary({storage:saved,current:anna()});saved.setItem('live-character-v1',JSON.stringify(current().character));
  const legacy=saved.getItem('live-character-v1'),before=saved.getItem(CHARACTER_LIBRARY_KEY),setItem=saved.setItem;
  saved.setItem=(key,value)=>{if(key===CHARACTER_LIBRARY_KEY)throw new Error('Quota.');setItem(key,value);};
  const library=new CharacterLibrary({storage:saved,current:anna(),recoveryProfiles:legacyRecoveryProfiles(saved)});
  assert.equal(library.storageError,true);assert.equal(library.list().length,2);assert.equal(library.writeLegacy(),false);
  assert.equal(saved.getItem('live-character-v1'),legacy);assert.equal(saved.getItem(CHARACTER_LIBRARY_KEY),before);
  saved.setItem=setItem;
  const restored=new CharacterLibrary({storage:saved,current:anna(),recoveryProfiles:legacyRecoveryProfiles(saved)});
  assert.equal(restored.storageError,false);assert.equal(restored.active().character.name,'Anna');assert.equal(restored.list().length,2);
  assert.deepEqual(restored.switchTo('recovered-live-character').character,current().character);
});
