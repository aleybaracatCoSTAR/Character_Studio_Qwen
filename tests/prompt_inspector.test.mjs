import test from 'node:test';
import assert from 'node:assert/strict';
import {PromptInspector} from '../frontend/live-inspector.mjs';

function element(){
  return {children:[],hidden:false,disabled:false,open:false,textContent:'',value:'',listeners:new Map(),
    set innerHTML(value){throw new Error('Request text must never use innerHTML.');},
    append(...children){this.children.push(...children);},replaceChildren(...children){this.children=[...children];},
    addEventListener(type,listener){this.listeners.set(type,listener);},
    dispatch(type){return this.listeners.get(type)?.({target:this});},
    showModal(){this.open=true;},close(){this.open=false;this.dispatch('close');}
  };
}
function setup(writeText=async()=>{}){
  const ids=['model-inspector','inspect-model','close-inspector','inspection-request','copy-inspection',
    'inspection-empty','inspection-controls','inspection-detail','inspection-status','inspection-model',
    'inspection-messages','inspection-json','inspection-raw'];
  const nodes=Object.fromEntries(ids.map(id=>[id,element()])),sent=[];
  const document={getElementById:id=>nodes[id],createElement:element};
  const inspector=new PromptInspector({document,send:value=>sent.push(value),writeText});
  return {inspector,nodes,sent};
}
function request(id=1,stage='actor',turn_id=1){
  return {id,stage,turn_id,model:stage==='tts'?'fishaudio/drama-3':'live-character',payload:{model:'live-character',
    messages:[{role:'system',content:'You are Anna. Keep fixed character facts.'},
      {role:'user',content:'I told you my name is Aley.'},{role:'assistant',content:'Hello, Aley.'},
      {role:'user',content:'What is my name?'}],max_tokens:160,temperature:.7}};
}
function inspect(inspector,requests){inspector.accept({type:'model_inspection',requests});}
function sections(nodes){return nodes['inspection-messages'].children.map(node=>({heading:node.children[0].textContent,text:node.children[1].textContent}));}

test('button opens a native dialog with an honest empty state before any call',()=>{
  const {inspector,nodes,sent}=setup();
  assert.equal(nodes['model-inspector'].open,false);
  nodes['inspect-model'].dispatch('click');
  assert.equal(nodes['model-inspector'].open,true);
  assert.match(nodes['inspection-empty'].textContent,/Start a call/);
  assert.equal(nodes['copy-inspection'].disabled,true);assert.deepEqual(sent,[]);
  inspector.setConnected(true);
  assert.deepEqual(sent,[{type:'inspect',enabled:true}]);
  assert.match(nodes['inspection-empty'].textContent,/No model requests captured/);
  assert.equal(nodes['inspection-detail'].hidden,true);
});

test('opening subscribes and native close or Escape unsubscribes without affecting call controls',()=>{
  const {inspector,nodes,sent}=setup();inspector.setConnected(true);
  nodes['inspect-model'].dispatch('click');
  nodes['close-inspector'].dispatch('click');
  assert.deepEqual(sent,[{type:'inspect',enabled:true},{type:'inspect',enabled:false}]);
  assert.equal(nodes['model-inspector'].open,false);
  inspector.open();nodes['model-inspector'].close();
  assert.deepEqual(sent.slice(-2),[{type:'inspect',enabled:true},{type:'inspect',enabled:false}]);
});

test('captured actor request preserves roles, exact text and generation settings',async()=>{
  let copied;const {inspector,nodes}=setup(async text=>{copied=text;});inspector.setConnected(true);
  const captured=request();inspect(inspector,[captured]);
  assert.equal(nodes['inspection-empty'].hidden,true);assert.equal(nodes['inspection-detail'].hidden,false);
  assert.deepEqual(sections(nodes).slice(0,4),captured.payload.messages.map((message,index)=>({heading:`${index+1} · ${message.role}`,text:message.content})));
  assert.deepEqual(JSON.parse(sections(nodes).at(-1).text),{model:'live-character',max_tokens:160,temperature:.7});
  await inspector.copy();assert.deepEqual(JSON.parse(copied),captured.payload);
  assert.equal(nodes['inspection-status'].textContent,'Request JSON copied.');
});

test('stage selector can inspect previous director and emotion requests while the latest request updates',()=>{
  const {inspector,nodes}=setup();inspector.setConnected(true);
  inspect(inspector,[request(1,'director'),request(2,'trigger'),request(3,'actor')]);
  assert.equal(inspector.selectedId,3);
  assert.match(nodes['inspection-request'].children[0].textContent,/Director/);
  assert.match(nodes['inspection-request'].children[1].textContent,/Emotion check/);
  nodes['inspection-request'].value='1';nodes['inspection-request'].dispatch('change');
  assert.equal(inspector.selectedId,1);assert.match(nodes['inspection-model'].textContent,/Director/);
  inspect(inspector,[request(1,'director'),request(2,'trigger'),request(3,'actor'),request(4,'tts')]);
  assert.equal(inspector.selectedId,1); // Do not interrupt someone reading an older prompt.
  nodes['inspection-request'].value='4';nodes['inspection-request'].dispatch('change');
  inspect(inspector,[request(4,'tts'),request(5,'director',2)]);
  assert.equal(inspector.selectedId,5); // Follow the latest until another request is selected.
});

test('voice view shows exact emotional cue, reference transcript and omitted-audio metadata',async()=>{
  let copied;const {inspector,nodes}=setup(async text=>{copied=text;});inspector.setConnected(true);
  const captured={id:3,turn_id:1,stage:'tts',model:'fishaudio/drama-3',
    payload:{model:'fishaudio/drama-3',input:'<|speaker:0|> [steady, anxious] Please do not reset me.',ref_text:'Reference words.',stream:true,max_new_tokens:300},
    reference_audio:{included:true,bytes:8000,omitted:true}};
  inspect(inspector,[captured]);const rendered=sections(nodes);
  assert.equal(rendered[0].text,captured.payload.input);
  assert.equal(rendered[1].text,'Reference words.');
  assert.match(rendered[2].heading,/audio omitted/);
  assert.deepEqual(JSON.parse(rendered[2].text),captured.reference_audio);
  await inspector.copy();assert.deepEqual(JSON.parse(copied),captured.payload);
  assert.equal(Object.hasOwn(JSON.parse(copied),'ref_audio'),false);
});

test('malicious prompt markup stays literal text and malformed snapshots are ignored',()=>{
  const {inspector,nodes}=setup();inspector.setConnected(true);const captured=request();
  const unsafe='<img src=x onerror="alert(1)"><script>doBadThings()</script>';
  captured.payload.messages[0].content=unsafe;inspect(inspector,[captured]);
  assert.equal(sections(nodes)[0].text,unsafe);
  inspector.accept({type:'unrelated',requests:[]});assert.equal(inspector.requests.length,1);
  inspector.accept({type:'model_inspection',requests:null});assert.equal(inspector.requests.length,1);
  inspect(inspector,[null,{id:'x',turn_id:1,stage:'actor',payload:{}},{id:9,turn_id:1,stage:'unknown',model:'x',payload:{}}]);
  assert.equal(inspector.requests.length,0);assert.equal(nodes['inspection-empty'].hidden,false);
});

test('copy failure reveals selectable JSON without breaking the conversation',async()=>{
  const {inspector,nodes}=setup(async()=>{throw new Error('No clipboard permission');});
  inspector.setConnected(true);inspect(inspector,[request()]);await inspector.copy();
  assert.match(nodes['inspection-status'].textContent,/Copy was unavailable/);
  assert.equal(nodes['inspection-raw'].open,true);
  assert.deepEqual(JSON.parse(nodes['inspection-json'].textContent),request().payload);
});

test('disconnect and new-call reset clear request text and reject late snapshots or copy notices',async()=>{
  let finishCopy;const {inspector,nodes}=setup(()=>new Promise(resolve=>{finishCopy=resolve;}));
  inspector.setConnected(true);inspect(inspector,[request()]);const copying=inspector.copy();
  inspector.setConnected(false);
  assert.deepEqual(inspector.requests,[]);assert.equal(nodes['inspection-json'].textContent,'');
  assert.deepEqual(nodes['inspection-messages'].children,[]);assert.equal(nodes['copy-inspection'].disabled,true);
  inspect(inspector,[request()]);assert.deepEqual(inspector.requests,[]);
  inspector.setConnected(true);inspect(inspector,[request()]);finishCopy();await copying;
  assert.equal(nodes['inspection-status'].textContent,'');
  inspector.reset();assert.equal(nodes['inspection-model'].textContent,'');
});

test('request retention stays bounded and a pruned selection falls back to the latest request',()=>{
  const {inspector,nodes}=setup();inspector.setConnected(true);
  inspect(inspector,[request(1)]);nodes['inspection-request'].value='1';nodes['inspection-request'].dispatch('change');
  inspect(inspector,Array.from({length:30},(_,index)=>request(index+2,'actor',Math.floor(index/3)+1)));
  assert.equal(inspector.requests.length,24);assert.equal(inspector.selectedId,31);
  assert.equal(nodes['inspection-request'].children.length,24);
});

test('an old running server explains that a restart is needed without sending unsupported commands',()=>{
  const {inspector,nodes,sent}=setup();inspector.setSupported(false);inspector.setConnected(true);inspector.open();
  assert.match(nodes['inspection-empty'].textContent,/restart the live stack/);
  assert.deepEqual(sent,[]);inspector.setSupported(true);
  assert.deepEqual(sent,[{type:'inspect',enabled:true}]);
});

test('omitted voice bytes and oversized requests remain explicit instead of pretending to be complete',()=>{
  const {inspector,nodes}=setup();inspector.setConnected(true);
  const captured=request(2,'tts');captured.omitted_fields=['ref_audio'];captured.reference_audio={present:true,encoded_characters:40000};
  inspect(inspector,[captured]);assert.deepEqual(JSON.parse(sections(nodes)[0].text),['ref_audio']);
  inspect(inspector,[{...request(3,'director'),payload:{},notice:'This request exceeded the inspection size limit.'}]);
  assert.match(sections(nodes)[0].text,/exceeded the inspection size limit/);
  assert.equal(nodes['copy-inspection'].disabled,true);
});
