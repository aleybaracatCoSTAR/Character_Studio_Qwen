import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {EngineComparison,comparisonMetrics,comparisonProfile,comparableReplies} from '../frontend/live-comparison.mjs';

class Element {
  constructor(tag='div'){this.tagName=tag;this.children=[];this.listeners={};this.value='';this.hidden=false;this.disabled=false;this.open=false;this.textContent='';}
  set innerHTML(_){throw new Error('Model content must stay literal text.');}
  addEventListener(type,callback){this.listeners[type]=callback;}
  emit(type){return this.listeners[type]?.({target:this,preventDefault(){}});}
  replaceChildren(...children){this.children=[];this.append(...children);}
  append(...children){this.children.push(...children);}
  showModal(){this.open=true;}
  close(){this.open=false;}
}
function setup(options={}){
  const nodes=new Map(),files=[],changes=[];let now=100;
  const document={getElementById(id){if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);},createElement:tag=>new Element(tag)};
  const comparison=new EngineComparison({document,clock:()=>now,saveFile:file=>files.push(file),onChange:value=>changes.push(value),...options});
  return {comparison,nodes,files,changes,document,tick(value){now+=value;}};
}
function profile(name='Robin'){return {character:{name,role:'Art guide',about:'A curious guide.',goal:'Explore the catalogue.',speaker:'Chelsie'},
  voice:{preset:'automatic',emotion:'calm',event:'none',direction:'Warm and measured.'},director:{enabled:true,notes:'Stay grounded.'},trigger:null,
  narrative:{enabled:false,mode:'manual',beats:[]},archive:{enabled:true,scope:'library',entry_ids:[],collection_ids:['national-gallery-of-art']}};}
function capabilities(default_engine='baseline',baseline=true,omni=true){return {default_engine,engines:{baseline:{available:true,ready:baseline},'qwen-omni':{available:omni,ready:omni}}};}

test('Drama streaming playback is visible separately from native Omni',()=>{
  const {comparison,nodes}=setup();const value=capabilities();
  value.engines.baseline.voice_playback='streaming';comparison.setCapabilities(value);
  assert.match(nodes.get('comparison-engine').children[0].textContent,/Drama 3.*live playback/);
  assert.doesNotMatch(nodes.get('comparison-engine').children[1].textContent,/live playback/);
  assert.equal(comparison.engine(),'baseline');
});
function start(comparison,{engine='baseline',id='robin',input='text',settings=profile(),turn=1,prompt='Tell me about the painting.'}={}){
  comparison.setCapabilities(capabilities(engine));comparison.selected=engine;
  assert.equal(comparison.beginCall({id,profile:settings,input}),true);
  comparison.accept({type:'turn_start',turn_id:turn,engine});
  comparison.accept({type:'transcript',turn_id:turn,text:prompt});
}
function finish(comparison,turn=1,text='The catalogue names the artist.'){
  comparison.accept({type:'text_delta',turn_id:turn,text:'Provisional '});
  comparison.accept({type:'text_done',turn_id:turn,text});
  comparison.accept({type:'response_done',turn_id:turn});
  assert.equal(comparison.playbackComplete(turn),true);
}
const content=node=>[node.textContent,...node.children.map(content)].join(' ');

test('default comparison download uses the bounded server attachment route',async()=>{
  const {comparison,document}=setup({saveFile:undefined});const requests=[],anchors=[];
  document.body={append:node=>anchors.push(node)};
  document.createElement=tag=>{const node=new Element(tag);node.click=()=>{node.clicked=true;};node.remove=()=>{};return node;};
  const original=globalThis.fetch;
  globalThis.fetch=async(url,options)=>{requests.push({url,options});return {ok:true,json:async()=>({download_url:'/api/inspection/downloads/'+'a'.repeat(48)})};};
  try{
    start(comparison);finish(comparison);
    assert.equal(await comparison.download(),true);
    assert.equal(requests[0].url,'/api/inspection/downloads');
    const file=JSON.parse(requests[0].options.body);
    assert.equal(file.filename,'character-studio-engine-comparison.json');
    assert.match(JSON.parse(file.content).note,/expires after two minutes/);
    assert.equal(anchors[0].href,'/api/inspection/downloads/'+'a'.repeat(48));
    assert.equal(anchors[0].clicked,true);
  }finally{globalThis.fetch=original;}
});

test('older servers keep the baseline workflow and receive no engine field',()=>{
  const {comparison,nodes}=setup();comparison.setCapabilities(undefined);
  assert.deepEqual(comparison.startFields(),{});assert.equal(comparison.engine(),'baseline');assert.equal(comparison.isReady(),null);
  assert.equal(nodes.get('engine-controls').hidden,true);assert.equal(comparison.startProblem({}), '');assert.equal(comparison.ready({}),true);
  comparison.setCapabilities(capabilities('qwen-omni'));assert.equal(comparison.engine(),'qwen-omni');
  comparison.setCapabilities(undefined);assert.equal(comparison.engine(),'baseline');assert.deepEqual(comparison.startFields(),{});
});

test('advertised default selects once, readiness stays explicit and switching is locked during a call',()=>{
  const {comparison,nodes,changes}=setup();comparison.setCapabilities(capabilities('qwen-omni'));
  assert.equal(comparison.engine(),'qwen-omni');assert.deepEqual(comparison.startFields(),{engine:'qwen-omni'});
  comparison.setCapabilities(capabilities('baseline',true,false));assert.equal(comparison.engine(),'qwen-omni');assert.equal(comparison.isReady(),false);
  assert.match(comparison.startProblem({}),/not configured/);
  comparison.setCapabilities({...capabilities(),engines:{baseline:{available:true,ready:false},'qwen-omni':{available:true,ready:false}}});
  assert.match(comparison.startProblem({}),/not ready/);
  comparison.setBusy(true);nodes.get('comparison-engine').value='baseline';nodes.get('comparison-engine').emit('change');
  assert.equal(comparison.engine(),'qwen-omni');assert.equal(nodes.get('comparison-engine').disabled,true);assert.deepEqual(changes,[]);
  comparison.setBusy(false);nodes.get('comparison-engine').value='baseline';nodes.get('comparison-engine').emit('change');
  assert.deepEqual(changes,['baseline']);assert.equal(comparison.engine(),'baseline');
});

test('native custom references are rejected without altering the character or saved voice',()=>{
  const {comparison}=setup();comparison.setCapabilities(capabilities('qwen-omni'));
  const saved=profile();saved.voice.preset='custom';const original=structuredClone(saved);
  assert.match(comparison.startProblem(saved.voice),/uploaded voice references are not supported/);
  for(const preset of ['female','male','automatic'])assert.equal(comparison.startProblem({preset}), '');
  assert.deepEqual(saved,original);comparison.setCapabilities(capabilities());comparison.selected='baseline';assert.equal(comparison.startProblem(saved.voice),'');
});

test('comparison snapshots contain authored settings, not reference audio, credentials or archive passages',()=>{
  const source=profile();source.voice.reference_audio='PRIVATE_AUDIO_BASE64';source.voice.reference_text='PRIVATE_REFERENCE';source.headers={Authorization:'PRIVATE_KEY'};
  source.character.audio='PRIVATE_CHARACTER_AUDIO';source.archive.text='PRIVATE_ARCHIVE_PASSAGE';source.endpoint='PRIVATE_ENDPOINT';
  const normalized=comparisonProfile(source,'robin');assert.equal(normalized.character.name,'Robin');
  assert.doesNotMatch(JSON.stringify(normalized),/PRIVATE_|reference_audio|headers|endpoint/);normalized.character.name='Changed';assert.equal(source.character.name,'Robin');
  assert.equal(comparisonProfile({...source,character:{...source.character,about:'x'.repeat(64001)}},'robin'),null);
});

test('current-turn canonical text, citations and bounded timings are captured without rebuilding the main chat',()=>{
  const {comparison,tick}=setup();start(comparison);
  assert.equal(comparison.accept({type:'text_delta',turn_id:99,text:'STALE'}),false);
  assert.equal(comparison.accept({type:'text_delta',turn_id:1,engine:'qwen-omni',text:'WRONG ENGINE'}),false);
  comparison.accept({type:'archive_sources',turn_id:1,matches:[{collection_id:'museum-aic',source_entry_id:'123'},{citation_id:'aic:123'},{citation_id:'aic:123'},{citation_id:'fourth'}]});
  comparison.accept({type:'director_note',turn_id:1,ok:true,intention:'Ask a question.',emotion:'calm',delivery:'Quiet.'});
  comparison.accept({type:'metrics',turn_id:1,first_text_ms:200,first_audio_ms:500,stt_ms:0,director_ms:100,archive_ms:30,complete_ms:900,voice_verification:true,raw_prompt:'PRIVATE_REQUEST'});
  tick(330);assert.equal(comparison.audioQueued(1),true);assert.equal(comparison.audioQueued(1),false);finish(comparison);
  const result=comparison.pair().baseline;assert.equal(result.reply,'The catalogue names the artist.');assert.deepEqual(result.citations,['museum-aic:123','aic:123']);
  assert.equal(result.browser_audio_queued_ms,330);assert.equal(result.voice_verification,true);assert.equal(result.status,'complete');
  assert.deepEqual(result.director,{intention:'Ask a question.',emotion:'calm',delivery:'Quiet.'});assert.doesNotMatch(JSON.stringify(result),/PRIVATE_REQUEST|STALE|WRONG ENGINE/);
});

test('server completion alone cannot claim heard playback; failures and interruption remain unmatched',()=>{
  const {comparison}=setup();start(comparison);comparison.accept({type:'text_done',turn_id:1,text:'Visible transcript.'});
  comparison.accept({type:'response_done',turn_id:1});assert.equal(comparison.pair().baseline.status,'audio_pending');assert.equal(comparison.pair().baseline.playback_complete,false);
  comparison.endCall();assert.equal(comparison.pair().baseline.status,'interrupted');
  start(comparison);comparison.accept({type:'error',turn_id:1,message:'PRIVATE_ERROR'});comparison.accept({type:'response_done',turn_id:1});
  assert.equal(comparison.playbackComplete(1),false);comparison.endCall();assert.equal(comparison.pair().baseline.status,'failed');
  start(comparison);comparison.accept({type:'interrupted',turn_id:1,heard:'Only the start.'});assert.equal(comparison.playbackComplete(1),false);
  assert.equal(comparison.pair().baseline.heard_text,'Only the start.');
});

test('same-turn voice retry preserves the prompt but requires fresh server completion and playback',()=>{
  const {comparison,tick}=setup();start(comparison);comparison.accept({type:'error',turn_id:1});
  comparison.accept({type:'turn_start',turn_id:1,voice_retry:true});assert.equal(comparison.draft.prompt,'Tell me about the painting.');
  assert.equal(comparison.playbackComplete(1),false);tick(55);comparison.audioQueued(1);comparison.accept({type:'response_done',turn_id:1});comparison.playbackComplete(1);
  assert.equal(comparison.pair().baseline.voice_retry,true);assert.equal(comparison.pair().baseline.browser_audio_queued_ms,55);
});

test('matching compares settings, input kind, prior conversation and finished playback, not a generated quality score',()=>{
  const {comparison}=setup();start(comparison);finish(comparison);comparison.endCall();
  start(comparison,{engine:'qwen-omni'});finish(comparison);const pair=comparison.pair();assert.equal(comparableReplies(pair.baseline,pair['qwen-omni']),true);
  for(const alteration of [{input_kind:'speech'},{input_kind:'stage'},{prompt:'Different prompt.'},{prior_context:[{prompt:'Earlier question.'}]},
    {settings:comparisonProfile(profile('Other name'),'robin')},{playback_complete:false},{status:'interrupted'}])
    assert.equal(comparableReplies(pair.baseline,{...pair['qwen-omni'],...alteration}),false);
  comparison.accept({type:'turn_start',turn_id:2});comparison.accept({type:'transcript',turn_id:2,text:'What next?'});finish(comparison,2);
  assert.deepEqual(comparison.pair()['qwen-omni'].prior_context,[{prompt:'Tell me about the painting.',reply:'The catalogue names the artist.',status:'complete'}]);
});

test('new calls reset turn history while results retain only the latest A/B samples per character',()=>{
  const {comparison}=setup();start(comparison);finish(comparison);comparison.endCall();start(comparison,{engine:'qwen-omni'});finish(comparison);comparison.endCall();
  assert.equal(comparison.results.length,2);start(comparison);finish(comparison,1,'A newer reply.');assert.deepEqual(comparison.draft.prior_context,[]);assert.equal(comparison.results.length,2);
  assert.equal(comparison.pair().baseline.reply,'A newer reply.');comparison.endCall();
  for(let index=0;index<15;index++){start(comparison,{id:`character-${index}`});finish(comparison);comparison.endCall();}
  assert.equal(comparison.results.length,12);assert.equal(comparison.accept({type:'text_done',turn_id:1,text:'LATE'}),false);
});

test('dialog renders literal text and honest timing labels, and exports only on an explicit action',async()=>{
  const {comparison,nodes,files}=setup();start(comparison);finish(comparison,1,'<img src=x onerror=alert(1)>');comparison.endCall();
  assert.deepEqual(files,[]);nodes.get('open-comparison').emit('click');assert.equal(nodes.get('comparison-dialog').open,true);
  const rendered=content(nodes.get('comparison-results'));assert.match(rendered,/<img src=x onerror=alert\(1\)>/);assert.match(rendered,/Not measured/);
  assert.match(rendered,/Server estimate to first audio/);assert.doesNotMatch(rendered,/consistency score|speaker onset:|NaN|undefined/);
  assert.equal(await comparison.download(),true);const exported=JSON.parse(files[0].content);assert.equal(exported.format,'character-studio-engine-comparison');assert.equal(exported.matched,false);
  assert.equal(exported.results[0].reply,'<img src=x onerror=alert(1)>');assert.match(exported.note,/not measured speaker onset/);
  nodes.get('close-comparison').emit('click');assert.equal(nodes.get('comparison-dialog').open,false);
});

test('clear and character changes prevent late export feedback from reappearing',async()=>{
  let reject;const {comparison,nodes}=setup({saveFile:()=>new Promise((_,no)=>{reject=no;})});start(comparison);finish(comparison);
  const pending=comparison.download();comparison.clearResults();reject(new Error('PRIVATE_BODY'));assert.equal(await pending,false);
  assert.equal(nodes.get('comparison-export-status').textContent,'');assert.equal(nodes.get('download-comparison').disabled,true);assert.equal(comparison.results.length,0);
  start(comparison);finish(comparison);const failed=comparison.download();reject(new Error('PRIVATE_SERVER_TEXT'));assert.equal(await failed,false);
  assert.equal(nodes.get('comparison-export-status').textContent,'The comparison could not be downloaded. Please try again.');
  assert.doesNotMatch(nodes.get('comparison-export-status').textContent,/PRIVATE/);
});

test('malformed timing values and source payloads do not leak into comparison exports',()=>{
  const values={};for(const key of ['first_text_ms','first_audio_ms','stt_ms','director_ms','archive_ms','complete_ms'])values[key]=-1;
  assert.deepEqual(comparisonMetrics(values),{});assert.deepEqual(comparisonMetrics({first_text_ms:0,first_audio_ms:3600000,complete_ms:3600001,archive_ms:NaN,raw:'PRIVATE'}),{first_text_ms:0,first_audio_ms:3600000});
  const {comparison}=setup();start(comparison,{engine:'qwen-omni'});comparison.accept({type:'archive_sources',turn_id:1,matches:null});
  comparison.accept({type:'metrics',turn_id:1,voice_verification:false,voice_pipeline:'qwen-omni-stream-v1'});finish(comparison);
  assert.equal(comparison.pair()['qwen-omni'].voice_verification,false);assert.deepEqual(comparison.pair()['qwen-omni'].citations,[]);
});

test('native UI wiring uses advertised audio rate and never previews the baseline reference as a native speaker',()=>{
  const script=readFileSync(new URL('../frontend/live.js',import.meta.url),'utf8'),html=readFileSync(new URL('../frontend/live.html',import.meta.url),'utf8');
  assert.match(script,/\.\.\.comparison\.startFields\(\)/);assert.match(script,/playback\?\.setRate\(data\.output_rate\?\?44100\)/);
  assert.match(script,/\$\('preset-voice-credit'\)\.hidden=native/);assert.match(script,/\$\('preset-audio'\)\.hidden=native\|\|/);
  assert.match(script,/data\.streaming===true\?\(streamingAudioQueued/);assert.match(html,/Turn-based conversation|turn-based/);
  assert.match(html,/A transcript alone does not prove every word was heard/);assert.doesNotMatch(html,/continuous full.duplex/i);
});
