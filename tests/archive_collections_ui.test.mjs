import test from 'node:test';
import assert from 'node:assert/strict';
import {ArchivePanel,publicArchiveURL,MAX_ARCHIVE_IMPORT_BYTES} from '../frontend/live-archive.mjs';

function element(tag='div'){
  return {tagName:tag.toUpperCase(),children:[],listeners:new Map(),attributes:new Map(),hidden:false,disabled:false,open:false,checked:false,value:'',textContent:'',
    set innerHTML(_){throw new Error('Archive data must remain literal text.');},
    append(...children){this.children.push(...children);},replaceChildren(...children){this.children=[...children];},
    setAttribute(key,value){this.attributes.set(key,value);},addEventListener(type,handler){this.listeners.set(type,handler);},
    dispatch(type){return this.listeners.get(type)?.({target:this,preventDefault(){}});},
    querySelectorAll(selector){const tags=selector.split(',').map(value=>value.trim().toUpperCase());return this.children.flatMap(child=>[...(tags.includes(child.tagName)?[child]:[]),...child.querySelectorAll(selector)]);},
    showModal(){this.open=true;},close(){this.open=false;this.dispatch('close');}};
}
const collections=[{id:'qtm',label:'Queering the Map',source_kind:'qtm',source_url:'https://www.queeringthemap.com/',count:123209,has_images:false},
  {id:'national-gallery-of-art',label:'National Gallery of Art',source_kind:'nga',source_url:'https://www.nga.gov/',count:42,has_images:true}];
const art=(id='a'.repeat(32),extra={})=>({id,title:'A fictional test artwork',text:'A catalogue record from the test fixture.',
  source_url:'https://www.nga.gov/artworks/test-object',collection_id:'national-gallery-of-art',collection_label:'National Gallery of Art',source_kind:'nga',
  provenance:'imported',image_url:'https://api.nga.gov/iiif/test/full/!400,400/0/default.jpg',image_alt:'Test artwork image',
  metadata:{artist:'Fixture artist',date:'1901',medium:'Oil on canvas',rights:'Test rights statement'},...extra});
const page=entries=>({entries,total:entries.length,matched:entries.length,offset:0,limit:25,scope:'library',collections:true});
const event=(matches,extra={})=>({type:'archive_sources',turn_id:1,scope:'library',collection_ids:['national-gallery-of-art'],selected_count:42,matches,...extra});
function setup(handler){
  const nodes=new Map(),calls=[],changes=[];
  const document={getElementById:id=>{if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);},createElement:element};
  const fetch=async(path,options)=>{
    calls.push({path,options});const result=handler?await handler(path,options):path==='/api/archive/collections'?{collections}:page([]);
    return result?.status?{ok:false,status:result.status,json:async()=>{throw new Error('PRIVATE_RESPONSE');}}
      :{ok:true,status:200,json:async()=>result};
  };
  const panel=new ArchivePanel({document,fetch,onChange:value=>changes.push(value)});
  return {panel,nodes,calls,changes,get:id=>document.getElementById(id)};
}
const content=node=>[node.textContent,...node.children.map(content)].join('\n');
const settle=()=>new Promise(resolve=>setImmediate(resolve));

test('legacy settings stay exact while explicit collection IDs force whole-collection scope and clear selected stories',()=>{
  const {panel,get}=setup();assert.deepEqual(panel.settings(),{enabled:false,scope:'selected',entry_ids:[]});
  panel.apply({enabled:true,scope:'selected',entry_ids:['a'.repeat(32)],collection_ids:['national-gallery-of-art','national-gallery-of-art','../bad']});
  assert.deepEqual(panel.settings(),{enabled:true,scope:'library',entry_ids:[],collection_ids:['national-gallery-of-art']});
  assert.equal(get('archive-scope').disabled,true);assert.equal(get('archive-add').hidden,true);assert.equal(get('archive-qtm-import').hidden,true);
  assert.equal(get('archive-search-label').textContent,'Find records');assert.equal(get('archive-search-form').attributes.get('aria-label'),'Search saved records');assert.equal(get('archive-entries').attributes.get('aria-label'),'Saved records');
  get('archive-scope').value='selected';get('archive-scope').dispatch('change');assert.equal(panel.scope,'library');
  panel.apply({enabled:true,scope:'selected',entry_ids:[]});assert.equal(Object.hasOwn(panel.settings(),'collection_ids'),false);
  assert.equal(get('archive-add').hidden,false);assert.equal(get('archive-qtm-import').hidden,false);
  assert.equal(get('archive-search-label').textContent,'Find stories');assert.equal(get('archive-search-form').attributes.get('aria-label'),'Search saved stories');assert.equal(get('archive-entries').attributes.get('aria-label'),'Saved stories');
});

test('collection discovery displays names and counts without importing or changing the current character',async()=>{
  const {panel,get,calls,changes}=setup();assert.equal(await panel.loadCollections(),true);
  assert.equal(calls[0].path,'/api/archive/collections');assert.match(content(get('archive-collections')),/National Gallery of Art · 42 records/);
  assert.match(content(get('archive-collections')),/Queering the Map · 123,209 records/);
  assert.deepEqual(get('archive-collections').querySelectorAll('input').map(value=>value.checked),[true,false]);
  assert.deepEqual(changes,[]);assert.equal(get('archive-artworks').hidden,true);
});

test('explicit collection pages use filtered routes, show thumbnails and are read-only',async()=>{
  const {panel,get,calls}=setup(path=>path==='/api/archive/collections'?{collections}:page([art()]));
  await panel.loadCollections();panel.apply({enabled:true,collection_ids:['national-gallery-of-art']});await panel.load({query:'blue sky'});
  const query=new URL(calls.at(-1).path,'http://test');
  assert.equal(query.pathname,'/api/archive/collections/entries');assert.equal(query.searchParams.get('ids'),'national-gallery-of-art');assert.equal(query.searchParams.get('q'),'blue sky');
  assert.equal(get('archive-entries').querySelectorAll('button').length,0);assert.equal(get('archive-entries').querySelectorAll('img').length,1);
  assert.match(content(get('archive-entries')),/Fixture artist · 1901 · Oil on canvas/);
  assert.doesNotMatch(content(get('archive-entries')),/Queering the Map|CC BY-NC/);
  assert.equal(await panel.remove('a'.repeat(32)),false);assert.equal(await panel.submit(),false);assert.equal(await panel.upload(),false);
});

test('generic source URLs permit public HTTPS artwork routes and reject private, credentialled or executable URLs',()=>{
  assert.equal(publicArchiveURL('https://api.nga.gov/iiif/object/full/!400,400/0/default.jpg?quality=good'),'https://api.nga.gov/iiif/object/full/!400,400/0/default.jpg?quality=good');
  for(const value of ['http://www.nga.gov/object','javascript:alert(1)','data:image/svg+xml,bad','https://user:secret@example.org/x',
    'https://127.0.0.1/x','https://2130706433/x','https://[::1]/x','https://localhost/x','https://my-server.local/x',
    'https://private.internal/x','https://host.lan/x','https://www.nga.gov:444/x','https://www.nga.gov/\nprivate'])assert.equal(publicArchiveURL(value),null);
});

test('new collection upload sends bounded raw file data and chooses the returned collection without modifying QTM',async()=>{
  const added={id:'local-art',label:'Local art',source_kind:'custom',source_url:'https://example.org/',count:3,has_images:true};
  const {panel,get,calls,changes}=setup((path,options)=>path==='/api/archive/collections'?{collections}
    :options?.method==='POST'?{collection:added,added:3,duplicates:0,total:3}:page([art('b'.repeat(32),{collection_id:'local-art',collection_label:'Local art',source_kind:'custom'})]));
  await panel.loadCollections();assert.equal(panel.openCollection(),true);
  const file={name:'records with spaces.jsonl',size:MAX_ARCHIVE_IMPORT_BYTES};get('archive-collection-name').value=' Local art ';get('archive-collection-file').files=[file];
  assert.equal(await panel.createCollection(),true);await settle();
  const request=calls.find(value=>value.options?.method==='POST');assert.equal(request.path,'/api/archive/collections/import');assert.equal(request.options.body,file);
  assert.deepEqual(request.options.headers,{'Content-Type':'application/octet-stream','X-Archive-Filename':encodeURIComponent(file.name),'X-Collection-Name':'Local%20art'});
  assert.deepEqual(panel.settings(),{enabled:false,scope:'library',entry_ids:[],collection_ids:['local-art']});
  assert.equal(get('archive-collection-dialog').open,false);assert.equal(panel.collections.some(value=>value.id==='qtm'),true);
  assert.deepEqual(changes.at(-1),panel.settings());assert.equal(calls.some(value=>value.path==='/api/archive/import'),false);
});

test('invalid collection names and files do not send requests and failed responses preserve profile settings',async()=>{
  const {panel,get,calls}=setup();await panel.loadCollections();const before=calls.length;
  for(const [name,file] of [[' ',{name:'records.json',size:1}],['x'.repeat(81),{name:'records.json',size:1}],
    ['Valid',{name:'records.zip',size:1}],['Valid',{name:'records.csv',size:MAX_ARCHIVE_IMPORT_BYTES+1}],['Valid',{name:'records.csv',size:0}]]){
    get('archive-collection-name').value=name;get('archive-collection-file').files=[file];assert.equal(await panel.createCollection(),false);
  }
  assert.equal(calls.length,before);
  const failure=setup(path=>path==='/api/archive/collections'?{collections}:{status:409});await failure.panel.loadCollections();
  failure.panel.apply({enabled:true,collection_ids:['national-gallery-of-art']});failure.panel.openCollection();
  failure.get('archive-collection-name').value='Valid';failure.get('archive-collection-file').files=[{name:'records.json',size:1}];
  assert.equal(await failure.panel.createCollection(),false);assert.equal(failure.panel.pending,false);
  assert.deepEqual(failure.panel.settings().collection_ids,['national-gallery-of-art']);assert.doesNotMatch(failure.get('archive-collection-error').textContent,/PRIVATE_RESPONSE/);
});

test('active calls lock collection selection and uploads and clear the draft dialog',async()=>{
  const {panel,get,calls}=setup();await panel.loadCollections();panel.openCollection();get('archive-collection-name').value='Unsaved name';panel.setBusy(true);
  assert.equal(get('archive-collection-dialog').open,false);assert.equal(get('archive-collection-name').value,'');
  assert.equal(get('archive-add-collection').disabled,true);assert.equal(await panel.createCollection(),false);
  const checkbox=get('archive-collections').querySelectorAll('input')[1];checkbox.checked=true;checkbox.dispatch('change');
  assert.deepEqual(panel.collectionIds,[]);assert.equal(checkbox.checked,false);assert.equal(calls.length,1);
});

test('an import finishing after a profile change cannot select its collection for the newer character',async()=>{
  let finish;const {panel,get,changes}=setup(path=>path==='/api/archive/collections'?{collections}:new Promise(resolve=>{finish=resolve;}));
  await panel.loadCollections();get('archive-collection-name').value='Old profile data';get('archive-collection-file').files=[{name:'data.json',size:1}];
  const importing=panel.createCollection();panel.apply({enabled:true,collection_ids:['national-gallery-of-art']});panel.setBusy(true);
  finish({collection:{id:'old-profile',label:'Old profile data',count:2,source_kind:'custom'},added:2});assert.equal(await importing,true);
  assert.deepEqual(panel.settings(),{enabled:true,scope:'library',entry_ids:[],collection_ids:['national-gallery-of-art']});assert.deepEqual(changes,[]);
});

test('current-turn artwork sources auto-show bounded images and captions without applying QTM licensing to art',()=>{
  const {panel,get}=setup();panel.apply({enabled:true,collection_ids:['national-gallery-of-art']});panel.setTurn(1);
  const matches=Array.from({length:5},(_,index)=>art(String(index+1).padStart(32,'0'),{excerpt:'Retrieved artwork description.'}));
  assert.equal(panel.showSources(event(matches)),true);assert.equal(get('archive-artworks').hidden,false);
  assert.match(get('archive-sources-note').textContent,/42 records/);assert.doesNotMatch(get('archive-sources-note').textContent,/stories/);
  assert.equal(get('archive-artworks-list').children.length,3);assert.equal(get('archive-artworks-list').querySelectorAll('img').length,3);
  const images=get('archive-artworks-list').querySelectorAll('img');assert.ok(images.every(image=>image.loading==='lazy'&&image.referrerPolicy==='no-referrer'));
  assert.match(content(get('archive-artworks-list')),/Fixture artist · 1901 · Oil on canvas/);
  assert.doesNotMatch(content(get('archive-sources-list')),/Queering the Map|CC BY-NC/);
  assert.ok(get('archive-artworks-list').querySelectorAll('a').every(anchor=>anchor.target==='_blank'&&anchor.rel==='noopener noreferrer'));
  assert.equal(panel.showSources(event(matches,{turn_id:2})),false);assert.equal(panel.showSources(event(matches,{collection_ids:['qtm']})),false);
  assert.equal(panel.showSources(event(matches,{collection_ids:['national-gallery-of-art','../bad']})),false);
  assert.equal(panel.showSources(event([],{selected_count:0})),true);
  assert.equal(get('archive-sources-note').textContent,'No records were available in the selected collections for this call.');
});

test('records with the same ID in different collections retain distinct cards and artwork images',()=>{
  const {panel,get}=setup();panel.apply({enabled:true,collection_ids:['national-gallery-of-art','local-art']});panel.setTurn(1);
  const first=art(),second=art(first.id,{collection_id:'local-art',collection_label:'Local art',source_kind:'custom',title:'A different record'});
  assert.equal(panel.showSources(event([first,second,first],{collection_ids:['local-art','national-gallery-of-art']})),true);
  assert.equal(get('archive-sources-list').children.length,2);assert.equal(get('archive-artworks-list').children.length,2);
  panel.setPage(page([first,second,first]));panel.render();assert.equal(panel.entries.length,2);
});

test('image errors reveal a graceful fallback, while profile changes erase all old source images',()=>{
  const {panel,get}=setup();panel.apply({enabled:true,collection_ids:['national-gallery-of-art']});panel.setTurn(1);panel.showSources(event([art()]));
  const image=get('archive-artworks-list').querySelectorAll('img')[0],frame=image===undefined?null:get('archive-artworks-list').children[0].children[0];
  image.dispatch('error');assert.equal(image.hidden,true);assert.equal(frame.children[1].hidden,false);assert.equal(frame.children[1].textContent,'Image unavailable');
  panel.apply({enabled:true,entry_ids:[]});assert.equal(get('archive-artworks').hidden,true);assert.equal(get('archive-artworks-list').children.length,0);
  image.dispatch('error');assert.equal(get('archive-artworks').hidden,true);assert.equal(panel.showSources(event([art()])),false);
});

test('record text remains literal and unsafe image URLs never become image requests',()=>{
  const {panel,get}=setup();panel.apply({enabled:true,collection_ids:['national-gallery-of-art']});panel.setTurn(1);
  const title='<img src=x onerror=bad()>',item=art(undefined,{title,image_url:'javascript:alert(1)',excerpt:'<script>literal text</script>'});
  panel.showSources(event([item]));assert.equal(get('archive-artworks').hidden,true);assert.match(content(get('archive-sources-list')),/<script>literal text<\/script>/);
  assert.equal(get('archive-sources-list').querySelectorAll('img').length,0);
});

test('older servers can retain the complete QTM workflow when the collections route is unavailable',async()=>{
  const {panel,get}=setup(path=>path==='/api/archive/collections'?{status:404}:{entries:[]});
  assert.equal(await panel.loadCollections(),false);assert.equal(get('archive-collection-tools').hidden,true);
  assert.equal(await panel.load(),true);assert.equal(panel.open(),true);assert.equal(Object.hasOwn(panel.settings(),'collection_ids'),false);
});

test('collection choices cannot exceed eight or leave an empty accidental selection',async()=>{
  const extras=Array.from({length:9},(_,index)=>({id:'collection-'+index,label:'Collection '+index,count:1,source_kind:'custom'}));
  const {panel,get}=setup(path=>path==='/api/archive/collections'?{collections:[...collections,...extras]}:page([]));await panel.loadCollections();
  let checkbox=get('archive-collections').querySelectorAll('input')[0];checkbox.checked=false;checkbox.dispatch('change');
  assert.equal(checkbox.checked,true);assert.deepEqual(panel.collectionIds,[]);
  panel.apply({enabled:true,collection_ids:extras.slice(0,8).map(value=>value.id)});await settle();
  checkbox=get('archive-collections').querySelectorAll('input').at(-1);assert.equal(checkbox.disabled,true);
  checkbox.checked=true;checkbox.dispatch('change');assert.equal(checkbox.checked,false);assert.equal(panel.collectionIds.length,8);
});

test('a page from an old collection cannot replace the next character collection after its request finishes',async()=>{
  let finish;const {panel,get}=setup(()=>new Promise(resolve=>{finish=resolve;}));
  panel.apply({enabled:true,collection_ids:['national-gallery-of-art']});const loading=panel.load();
  panel.apply({enabled:true,collection_ids:['local-art']});panel.setBusy(true);finish(page([art()]));
  assert.equal(await loading,false);assert.deepEqual(panel.collectionIds,['local-art']);assert.deepEqual(panel.entries,[]);
  assert.equal(get('archive-artworks').hidden,true);
});

test('source metadata must belong to the chosen collection even when the event names the correct collection list',()=>{
  const {panel,get}=setup();panel.apply({enabled:true,collection_ids:['national-gallery-of-art']});panel.setTurn(1);
  panel.showSources(event([art('f'.repeat(32),{collection_id:'other-collection'})]));
  assert.equal(get('archive-sources-list').children.length,0);assert.equal(get('archive-artworks').hidden,true);
  panel.apply({enabled:true,scope:'library',entry_ids:[]});panel.setTurn(1);
  panel.showSources({...event([art()]),collection_ids:[]});
  assert.equal(get('archive-sources-list').children.length,0);assert.equal(get('archive-artworks').hidden,true);
});

test('changing a character collection clears the previous database search before loading its first page',async()=>{
  const {panel,get,calls}=setup(path=>path==='/api/archive/collections'?{collections}:page([]));
  await panel.loadCollections();panel.apply({enabled:true,collection_ids:['national-gallery-of-art']});await panel.load({query:'previous database query'});
  panel.apply({enabled:true,collection_ids:['local-art']});assert.equal(get('archive-search').value,'');await settle();
  const request=new URL(calls.at(-1).path,'http://test');assert.equal(request.searchParams.get('ids'),'local-art');assert.equal(request.searchParams.get('q'),'');
});
