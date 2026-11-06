import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {ArchivePanel,displayArchiveImageURL} from '../frontend/live-archive.mjs';

function element(tag='div'){
  return {tagName:tag.toUpperCase(),children:[],listeners:new Map(),attributes:new Map(),hidden:false,disabled:false,open:false,checked:false,value:'',textContent:'',
    set innerHTML(_){throw new Error('Museum text must never become HTML.');},
    append(...children){this.children.push(...children);},replaceChildren(...children){this.children=[...children];},
    setAttribute(key,value){this.attributes.set(key,value);},addEventListener(type,handler){this.listeners.set(type,handler);},
    dispatch(type){return this.listeners.get(type)?.({target:this,preventDefault(){}});},
    querySelectorAll(selector){const tags=selector.split(',').map(value=>value.trim().toUpperCase());return this.children.flatMap(child=>[...(tags.includes(child.tagName)?[child]:[]),...child.querySelectorAll(selector)]);},
    showModal(){this.open=true;},close(){this.open=false;this.dispatch('close');}};
}
const sources=[{id:'met',label:'The Metropolitan Museum of Art',collection_id:'metropolitan-museum-of-art'},
  {id:'aic',label:'Art Institute of Chicago',collection_id:'art-institute-of-chicago'},
  {id:'cleveland',label:'Cleveland Museum of Art'}, {id:'rijksmuseum',label:'Rijksmuseum'},{id:'getty',label:'J. Paul Getty Museum'}];
const nga={id:'national-gallery-of-art',label:'National Gallery of Art',source_kind:'nga',source_url:'https://www.nga.gov/',count:146454,has_images:true};
const aic={id:'art-institute-of-chicago',label:'Art Institute of Chicago',source_kind:'custom',source_url:'https://www.artic.edu/',count:2,has_images:true};
const page={entries:[],total:0,matched:0,offset:0,limit:25,scope:'library',collections:true};
const success=(extra={})=>({results:[{source:'aic',collection:aic,added:2,duplicates:0,total:2}],errors:[],...extra});
function setup(handler){
  const nodes=new Map(),calls=[],changes=[];const document={getElementById:id=>{if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);},createElement:element};
  const fetch=async(path,options)=>{
    calls.push({path,options});let result=await handler?.(path,options);
    if(result===undefined)result=path==='/api/archive/museums'?{sources}:path==='/api/archive/collections'?{collections:[nga,aic]}:path==='/api/archive/museums/import'?success():page;
    return result?.status?{ok:false,status:result.status,json:async()=>{throw new Error('PRIVATE_RESPONSE');}}:{ok:true,status:200,json:async()=>result};
  };
  const panel=new ArchivePanel({document,fetch,onChange:settings=>changes.push(settings)});panel.setCollections([nga]);panel.render();
  return {panel,get:id=>document.getElementById(id),calls,changes};
}
const content=node=>[node.textContent,...node.children.map(content)].join('\n');
const settle=()=>new Promise(resolve=>setImmediate(resolve));
async function ready(options){const result=setup(options);await result.panel.openMuseum();result.get('archive-museum-source').value='aic';result.get('archive-museum-query').value=' Monet ';return result;}

test('museum discovery is lazy and offers five official sources without selecting or adding empty collections',async()=>{
  const {panel,get,calls,changes}=setup();assert.deepEqual(calls,[]);assert.equal(await panel.openMuseum(),true);
  assert.equal(calls[0].path,'/api/archive/museums');assert.equal(get('archive-museum-dialog').open,true);
  assert.deepEqual(get('archive-museum-source').children.map(option=>option.value),['met','aic','cleveland','rijksmuseum','getty']);
  assert.equal(get('archive-museum-limit').value,'25');assert.equal(get('archive-museum-submit').disabled,false);
  assert.deepEqual(panel.collections,[nga]);assert.deepEqual(changes,[]);
});

test('museum import sends the exact bounded query, retains NGA and selects the returned collection ID rather than the provider ID',async()=>{
  const {panel,get,calls,changes}=await ready();panel.apply({enabled:true,scope:'library',collection_ids:[nga.id]});
  get('archive-museum-source').value='aic';get('archive-museum-query').value=' Monet ';get('archive-museum-limit').value='50';
  assert.equal(await panel.importMuseum(),true);await settle();
  const request=calls.find(value=>value.path==='/api/archive/museums/import');assert.equal(request.options.method,'POST');
  assert.equal(request.options.redirect,'error');assert.deepEqual(request.options.headers,{'Content-Type':'application/json'});
  assert.deepEqual(JSON.parse(request.options.body),{source:'aic',query:'Monet',limit:50});
  assert.deepEqual(panel.settings(),{enabled:true,scope:'library',entry_ids:[],collection_ids:[aic.id,nga.id]});
  assert.deepEqual(changes.at(-1).collection_ids,[aic.id,nga.id]);assert.equal(get('archive-museum-dialog').open,false);
  assert.equal(calls.some(value=>value.path==='/api/archive/collections'),true);
  assert.equal(panel.query,'Monet');assert.equal(get('archive-search').value,'Monet');
  const browse=calls.find(value=>value.path.startsWith('/api/archive/collections/entries?'));
  const params=new URL(browse.path,'http://test').searchParams;
  assert.equal(params.get('ids'),aic.id+','+nga.id);assert.equal(params.get('q'),'Monet');assert.equal(params.get('offset'),'0');
  assert.match(get('archive-status').textContent,/Imported 2 catalogue records/);
  await panel.search();assert.equal(get('archive-status').textContent,'');
});

test('import pending locks source edits and keeps progress visible until the response finishes',async()=>{
  let resolve;const {panel,get}=await ready(path=>path==='/api/archive/museums/import'?new Promise(done=>{resolve=done;}):undefined);
  const importing=panel.importMuseum();assert.equal(panel.pending,true);assert.equal(get('archive-enabled').disabled,true);
  assert.equal(get('archive-museum-submit').disabled,true);assert.equal(get('archive-museum-query').disabled,true);
  assert.equal(get('archive-add-collection').disabled,true);assert.equal(get('archive-add-museum').disabled,true);
  assert.equal(get('archive-museum-error').textContent,'Importing catalogue records…');assert.equal(get('archive-museum-form').attributes.get('aria-busy'),'true');
  assert.equal(await panel.openMuseum(),false);resolve(success());assert.equal(await importing,true);await settle();assert.equal(panel.pending,false);
});

test('invalid museum IDs, blank or long searches, controls and unlisted limits never reach the import endpoint',async()=>{
  const {panel,get,calls}=await ready();const before=calls.length;
  for(const [source,query,limit] of [['unknown','Monet','25'],['aic',' ','25'],['aic','x'.repeat(201),'25'],['aic','Monet\nsecret','25'],['aic','Monet','26'],['aic','Monet','NaN']]){
    get('archive-museum-source').value=source;get('archive-museum-query').value=query;get('archive-museum-limit').value=limit;
    assert.equal(await panel.importMuseum(),false);
  }
  assert.equal(calls.length,before);assert.deepEqual(panel.settings().entry_ids,[]);
});

test('missing museum API shows an update hint and leaves existing archive collections and controls usable',async()=>{
  const {panel,get}=setup(path=>path==='/api/archive/museums'?{status:404}:undefined);
  assert.equal(await panel.openMuseum(),false);assert.match(get('archive-museum-error').textContent,/Update, restart and refresh/);
  assert.equal(get('archive-museum-submit').disabled,true);assert.equal(get('archive-add-collection').disabled,false);
  assert.deepEqual(panel.collections,[nga]);assert.equal(await panel.importMuseum(),false);
});

test('provider failures use fixed readable messages and never reflect upstream text or mutate selected collections',async()=>{
  const {panel,get,changes}=await ready(path=>path==='/api/archive/museums/import'?{results:[],errors:[{source:'aic',reason:'upstream_error',message:'<script>PRIVATE_UPSTREAM</script>'}]}:undefined);
  const original=panel.settings();assert.equal(await panel.importMuseum(),false);
  assert.match(get('archive-museum-error').textContent,/Art Institute of Chicago: The museum import could not be completed/);
  assert.doesNotMatch(get('archive-museum-error').textContent,/script|PRIVATE_UPSTREAM/);assert.deepEqual(panel.settings(),original);assert.deepEqual(changes,[]);
});

test('an import finishing after a character switch updates global metadata but cannot change the newer character or reopen a dialog',async()=>{
  let resolve;const {panel,get,changes}=await ready(path=>path==='/api/archive/museums/import'?new Promise(done=>{resolve=done;}):undefined);
  const importing=panel.importMuseum();panel.apply({enabled:false,scope:'library',collection_ids:[nga.id]});resolve(success());
  assert.equal(await importing,true);await settle();assert.deepEqual(panel.settings(),{enabled:false,scope:'library',entry_ids:[],collection_ids:[nga.id]});
  assert.equal(panel.collections.some(value=>value.id===aic.id),true);assert.deepEqual(changes,[]);assert.equal(get('archive-museum-dialog').open,false);
});

test('a full eight-collection selection is preserved rather than silently replacing NGA or another chosen source',async()=>{
  const {panel,get,changes}=await ready();const original=[nga.id,...Array.from({length:7},(_,index)=>'custom-'+index)];
  panel.apply({enabled:true,collection_ids:original});get('archive-museum-source').value='aic';get('archive-museum-query').value='Monet';
  assert.equal(await panel.importMuseum(),true);assert.deepEqual(panel.settings().collection_ids,original);assert.deepEqual(changes,[]);
  assert.match(get('archive-status').textContent,/eight selected collections were kept/);assert.equal(panel.collections.some(value=>value.id===aic.id),true);
});

test('an explicit zero-result import can register its named collection without pretending the whole museum was loaded',async()=>{
  const {panel,get}=await ready(path=>path==='/api/archive/museums/import'?success({results:[{source:'aic',collection:{...aic,count:0,has_images:false},added:0,total:0}]}):undefined);
  assert.equal(await panel.importMuseum(),true);await settle();assert.match(get('archive-status').textContent,/Imported 0 catalogue records/);
  assert.match(get('archive-status').textContent,/search subset, not the entire museum/);
});

test('busy calls prevent museum import and close unsaved museum input without sending requests',async()=>{
  const {panel,get,calls}=await ready();panel.setBusy(true);const before=calls.length;
  assert.equal(get('archive-museum-dialog').open,false);assert.equal(get('archive-museum-query').value,'');
  assert.equal(await panel.importMuseum(),false);assert.equal(await panel.openMuseum(),false);assert.equal(calls.length,before);
});

test('only canonical400px Art Institute images use the local image endpoint; other public image URLs retain their existing policy',()=>{
  const uuid='2d484387-2509-5e8e-2c43-22f9981972eb',base='https://www.artic.edu/iiif/2/'+uuid;
  assert.equal(displayArchiveImageURL(base+'/full/400,/0/default.jpg'),'/api/archive/museum-images/aic/'+uuid);
  for(const value of [base+'/full/843,/0/default.jpg',base+'/full/400,/0/default.jpg?x=1',base+'/full/400,/0/default.jpg#bad',
    'https://www.artic.edu.evil.example/iiif/2/'+uuid+'/full/400,/0/default.jpg','https://api.nga.gov/test.jpg'])assert.equal(displayArchiveImageURL(value),value);
  for(const value of ['javascript:alert(1)','data:image/gif;base64,bad','https://user:secret@www.artic.edu/x','http://www.artic.edu/x'])assert.equal(displayArchiveImageURL(value),null);
});

test('museum cards retain bounded literal rights text in browsing, source cards and artwork captions',()=>{
  const {panel,get}=setup();panel.apply({enabled:true,collection_ids:[aic.id]});
  const uuid='2d484387-2509-5e8e-2c43-22f9981972eb';
  const entry={id:'a'.repeat(32),collection_id:aic.id,collection_label:aic.label,source_kind:'custom',title:'Test catalogue record',text:'Test record only.',
    source_url:'https://www.artic.edu/artworks/27992',image_url:'https://www.artic.edu/iiif/2/'+uuid+'/full/400,/0/default.jpg',metadata:{artist:'Test artist',rights:'CC0 catalogue metadata <b>literal</b>'}};
  panel.setEntries([entry]);panel.render();assert.match(content(get('archive-entries')),/CC0 catalogue metadata <b>literal<\/b>/);
  panel.setTurn(1);assert.equal(panel.showSources({type:'archive_sources',turn_id:1,scope:'library',collection_ids:[aic.id],selected_count:1,matches:[entry]}),true);
  assert.match(content(get('archive-sources-list')),/CC0 catalogue metadata/);assert.match(content(get('archive-artworks-list')),/CC0 catalogue metadata/);
  assert.equal(get('archive-artworks-list').querySelectorAll('img')[0].src,'/api/archive/museum-images/aic/'+uuid);
  assert.equal(get('archive-artworks-list').querySelectorAll('img')[0].referrerPolicy,'no-referrer');
});

test('museum help links the exact supplied guide and clearly describes limited catalogue imports',()=>{
  const html=readFileSync(new URL('../frontend/live.html',import.meta.url),'utf8');
  assert.match(html,/https:\/\/www\.navigating\.art\/education\/open-access-2026/);
  assert.match(html,/This adds a search subset, not the entire museum/);assert.match(html,/id="archive-museum-query" required maxlength="200"/);
  assert.match(html,/id="archive-museum-dialog" aria-labelledby="archive-museum-title" aria-describedby="archive-museum-description"/);
});
