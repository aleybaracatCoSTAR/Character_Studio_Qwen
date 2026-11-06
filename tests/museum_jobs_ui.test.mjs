import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {ArchivePanel} from '../frontend/live-archive.mjs';

function element(tag='div'){
  return {tagName:tag.toUpperCase(),children:[],listeners:new Map(),attributes:new Map(),hidden:false,disabled:false,open:false,checked:false,value:'',textContent:'',
    set innerHTML(_){throw new Error('Museum progress must remain literal text.');},
    append(...children){this.children.push(...children);},replaceChildren(...children){this.children=[...children];},
    setAttribute(key,value){this.attributes.set(key,value);},addEventListener(type,handler){this.listeners.set(type,handler);},
    dispatch(type){return this.listeners.get(type)?.({target:this,preventDefault(){}});},
    querySelectorAll(selector){const tags=selector.split(',').map(value=>value.trim().toUpperCase());return this.children.flatMap(child=>[...(tags.includes(child.tagName)?[child]:[]),...child.querySelectorAll(selector)]);},
    showModal(){this.open=true;},close(){this.open=false;this.dispatch('close');}};
}
const ids=['met','aic','cleveland','rijksmuseum','getty'];
const sources=ids.map(id=>({id,label:id}));
const nga={id:'national-gallery-of-art',label:'NGA',source_kind:'nga',count:146454,source_url:'https://www.nga.gov/',has_images:true};
const aic={id:'art-institute-of-chicago',label:'Art Institute of Chicago',source_kind:'museum-aic',count:1000,source_url:'https://www.artic.edu/',has_images:true};
const page={entries:[],total:0,matched:0,offset:0,limit:25,scope:'library',collections:true};
const running=(extra={})=>({id:'durable-job-1',status:'running',sources:[...ids],current_source:'met',processed:12,added:10,updated:1,duplicates:1,images:4,total_expected:null,message:'Importing a published catalogue.',errors:[],results:[],...extra});
const result={source:'aic',collection:aic,added:1000,duplicates:0,total:1000};
const settle=()=>new Promise(resolve=>setImmediate(resolve));
async function drained(){await settle();await settle();}
function setup(handler){
  const nodes=new Map(),calls=[],changes=[],timers=new Map();let clock=0,current=null;
  const document={getElementById:id=>{if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);},createElement:element};
  const fetch=async(path,options)=>{
    calls.push({path,options});let value=await handler?.(path,options);
    if(value===undefined)value=path==='/api/archive/museums'?{sources}:path==='/api/archive/museums/imports/current'?{job:current}:
      path==='/api/archive/collections'?{collections:[nga,aic]}:path==='/api/archive/museums/import'?(current=running(),{job:current}):page;
    return value?.status?{ok:false,status:value.status,json:async()=>{throw new Error('PRIVATE_BODY');}}:{ok:true,status:200,json:async()=>value};
  };
  const panel=new ArchivePanel({document,fetch,onChange:settings=>changes.push(settings),
    schedule:(callback,delay)=>{const id=++clock;timers.set(id,{callback,delay});return id;},cancelSchedule:id=>timers.delete(id)});
  panel.setCollections([nga]);panel.apply({enabled:true,collection_ids:[nga.id]});panel.render();
  return {panel,get:id=>document.getElementById(id),calls,changes,timers,setCurrent:value=>{current=value;},
    tick:async()=>{const next=timers.entries().next().value;if(next){timers.delete(next[0]);next[1].callback();await drained();}}};
}
async function ready(handler){const fixture=setup(handler);await fixture.panel.openMuseum();return fixture;}
function full(fixture,source='all'){
  fixture.get('archive-museum-mode').value='full';fixture.get('archive-museum-mode').dispatch('change');fixture.get('archive-museum-source').value=source;
}

test('the existing dialog defaults to matching artworks and exposes All five only for full imports',async()=>{
  const fixture=await ready();const {panel,get}=fixture;
  assert.equal(panel.museumMode,'matching');assert.equal(get('archive-museum-matching').hidden,false);assert.equal(get('archive-museum-query').required,true);
  assert.equal(get('archive-museum-source').children.some(option=>option.value==='all'),false);assert.equal(get('archive-museum-full-option').disabled,false);
  full(fixture);assert.equal(get('archive-museum-matching').hidden,true);assert.equal(get('archive-museum-full-note').hidden,false);
  assert.equal(get('archive-museum-query').disabled,true);assert.equal(get('archive-museum-query').required,false);assert.equal(get('archive-museum-limit').disabled,true);
  assert.equal(get('archive-museum-source').children[0].value,'all');assert.equal(get('archive-museum-submit').textContent,'Start background import');
  get('archive-museum-mode').value='matching';get('archive-museum-mode').dispatch('change');
  assert.equal(get('archive-museum-source').value,'met');assert.equal(get('archive-museum-source').children.some(option=>option.value==='all'),false);
});

test('full imports send one serial-all job request without hidden query or numeric limits',async()=>{
  const fixture=await ready();full(fixture);fixture.get('archive-museum-query').value='DO_NOT_SEND';fixture.get('archive-museum-limit').value='26';
  assert.equal(await fixture.panel.importMuseum(),true);
  const request=fixture.calls.find(call=>call.path==='/api/archive/museums/import');
  assert.deepEqual(JSON.parse(request.options.body),{source:'all',scope:'full'});assert.equal(request.options.method,'POST');assert.equal(request.options.redirect,'error');
  assert.deepEqual(request.options.headers,{'Content-Type':'application/json'});assert.equal(fixture.calls.filter(call=>call.path==='/api/archive/museums/import').length,1);
  assert.equal(fixture.panel.pending,false);assert.equal(fixture.get('archive-enabled').disabled,false);assert.equal(fixture.get('archive-museum-submit').disabled,true);
  assert.match(fixture.get('archive-museum-job-state').textContent,/Import running/);assert.match(fixture.get('archive-museum-job-counts').textContent,/12 processed · 10 new · 1 updated · 1 duplicates skipped · 4 image links/);
  assert.equal(fixture.get('archive-museum-job-progress').hidden,true);assert.equal(fixture.timers.size,1);
});

test('only the short start request holds pending; background work leaves normal archive and call readiness free',async()=>{
  let resolve;const fixture=await ready(path=>path==='/api/archive/museums/import'?new Promise(done=>{resolve=done;}):undefined);full(fixture);
  const request=fixture.panel.importMuseum();assert.equal(fixture.panel.pending,true);assert.equal(fixture.get('archive-enabled').disabled,true);
  resolve({job:running()});assert.equal(await request,true);assert.equal(fixture.panel.pending,false);assert.equal(fixture.get('archive-enabled').disabled,false);
  assert.equal(fixture.get('archive-add-collection').disabled,false);assert.equal(fixture.get('archive-museum-job-stop').disabled,false);
});

test('progress polling is bounded to one two-second timer and updates without starting five parallel imports',async()=>{
  const fixture=await ready();full(fixture);await fixture.panel.importMuseum();assert.equal([...fixture.timers.values()][0].delay,2000);
  fixture.setCurrent(running({processed:500,added:450,images:80,current_source:'aic',total_expected:2000}));await fixture.tick();
  assert.match(fixture.get('archive-museum-job-state').textContent,/Art Institute of Chicago/);assert.equal(fixture.get('archive-museum-job-progress').value,500);
  assert.equal(fixture.get('archive-museum-job-progress').max,2000);assert.equal(fixture.timers.size,1);assert.equal(fixture.panel.pending,false);
  assert.equal(fixture.calls.filter(call=>call.path==='/api/archive/museums/import').length,1);assert.deepEqual(fixture.changes,[]);
});

test('closing the dialog stops polling while the durable job remains discoverable after reopening',async()=>{
  const fixture=await ready();full(fixture);await fixture.panel.importMuseum();fixture.setCurrent(running({processed:700}));
  fixture.get('archive-museum-dialog').close();assert.equal(fixture.timers.size,0);assert.equal(fixture.panel.museumJob.status,'running');
  await fixture.panel.openMuseum();assert.equal(fixture.panel.museumJob.processed,700);assert.equal(fixture.timers.size,1);
  assert.equal(fixture.calls.filter(call=>call.path==='/api/archive/museums/import').length,1);
});

test('stop is cooperative, uses only the validated job ID and exposes resume after the batch checkpoint pauses',async()=>{
  const fixture=await ready(path=>path.endsWith('/stop')?{job:running({status:'paused',processed:500,results:[result]})}:undefined);
  full(fixture);await fixture.panel.importMuseum();assert.equal(await fixture.panel.stopMuseumJob(),true);await drained();
  const request=fixture.calls.find(call=>call.path.endsWith('/stop'));assert.equal(request.path,'/api/archive/museums/imports/durable-job-1/stop');assert.equal(request.options.method,'POST');
  assert.equal(fixture.timers.size,0);assert.match(fixture.get('archive-museum-job-state').textContent,/Import paused/);
  assert.equal(fixture.get('archive-museum-job-resume').hidden,false);assert.equal(fixture.get('archive-museum-job-stop').hidden,true);
  assert.equal(await fixture.panel.resumeMuseumJob(),true);assert.deepEqual(JSON.parse(fixture.calls.filter(call=>call.path==='/api/archive/museums/import').at(-1).options.body),{source:'all',scope:'full'});
  assert.equal(fixture.panel.museumJob.status,'running');assert.equal(fixture.timers.size,1);
});

test('a requested stop stays disabled while the current batch is still running',async()=>{
  const fixture=await ready(path=>path.endsWith('/stop')?{job:running({message:'Finishing this batch.'})}:undefined);full(fixture);await fixture.panel.importMuseum();
  assert.equal(await fixture.panel.stopMuseumJob(),true);assert.equal(fixture.get('archive-museum-job-stop').disabled,true);
  assert.match(fixture.get('archive-museum-job-state').textContent,/Stopping after the current batch/);assert.equal(await fixture.panel.stopMuseumJob(),false);
});

test('single-museum resume keeps that provider and does not expand into an all-museum job',async()=>{
  const fixture=await ready();fixture.setCurrent(running({sources:['aic'],status:'paused',current_source:'aic'}));await fixture.panel.loadCurrentMuseumJob();await drained();
  assert.equal(await fixture.panel.resumeMuseumJob(),true);const request=fixture.calls.find(call=>call.path==='/api/archive/museums/import');
  assert.deepEqual(JSON.parse(request.options.body),{source:'aic',scope:'full'});
});

test('terminal progress refreshes registry counts and adds actual result collections only to the initiating current character',async()=>{
  const fixture=await ready();full(fixture);await fixture.panel.importMuseum();
  fixture.setCurrent(running({status:'completed',processed:1000,added:1000,results:[result]}));await fixture.tick();await drained();
  assert.equal(fixture.timers.size,0);assert.deepEqual(fixture.panel.settings().collection_ids,[aic.id,nga.id]);
  assert.deepEqual(fixture.changes.at(-1).collection_ids,[aic.id,nga.id]);assert.equal(fixture.panel.scope,'library');
  assert.equal(fixture.calls.filter(call=>call.path==='/api/archive/collections').length>=1,true);assert.match(fixture.get('archive-museum-job-state').textContent,/Import finished/);
});

test('completed work discovered after a reload updates counts without silently changing that page character',async()=>{
  const fixture=setup();fixture.setCurrent(running({status:'completed',results:[result]}));await fixture.panel.openMuseum();await drained();
  assert.deepEqual(fixture.panel.settings().collection_ids,[nga.id]);assert.deepEqual(fixture.changes,[]);assert.equal(fixture.panel.collections.some(collection=>collection.id===aic.id),true);
});

test('archive choices edited on the same character during a long job take precedence over automatic terminal selection',async()=>{
  const custom={id:'personal',label:'My references',source_kind:'custom',count:2};
  const fixture=await ready(path=>path==='/api/archive/collections'?{collections:[nga,aic,custom]}:undefined);
  full(fixture);await fixture.panel.importMuseum();const epoch=fixture.panel.profileEpoch;
  fixture.panel.setCollections([nga,custom]);fixture.panel.render();
  const checkbox=fixture.get('archive-collections').querySelectorAll('input')[1];checkbox.checked=true;checkbox.dispatch('change');await drained();
  assert.equal(fixture.panel.profileEpoch,epoch);assert.deepEqual(fixture.panel.settings().collection_ids,[nga.id,custom.id]);
  fixture.setCurrent(running({status:'completed',results:[result]}));await fixture.tick();await drained();
  assert.deepEqual(fixture.panel.settings().collection_ids,[nga.id,custom.id]);
});

test('late start or progress responses cannot add collections to a switched character',async()=>{
  let resolve;const fixture=await ready(path=>path==='/api/archive/museums/import'?new Promise(done=>{resolve=done;}):undefined);full(fixture);
  const starting=fixture.panel.importMuseum();fixture.panel.apply({enabled:false,collection_ids:[nga.id]});
  resolve({job:running({status:'completed',results:[result]})});assert.equal(await starting,true);await drained();
  assert.deepEqual(fixture.panel.settings(),{enabled:false,scope:'library',entry_ids:[],collection_ids:[nga.id]});assert.deepEqual(fixture.changes,[]);
  assert.equal(fixture.get('archive-museum-dialog').open,false);
});

test('a call may start during a background import and freezes character choices while polling is closed',async()=>{
  const fixture=await ready();full(fixture);await fixture.panel.importMuseum();assert.equal(fixture.panel.pending,false);
  fixture.panel.setBusy(true);assert.equal(fixture.get('archive-museum-dialog').open,false);assert.equal(fixture.timers.size,0);
  assert.equal(await fixture.panel.stopMuseumJob(),false);fixture.panel.acceptMuseumJob(running({status:'completed',results:[result]}));
  assert.deepEqual(fixture.panel.settings().collection_ids,[nga.id]);assert.deepEqual(fixture.changes,[]);
});

test('the eight-collection cap preserves prior choices and explains how to select imported sources',async()=>{
  const fixture=await ready();const prior=[nga.id,...Array.from({length:7},(_,index)=>'personal-'+index)];fixture.panel.apply({enabled:true,collection_ids:prior});
  await fixture.panel.openMuseum();full(fixture);await fixture.panel.importMuseum();fixture.setCurrent(running({status:'completed',results:[result]}));await fixture.tick();await drained();
  assert.deepEqual(fixture.panel.settings().collection_ids,prior);assert.match(fixture.get('archive-museum-job-message').textContent,/Uncheck a collection/);assert.deepEqual(fixture.changes,[]);
});

test('an old server without job support retains matching imports and provides an honest update hint',async()=>{
  const fixture=await ready(path=>path==='/api/archive/museums/imports/current'?{status:404}:undefined);
  assert.equal(fixture.get('archive-museum-full-option').disabled,true);assert.equal(fixture.get('archive-museum-submit').disabled,false);
  assert.match(fixture.get('archive-museum-error').textContent,/Update, restart and refresh/);full(fixture);assert.equal(await fixture.panel.importMuseum(),false);
  assert.equal(fixture.calls.some(call=>call.path==='/api/archive/museums/import'),false);assert.equal(fixture.get('archive-add-collection').disabled,false);
});

test('numeric totals or adapter errors never turn a still-running or failed import into a claim of full completion',async()=>{
  const fixture=await ready();full(fixture);await fixture.panel.importMuseum();
  fixture.panel.acceptMuseumJob(running({processed:200,total_expected:200,message:'A batch checkpoint was saved.'}));
  assert.match(fixture.get('archive-museum-job-state').textContent,/Import running/);assert.doesNotMatch(fixture.get('archive-museum-job-state').textContent,/finished|complete/i);
  fixture.panel.acceptMuseumJob(running({status:'failed',processed:200,errors:[{source:'aic',reason:'upstream_error',message:'PRIVATE'}]}));
  assert.match(fixture.get('archive-museum-job-state').textContent,/stopped with an error/);assert.equal(fixture.get('archive-museum-job-resume').hidden,false);
  fixture.panel.acceptMuseumJob(running({status:'completed',errors:[{source:'aic',reason:'upstream_error'}]}));
  assert.match(fixture.get('archive-museum-job-state').textContent,/finished with errors/);
});

test('poll errors retain the last known running job without reflecting raw server content or declaring it stopped',async()=>{
  let fail=false;const fixture=await ready(path=>path==='/api/archive/museums/imports/current'&&fail?{status:503}:undefined);full(fixture);await fixture.panel.importMuseum();
  fail=true;await fixture.tick();assert.equal(fixture.panel.museumJob.status,'running');assert.equal(fixture.timers.size,1);
  assert.match(fixture.get('archive-museum-job-message').textContent,/server may still be importing/);assert.doesNotMatch(fixture.get('archive-museum-job-message').textContent,/PRIVATE_BODY/);
});

test('job identifiers and source lists are validated before any stop route can be constructed',async()=>{
  const fixture=await ready();
  for(const job of [running({id:'../../private'}),running({sources:['unknown']}),running({sources:['aic','aic']}),running({status:'not-a-status'})]){
    assert.throws(()=>fixture.panel.acceptMuseumJob(job),/unreadable import progress/);
  }
  assert.equal(await fixture.panel.stopMuseumJob(),false);assert.equal(fixture.calls.some(call=>call.path.endsWith('/stop')),false);
});

test('full-import help accurately describes metadata, background checkpoints and separate matching limits',()=>{
  const html=readFileSync(new URL('../frontend/live.html',import.meta.url),'utf8');
  assert.match(html,/Entire published collection/);assert.match(html,/metadata and available image links, not image files/);
  assert.match(html,/stopped and resumed/);assert.match(html,/All five museums are imported one after another/);
  assert.match(html,/id="archive-museum-matching"/);assert.match(html,/id="archive-museum-progress"[^>]*aria-live="polite"/);
});

test('Use all art collections selects only nonempty trusted museum records and enables library search for this character',async()=>{
  const values=[nga,aic,
    {id:'qtm',label:'Stories',source_kind:'qtm',count:500},
    {id:'metropolitan-museum-of-art',label:'Met',source_kind:'museum-met',count:0},
    {id:'cleveland-museum-of-art',label:'An uploaded name',source_kind:'custom',count:50},
    {id:'personal',label:'Private collection',source_kind:'custom',count:20},
    {id:'untrusted-alias',label:'Alias',source_kind:'museum-aic',count:40}];
  const fixture=await ready(path=>path==='/api/archive/collections'?{collections:values}:undefined);
  fixture.panel.apply({enabled:false,scope:'selected',entry_ids:[]});assert.equal(await fixture.panel.useAllArtCollections(),true);await drained();
  assert.deepEqual(fixture.panel.settings(),{enabled:true,scope:'library',entry_ids:[],collection_ids:[nga.id,aic.id]});
  assert.equal(fixture.get('archive-enabled').checked,true);assert.match(fixture.get('archive-status').textContent,/records and image links imported so far/);
  assert.deepEqual(fixture.changes.at(-1).collection_ids,[nga.id,aic.id]);
});

test('Use all art collections can adopt available batches while the full job is still running',async()=>{
  const partial={id:'cleveland-museum-of-art',label:'Cleveland Museum of Art',source_kind:'museum-cleveland',count:35,has_images:true};
  const fixture=await ready(path=>path==='/api/archive/collections'?{collections:[nga,partial]}:undefined);
  full(fixture);await fixture.panel.importMuseum();assert.equal(fixture.panel.museumJob.status,'running');
  assert.equal(await fixture.panel.useAllArtCollections(),true);await drained();
  assert.deepEqual(fixture.panel.settings().collection_ids,[nga.id,partial.id]);assert.equal(fixture.panel.museumJob.status,'running');
  assert.equal(fixture.panel.pending,false);assert.equal(fixture.timers.size,1);
});

test('Use all art collections is blocked by calls and cannot apply a late registry response to another character',async()=>{
  let resolve;const fixture=await ready(path=>path==='/api/archive/collections'?new Promise(done=>{resolve=done;}):undefined);
  const selecting=fixture.panel.useAllArtCollections();fixture.panel.apply({enabled:false,collection_ids:[nga.id]});resolve({collections:[nga,aic]});
  assert.equal(await selecting,false);await drained();assert.deepEqual(fixture.panel.settings().collection_ids,[nga.id]);assert.equal(fixture.panel.enabled,false);
  fixture.panel.setBusy(true);const before=fixture.calls.length;assert.equal(await fixture.panel.useAllArtCollections(),false);assert.equal(fixture.calls.length,before);
  assert.equal(fixture.get('archive-use-art-collections').disabled,true);assert.deepEqual(fixture.changes,[]);
});

test('Use all art collections does not select empty museums or claim records are available before import',async()=>{
  const fixture=await ready(path=>path==='/api/archive/collections'?{collections:[{...nga,count:0},{...aic,count:0}]}:undefined);
  fixture.panel.apply({enabled:false,collection_ids:[nga.id]});assert.equal(await fixture.panel.useAllArtCollections(),false);
  assert.equal(fixture.panel.enabled,false);assert.match(fixture.get('archive-status').textContent,/No museum records are available yet/);assert.deepEqual(fixture.changes,[]);
});
