import test from 'node:test';
import assert from 'node:assert/strict';
import {requestText,recentJSON,requestFilename,downloadFile} from '../frontend/live-inspection-export.mjs';

function actorRequest(){
  return {id:7,turn_id:3,phase:'reply',stage:'actor',model:'live-character',payload:{model:'live-character',
    messages:[
      {role:'system',content:'You are Anaïs, a thoughtful character in القاهرة.\nPreserve what was actually heard.'},
      {role:'user',content:'My favorite object is a blue teacup — remember it.'},
      {role:'assistant',content:'A blue teacup. I will keep that in mind.'},
      {role:'user',content:'What object did I tell you about?'}
    ],stream:true,max_tokens:160,temperature:.7,chat_template_kwargs:{enable_thinking:false}}};
}

test('plain text export preserves actual ordered system, user and assistant messages',()=>{
  const request=actorRequest(),text=requestText(request);
  assert.equal(typeof text,'string');
  assert.ok(text.includes(request.model));
  const positions=request.payload.messages.map(message=>{
    assert.ok(text.includes(message.content),`Missing exact ${message.role} content`);
    return text.indexOf(message.content);
  });
  assert.deepEqual(positions,[...positions].sort((a,b)=>a-b));
  assert.match(text,/system/i);assert.match(text,/user/i);assert.match(text,/assistant/i);
  assert.match(text,/max_tokens/);assert.match(text,/160/);assert.match(text,/enable_thinking/);
  assert.ok(text.includes('Anaïs'));assert.ok(text.includes('القاهرة'));assert.ok(text.includes('—'));
});

test('plain text export retains the full emotional cue, reference transcript and omission metadata',()=>{
  const request={id:8,turn_id:3,stage:'tts',phase:'voice_retry',model:'fishaudio/drama-3',
    payload:{model:'fishaudio/drama-3',input:'<|speaker:0|> [anxious, steady] لا تضغط على الزر. I need a moment…',
      ref_text:'These are the exact reference words — merci.',stream:true,stream_format:'sse',response_format:'pcm',max_new_tokens:420},
    omitted_fields:['ref_audio'],reference_audio:{present:true,encoded_characters:8021}};
  const text=requestText(request);
  assert.ok(text.includes(request.payload.input));
  assert.ok(text.includes(request.payload.ref_text));
  assert.ok(text.includes('ref_audio'));assert.ok(text.includes('8021'));
  assert.ok(text.includes('voice_retry'));
  assert.match(text,/max_new_tokens/);assert.match(text,/420/);
});

test('long request contents are complete and oversized omission notices stay explicit',()=>{
  const request=actorRequest();
  request.payload.messages[0].content='Beginning of the real prompt.\n'+'long context — '.repeat(5000)+'\nFINAL PROMPT WORDS.';
  const text=requestText(request);
  assert.ok(text.includes(request.payload.messages[0].content));
  assert.ok(text.includes('FINAL PROMPT WORDS.'));
  const omitted={id:9,turn_id:4,stage:'director',model:'live-director',payload:{},
    notice:'This request exceeds the inspector size limit; its input was not retained.'};
  assert.ok(requestText(omitted).includes(omitted.notice));
});

test('recent JSON exports exact request payloads and explicit bounded-snapshot metadata',()=>{
  const requests=[actorRequest(),{id:8,turn_id:3,stage:'tts',model:'fishaudio/drama-3',payload:{input:'[calm] Bonjour.'},
    omitted_fields:['ref_audio'],reference_audio:{present:true,encoded_characters:4000}}];
  const before=Date.now(),text=recentJSON(requests),after=Date.now(),exported=JSON.parse(text);
  assert.equal(exported.export_format,'character-studio-model-inputs');
  assert.equal(exported.version,1);assert.equal(exported.scope,'recent_requests');
  assert.deepEqual(exported.retention,{max_requests:24,max_turns:5,max_characters:256000});
  assert.deepEqual(exported.requests,requests);
  const instant=Date.parse(exported.exported_at);
  assert.ok(Number.isFinite(instant));assert.ok(instant>=before&&instant<=after);
  assert.ok(text.includes('القاهرة'));
});

test('exported JSON is a point-in-time copy unaffected by later requests or mutation',()=>{
  const requests=[actorRequest()],expected=structuredClone(requests),text=recentJSON(requests);
  requests[0].payload.messages[0].content='Changed after downloading.';
  requests.push({id:10,turn_id:4,stage:'actor',model:'later-model',payload:{messages:[]}});
  assert.deepEqual(JSON.parse(text).requests,expected);
});

test('empty recent export remains a valid honest snapshot with no fabricated prompts',()=>{
  const exported=JSON.parse(recentJSON([]));
  assert.deepEqual(exported.requests,[]);
  assert.equal(exported.scope,'recent_requests');
});

test('filenames retain safe stage and numeric identifiers without model or path injection',()=>{
  const request=actorRequest();
  const textName=requestFilename(request),jsonName=requestFilename(request,'json');
  assert.match(textName,/^[a-z0-9_-]+\.txt$/i);
  assert.match(jsonName,/^[a-z0-9_-]+\.json$/i);
  assert.match(textName,/actor/);assert.match(textName,/3/);assert.match(textName,/7/);
  for(const stage of ['actor','director','trigger','tts']){
    assert.match(requestFilename({...request,stage}),new RegExp(stage));
  }
  const unsafe={...request,stage:'../../<script>evil</script>',model:'../../private/path',turn_id:'../4',id:'x/../9'};
  for(const extension of ['../../html','svg','txt','json','TXT']){
    const name=requestFilename(unsafe,extension);
    assert.match(name,/^[a-z0-9_-]+\.(?:txt|json)$/i);
    assert.doesNotMatch(name,/[\\/<>]|\.\./);
    assert.doesNotMatch(name,/private|script|evil/i);
  }
});

function downloadSetup(failure=null){
  const calls={blobs:[],created:[],revoked:[],scheduled:[],appended:[],removed:0,clicks:0};
  let anchor;
  const document={body:{append(node){
    if(failure==='append')throw new Error('Cannot append temporary link');
    calls.appended.push(node);
  }},createElement(tag){
    assert.equal(tag,'a');
    if(failure==='create')throw new Error('Cannot create temporary link');
    anchor={style:{},download:'',href:'',click(){calls.clicks++;if(failure==='click')throw new Error('Download click blocked');},
      remove(){calls.removed++;if(failure==='remove')throw new Error('Temporary-link removal failed');}};
    return anchor;
  }};
  document.body.appendChild=node=>document.body.append(node);
  class FakeBlob{constructor(parts,options){this.parts=parts;this.type=options.type;calls.blobs.push(this);}}
  const URL={createObjectURL(blob){calls.created.push(blob);return 'blob:local-export';},
    revokeObjectURL(value){calls.revoked.push(value);}};
  const schedule=(callback,ms)=>calls.scheduled.push({callback,ms});
  return {document,calls,dependencies:{URL,Blob:FakeBlob,schedule},anchor:()=>anchor};
}

test('download creates a UTF-8 local Blob, clicks and removes its temporary link, then revokes later',()=>{
  const fixture=downloadSetup(),file={filename:'model-actor-turn-3-request-7.txt',content:'Anaïs — القاهرة\nComplete prompt.',mime:'text/plain'};
  downloadFile(fixture.document,file,fixture.dependencies);
  const {calls}=fixture,anchor=fixture.anchor();
  assert.equal(calls.blobs.length,1);assert.deepEqual(calls.blobs[0].parts,[file.content]);
  assert.match(calls.blobs[0].type,/^text\/plain\s*;\s*charset=utf-8$/i);
  assert.equal(calls.created[0],calls.blobs[0]);
  assert.equal(anchor.href,'blob:local-export');assert.equal(anchor.download,file.filename);
  assert.equal(calls.clicks,1);assert.equal(calls.removed,1);assert.deepEqual(calls.appended,[anchor]);
  assert.deepEqual(calls.revoked,[]);assert.equal(calls.scheduled.length,1);assert.equal(calls.scheduled[0].ms,1000);
  calls.scheduled[0].callback();assert.deepEqual(calls.revoked,['blob:local-export']);
});

test('already explicit UTF-8 MIME is not duplicated',()=>{
  const fixture=downloadSetup();
  downloadFile(fixture.document,{filename:'prompts.json',content:'{"text":"القاهرة"}',mime:'application/json;charset=utf-8'},fixture.dependencies);
  assert.equal((fixture.calls.blobs[0].type.match(/charset/gi)||[]).length,1);
  assert.match(fixture.calls.blobs[0].type,/charset=utf-8/i);
});

for(const failure of ['click','append','create','remove']){
  test(`local object URL is cleaned up when temporary-link ${failure} fails`,()=>{
    const fixture=downloadSetup(failure);
    assert.throws(()=>downloadFile(fixture.document,{filename:'prompts.txt',content:'Actual prompt.',mime:'text/plain'},fixture.dependencies));
    assert.equal(fixture.calls.created.length,1);
    if(failure!=='create')assert.equal(fixture.calls.removed,1);
    assert.equal(fixture.calls.scheduled.length,1);assert.equal(fixture.calls.scheduled[0].ms,1000);
    fixture.calls.scheduled[0].callback();assert.deepEqual(fixture.calls.revoked,['blob:local-export']);
  });
}

test('a failed cleanup scheduler releases the local object URL immediately',()=>{
  const fixture=downloadSetup();
  fixture.dependencies.schedule=()=>{throw new Error('Scheduling unavailable');};
  assert.throws(()=>downloadFile(fixture.document,{filename:'prompts.txt',content:'Actual prompt.',mime:'text/plain'},fixture.dependencies));
  assert.equal(fixture.calls.clicks,1);assert.equal(fixture.calls.removed,1);
  assert.deepEqual(fixture.calls.revoked,['blob:local-export']);
});
