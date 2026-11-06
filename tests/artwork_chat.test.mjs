import test from 'node:test';
import assert from 'node:assert/strict';
import {ArtworkChat,artworkImageViewURL} from '../frontend/live-artwork.mjs';
import {ArchivePanel} from '../frontend/live-archive.mjs';

test('Open image uses a larger official NGA view while keeping vetted museum relays and rejecting unsafe URLs',()=>{
  const id='abec1c5b-ee32-450d-9398-5c84f1f8cf14';
  assert.equal(artworkImageViewURL(`https://api.nga.gov/iiif/${id}/full/!200,200/0/default.jpg`),`https://api.nga.gov/iiif/${id}/full/!1200,1200/0/default.jpg`);
  assert.equal(artworkImageViewURL(`https://www.artic.edu/iiif/2/${id}/full/400,/0/default.jpg`),`/api/archive/museum-images/aic/${id}`);
  assert.equal(artworkImageViewURL('https://museum.example/image.jpg'),'https://museum.example/image.jpg');
  for(const url of ['javascript:alert(1)','http://127.0.0.1/image.jpg','https://user:pass@museum.example/image.jpg'])assert.equal(artworkImageViewURL(url),null);
});

function element(tag='div'){
  return {tagName:tag.toUpperCase(),children:[],listeners:new Map(),attributes:new Map(),hidden:false,disabled:false,open:false,checked:false,value:'',textContent:'',
    set innerHTML(_){throw new Error('Archive image text must never become HTML.');},
    append(...children){for(const child of children){child.parent=this;this.children.push(child);}},replaceChildren(...children){for(const child of this.children)child.parent=null;this.children=[];this.append(...children);},
    remove(){if(this.parent){this.parent.children=this.parent.children.filter(child=>child!==this);this.parent=null;}},
    setAttribute(key,value){this.attributes.set(key,value);},addEventListener(type,handler){this.listeners.set(type,handler);},
    dispatch(type){return this.listeners.get(type)?.({target:this,preventDefault(){}});},
    querySelectorAll(selector){const tags=selector.split(',').map(value=>value.trim().toUpperCase());return this.children.flatMap(child=>[...(tags.includes(child.tagName)?[child]:[]),...child.querySelectorAll(selector)]);},
    showModal(){this.open=true;},close(){this.open=false;this.dispatch('close');}};
}
const content=node=>[node.textContent,...node.children.map(content)].join('\n');
function setup({onImageStatus}={}){
  const nodes=new Map(),document={getElementById:id=>{if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);},createElement:element};
  const container=element(),empty=element();container.append(empty);
  const statuses=[];const chat=new ArtworkChat({document,container,empty,onImageStatus:value=>{statuses.push(value);onImageStatus?.(value,container);}});return {chat,document,container,empty,statuses};
}
const entry=(id='a'.repeat(32),extra={})=>({id,title:'Fixture artwork',collection_id:'national-gallery-of-art',collection_label:'National Gallery of Art',
  source_kind:'nga',source_url:'https://www.nga.gov/artworks/fixture',image_url:'https://api.nga.gov/iiif/fixture/full/!400,400/0/default.jpg',
  image_alt:'An artwork from the synthetic test fixture.',excerpt:'Test-only catalogue metadata.',metadata:{artist:'Fixture artist',date:'1901',medium:'Oil on canvas',rights:'Test catalogue rights'},...extra});
const event=(turn=1,extra={})=>({type:'archive_sources',turn_id:turn,image_request:true,request_id:'image-request-'+turn,...extra});

test('only an explicit current-turn image request appends conversation cards',()=>{
  const {chat,container,empty}=setup();chat.setTurn(1);
  for(const message of [event(1,{image_request:false}),event(2),event(0),{...event(),type:'text_done'}])assert.equal(chat.accept(message,[entry()]),false);
  assert.equal(container.children.length,1);assert.equal(empty.hidden,false);
  assert.equal(chat.accept(event(),[entry()]),true);assert.equal(container.children.length,2);assert.equal(empty.hidden,true);
  assert.match(content(container),/Fixture artwork|Fixture artist · 1901 · Oil on canvas/);
  assert.equal(container.children[1].attributes.get('data-turn-id'),'1');
});

test('the same turn and request are deduplicated without rebuilding cards or reloading images',()=>{
  const {chat,container}=setup();chat.setTurn(1);assert.equal(chat.accept(event(),[entry()]),true);
  const node=container.children[1],image=node.querySelectorAll('img')[0];
  assert.equal(chat.accept(event(),[entry('b'.repeat(32))]),false);
  assert.equal(container.children.length,2);assert.equal(container.children[1],node);assert.equal(node.querySelectorAll('img')[0],image);
});

test('new turns preserve earlier requested images while stale sources and disconnected events cannot append',()=>{
  const {chat,container}=setup();chat.setTurn(1);chat.accept(event(),[entry()]);const original=container.children[1];
  chat.setTurn(2);assert.equal(chat.accept(event(1),[entry()]),false);assert.equal(chat.accept(event(2),[entry('b'.repeat(32))]),true);
  assert.equal(container.children.length,3);assert.equal(container.children[1],original);assert.equal(chat.setTurn(1),false);
  chat.disconnect();assert.equal(chat.accept(event(2,{request_id:'late'}),[entry()]),false);assert.equal(container.children.length,3);
});

test('a new-call reset removes only inline image records and clears request identities',()=>{
  const {chat,container,document}=setup();const text=document.createElement('div');text.textContent='Existing dialogue';container.append(text);
  chat.setTurn(1);chat.accept(event(),[entry()]);chat.reset();
  assert.equal(container.children.includes(text),true);assert.equal(container.children.length,2);
  chat.setTurn(1);assert.equal(chat.accept(event(),[entry()]),true);assert.equal(container.children.length,3);
});

test('multiple matches retain at most three distinct candidate cards and do not assert a unique choice',()=>{
  const {chat,container}=setup();chat.setTurn(1);
  const first=entry(),sameIDOtherCollection=entry(first.id,{collection_id:'local-art',collection_label:'Local collection'});
  const candidates=[first,first,sameIDOtherCollection,entry('b'.repeat(32)),entry('c'.repeat(32))];
  chat.accept(event(),candidates);assert.equal(container.querySelectorAll('article').length,3);assert.equal(container.querySelectorAll('img').length,3);
  assert.match(content(container),/These archive images match your request/);assert.match(content(container),/Local collection/);
});

test('an image request without an eligible image creates a readable conversation response instead of disappearing',()=>{
  const {chat,container}=setup();chat.setTurn(1);assert.equal(chat.accept(event(),[entry('b'.repeat(32),{image_url:''})]),true);
  assert.equal(container.querySelectorAll('img').length,0);assert.match(content(container),/No image link in this imported record/);
  assert.match(content(container),/Fixture artwork|Fixture artist/);assert.equal(container.querySelectorAll('a')[0].textContent,'Original source');
  chat.setTurn(2);chat.accept(event(2,{image_message:'There are several possible works. Please name an artist or title.'}),[]);
  assert.match(content(container),/Please name an artist or title/);
});

test('separate image views are explicit safe links with no automatic popup or navigation',()=>{
  const {chat,container}=setup();chat.setTurn(1);chat.accept(event(),[entry()]);
  const links=container.querySelectorAll('a');assert.deepEqual(links.map(link=>link.textContent),['Open image','Original source']);
  assert.equal(links[0].href,entry().image_url);assert.equal(links[1].href,entry().source_url);
  assert.ok(links.every(link=>link.target==='_blank'&&link.rel==='noopener noreferrer'));
});

test('canonical Art Institute images use the same local image endpoint for inline display and explicit open links',()=>{
  const {chat,container}=setup();chat.setTurn(1);const uuid='2d484387-2509-5e8e-2c43-22f9981972eb';
  chat.accept(event(),[entry('a'.repeat(32),{image_url:'https://www.artic.edu/iiif/2/'+uuid+'/full/400,/0/default.jpg'})]);
  const local='/api/archive/museum-images/aic/'+uuid;assert.equal(container.querySelectorAll('img')[0].src,local);assert.equal(container.querySelectorAll('a')[0].href,local);
});

test('unvalidated records, unsafe image/source URLs and arbitrary relative endpoints cannot create image links',()=>{
  const {chat,container}=setup();chat.setTurn(1);
  const candidates=[entry('bad id'),entry('b'.repeat(32),{collection_id:'../private'}),
    entry('c'.repeat(32),{image_url:'javascript:alert(1)'}),entry('d'.repeat(32),{image_url:'https://127.0.0.1/private'}),
    entry('e'.repeat(32),{source_url:'data:text/html,bad'}),entry('f'.repeat(32),{image_url:'/api/private/export'})];
  chat.accept(event(),candidates);assert.equal(container.querySelectorAll('img').length,0);
  assert.ok(container.querySelectorAll('a').every(link=>link.textContent==='Original source'&&link.href===entry().source_url));
  assert.equal(chat.accept(event(1,{request_id:'raw-only',matches:[entry()]})),false);
});

test('literal bounded captions and delivery failure preserve source access without executing text',()=>{
  const {chat,container}=setup();chat.setTurn(1);
  chat.accept(event(1,{image_message:'<script>literal notice</script>'}),[entry('a'.repeat(32),{title:'<img onerror=bad>',metadata:{artist:'<b>literal artist</b>',rights:'R'.repeat(2000)}})]);
  assert.match(content(container),/<script>literal notice<\/script>/);assert.match(content(container),/<img onerror=bad>|<b>literal artist<\/b>/);
  assert.equal(content(container).includes('R'.repeat(1001)),false);
  const image=container.querySelectorAll('img')[0];assert.equal(image.loading,'lazy');assert.equal(image.referrerPolicy,'no-referrer');
  image.dispatch('error');assert.equal(image.hidden,true);assert.match(content(container),/could not be loaded/);assert.equal(container.querySelectorAll('a').length,2);
});

test('ArchivePanel exposes only validated current source snapshots and clears them without removing inline conversation images',()=>{
  const {chat,document,container}=setup();const panel=new ArchivePanel({document,fetch:async()=>{throw new Error('No network in this fixture.');}});
  panel.apply({enabled:true,collection_ids:['national-gallery-of-art']});panel.setTurn(1);chat.setTurn(1);
  const good=entry(),wrong=entry('b'.repeat(32),{collection_id:'other-archive'}),unsafe=entry('c'.repeat(32),{source_url:'javascript:bad'});
  const sources={...event(),scope:'library',collection_ids:['national-gallery-of-art'],selected_count:3,matches:[good,wrong,unsafe]};
  assert.equal(panel.showSources(sources),true);const snapshot=panel.sourceMatches;assert.equal(snapshot.length,1);
  assert.equal(document.getElementById('archive-artworks').hidden,true);
  assert.equal(chat.accept(sources,snapshot),true);snapshot[0].metadata.artist='Changed outside';assert.equal(panel.sourceMatches[0].metadata.artist,'Fixture artist');
  panel.resetSources();assert.deepEqual(panel.sourceMatches,[]);assert.equal(container.querySelectorAll('img').length,1);
});

test('an explicit image card stays at its top while subsequent actor text grows',()=>{
  const {chat,container,document}=setup();
  const create=document.createElement;
  document.createElement=tag=>{const node=create(tag);if(tag==='article')node.offsetTop=280;return node;};
  container.scrollTop=0;container.scrollHeight=1600;chat.setTurn(1);
  assert.equal(chat.hasCurrent(),false);assert.equal(chat.accept(event(),[entry()]),true);
  assert.equal(chat.hasCurrent(),true);assert.equal(container.scrollTop,280);
  const reply=document.createElement('p');reply.textContent='A growing actor reply.';container.append(reply);
  container.scrollHeight=2300;container.scrollTop=2300;
  assert.equal(chat.scrollForReply(),true);assert.equal(container.scrollTop,280);
  assert.equal(chat.accept(event(),[entry('b'.repeat(32))]),false);
  assert.equal(chat.scrollForReply(),true);assert.equal(container.scrollTop,280);
});

test('image anchoring uses scroll-container geometry instead of a different offset parent',()=>{
  const {chat,container}=setup();chat.setTurn(1);chat.accept(event(),[entry()]);
  const card=container.querySelectorAll('article')[0];
  container.scrollTop=999;container.clientTop=2;container.getBoundingClientRect=()=>({top:100});
  card.offsetTop=9000;card.getBoundingClientRect=()=>({top:350-container.scrollTop+102});
  assert.equal(chat.scrollForReply(),true);assert.equal(container.scrollTop,350);
  container.scrollTop=1800;
  assert.equal(chat.scrollForReply(),true);assert.equal(container.scrollTop,350);
});

test('new turns and disconnection release the pin without removing earlier conversation images',()=>{
  const {chat,container}=setup();chat.setTurn(1);chat.accept(event(),[entry()]);
  const card=container.querySelectorAll('article')[0];card.offsetTop=100;
  assert.equal(chat.scrollForReply(),true);assert.equal(chat.setTurn(1),true);assert.equal(chat.hasCurrent(),true);
  chat.setTurn(2);container.scrollTop=420;
  assert.equal(chat.hasCurrent(),false);assert.equal(chat.scrollForReply(),false);assert.equal(container.scrollTop,420);
  assert.equal(container.querySelectorAll('img').length,1);
  assert.equal(chat.accept(event(1),[entry()]),false);assert.equal(chat.hasCurrent(),false);
  chat.accept(event(2),[entry('b'.repeat(32))]);container.querySelectorAll('article')[1].offsetTop=600;
  assert.equal(chat.scrollForReply(),true);chat.disconnect();container.scrollTop=900;
  assert.equal(chat.hasCurrent(),false);assert.equal(chat.scrollForReply(),false);assert.equal(container.scrollTop,900);
  assert.equal(container.querySelectorAll('img').length,2);
});

test('ordinary, empty and detached image responses never pin an unrelated message',()=>{
  const {chat,container}=setup();chat.setTurn(1);container.scrollTop=120;
  assert.equal(chat.scrollForReply(),false);assert.equal(container.scrollTop,120);
  chat.accept(event(),[entry('b'.repeat(32),{image_url:''})]);
  assert.equal(chat.hasCurrent(),false);container.scrollTop=180;
  assert.equal(chat.scrollForReply(),false);assert.equal(container.scrollTop,180);
  chat.setTurn(2);chat.accept(event(2),[entry()]);
  const card=container.querySelectorAll('article').at(-1);card.offsetTop=240;
  assert.equal(chat.hasCurrent(),true);card.remove();container.scrollTop=300;
  assert.equal(chat.hasCurrent(),false);assert.equal(chat.scrollForReply(),false);assert.equal(container.scrollTop,300);
  chat.reset();assert.equal(chat.hasCurrent(),false);
});

test('an image batch reports loading only after its cards enter the conversation, then actual browser loads',()=>{
  const {chat,container,statuses}=setup({onImageStatus:(value,node)=>{
    assert.equal(node.querySelectorAll('article').length,2);assert.equal(node.querySelectorAll('img').length,2);
  }});
  chat.setTurn(1);chat.accept(event(),[entry(),entry('b'.repeat(32))]);
  assert.deepEqual(statuses,[{type:'image_status',turn_id:1,status:'loading',loaded_count:0,total_count:2}]);
  const images=container.querySelectorAll('img');images[0].dispatch('load');
  assert.deepEqual(statuses.at(-1),{type:'image_status',turn_id:1,status:'loaded',loaded_count:1,total_count:2});
  images[1].dispatch('load');assert.equal(statuses.at(-1).loaded_count,2);assert.equal(statuses.length,3);
  images[0].dispatch('load');images[1].dispatch('load');assert.equal(statuses.length,3);
});

test('unavailable is reported only after every actual image fails, while mixed success remains loaded',()=>{
  const {chat,container,statuses}=setup();chat.setTurn(1);chat.accept(event(),[entry(),entry('b'.repeat(32))]);
  const images=container.querySelectorAll('img');images[0].dispatch('error');assert.equal(statuses.length,1);
  images[1].dispatch('error');assert.deepEqual(statuses.at(-1),{type:'image_status',turn_id:1,status:'unavailable',loaded_count:0,total_count:2});
  chat.setTurn(2);chat.accept(event(2),[entry(),entry('b'.repeat(32))]);const later=container.querySelectorAll('img').slice(2);
  later[0].dispatch('load');later[1].dispatch('error');
  assert.deepEqual(statuses.at(-1),{type:'image_status',turn_id:2,status:'loaded',loaded_count:1,total_count:2});
});

test('catalogue records without imported image links retain their titles and sources and report unavailable zero of zero',()=>{
  const {chat,container,statuses}=setup();chat.setTurn(1);
  const records=[entry('a'.repeat(32),{title:'Queer Birds',image_url:null,metadata:{artist:'Fixture artist',date:'1901'}}),
    entry('b'.repeat(32),{title:'Queer House',image_url:null}),entry('c'.repeat(32),{title:'Queer Fish',image_url:null})];
  chat.accept(event(),records);
  assert.equal(container.querySelectorAll('article').length,3);assert.equal(container.querySelectorAll('img').length,0);
  for(const title of ['Queer Birds','Queer House','Queer Fish'])assert.match(content(container),new RegExp(title));
  assert.equal(container.querySelectorAll('a').length,3);assert.ok(container.querySelectorAll('a').every(link=>link.textContent==='Original source'));
  assert.equal(chat.hasCurrent(),false);
  assert.deepEqual(statuses,[{type:'image_status',turn_id:1,status:'unavailable',loaded_count:0,total_count:0}]);
});

test('mixed metadata-only and image cards count only actual image URLs and pin only a real image card',()=>{
  const {chat,container,document,statuses}=setup();const create=document.createElement;
  let cardIndex=0;document.createElement=tag=>{const node=create(tag);if(tag==='article')node.offsetTop=100*(++cardIndex);return node;};
  chat.setTurn(1);chat.accept(event(),[entry('a'.repeat(32),{image_url:null}),entry('b'.repeat(32)),entry('c'.repeat(32),{image_url:null})]);
  assert.equal(container.querySelectorAll('article').length,3);assert.equal(container.querySelectorAll('img').length,1);
  assert.match(content(container),/include records without an image link/);assert.equal(container.scrollTop,200);
  assert.equal(statuses[0].total_count,1);container.querySelectorAll('img')[0].dispatch('load');assert.equal(statuses.at(-1).loaded_count,1);
  assert.equal(container.querySelectorAll('a').filter(link=>link.textContent==='Open image').length,1);
});

test('a later-turn repeated show request creates fresh cards instead of being filtered as a duplicate',()=>{
  const {chat,container,statuses}=setup();
  for(const turn of [1,2,3]){
    chat.setTurn(turn);assert.equal(chat.accept(event(turn,{request_id:'same-work'}),[entry()]),true);
    const image=container.querySelectorAll('img').at(-1);image.dispatch('load');assert.equal(statuses.at(-1).turn_id,turn);
  }
  assert.equal(container.querySelectorAll('img').length,3);assert.equal(statuses.length,6);
  assert.equal(chat.accept(event(3,{request_id:'same-work'}),[entry()]),false);assert.equal(statuses.length,6);
});

test('late image events after new turn, disconnect, reset or DOM removal never report stale display success',()=>{
  for(const cleanup of [chat=>chat.setTurn(2),chat=>chat.disconnect(),chat=>chat.reset(),
    (chat,container)=>container.children.at(-1).remove()]){
    const {chat,container,statuses}=setup();chat.setTurn(1);chat.accept(event(),[entry()]);const image=container.querySelectorAll('img')[0];
    cleanup(chat,container);image.dispatch('load');image.dispatch('error');assert.equal(statuses.length,1);
  }
});

test('reusing turn one after reset cannot let an old image load impersonate the new call',()=>{
  const {chat,container,statuses}=setup();chat.setTurn(1);chat.accept(event(),[entry()]);const old=container.querySelectorAll('img')[0];
  chat.reset();chat.setTurn(1);chat.accept(event(),[entry()]);const current=container.querySelectorAll('img')[0];
  old.dispatch('load');assert.equal(statuses.length,2);current.dispatch('load');assert.equal(statuses.length,3);assert.equal(statuses.at(-1).status,'loaded');
});

test('a newer image request on the same turn owns status while the earlier cards remain readable',()=>{
  const {chat,container,statuses}=setup();chat.setTurn(1);chat.accept(event(1,{request_id:'first'}),[entry()]);
  const previous=container.querySelectorAll('img')[0];chat.accept(event(1,{request_id:'second'}),[entry('b'.repeat(32))]);
  previous.dispatch('load');assert.equal(statuses.length,2);container.querySelectorAll('img')[1].dispatch('error');
  assert.equal(statuses.length,3);assert.equal(statuses.at(-1).status,'unavailable');assert.equal(container.querySelectorAll('article').length,2);
});

test('image status contains only bounded counters and fixed state, never source URLs, captions or raw errors',()=>{
  const {chat,container,statuses}=setup();chat.setTurn(1);
  chat.accept(event(),Array.from({length:40},(_,index)=>entry(String(index+1).padStart(32,'0'))));
  for(const image of container.querySelectorAll('img')){image.dispatch('load');image.dispatch('load');}
  assert.equal(statuses.at(-1).total_count,3);assert.equal(statuses.at(-1).loaded_count,3);
  for(const status of statuses){
    assert.deepEqual(Object.keys(status),['type','turn_id','status','loaded_count','total_count']);
    assert.ok(status.loaded_count>=0&&status.loaded_count<=status.total_count&&status.total_count<=3);
    assert.doesNotMatch(JSON.stringify(status),/https|Fixture|fixture|artist|PRIVATE/);
  }
});

test('a closed or throwing status callback cannot prevent safe images and separate Open image links',()=>{
  const {chat,container}=setup({onImageStatus:()=>{throw new Error('Closed connection');}});
  chat.setTurn(1);assert.equal(chat.accept(event(),[entry()]),true);assert.doesNotThrow(()=>container.querySelectorAll('img')[0].dispatch('load'));
  assert.equal(container.querySelectorAll('a')[0].textContent,'Open image');assert.equal(container.querySelectorAll('a')[0].target,'_blank');
});
