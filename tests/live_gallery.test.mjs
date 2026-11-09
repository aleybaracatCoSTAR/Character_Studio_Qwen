import test from 'node:test';
import assert from 'node:assert/strict';
import {ArchiveGallery,galleryRecords} from '../frontend/live-gallery.mjs';

// A small moving-node DOM: innerHTML is deliberately unavailable. Mounting the
// gallery must retain the existing forms, conversation and their listeners.
class Node{
  constructor(tag,document){this.tagName=tag.toUpperCase();this.ownerDocument=document;this.children=[];this.parentNode=null;this.listeners=new Map();this.attributes=new Map();this.hidden=false;this.disabled=false;this.className='';this._text='';this.value='';this.style={};this.complete=false;this.naturalWidth=0;this.naturalHeight=0;
    this.classList={add:name=>{this.className=[...new Set([...this.className.split(/\s+/).filter(Boolean),name])].join(' ');},contains:name=>this.className.split(/\s+/).includes(name)};
  }
  set id(value){this._id=value;this.ownerDocument.ids.set(value,this);}get id(){return this._id;}
  set textContent(value){this.replaceChildren();this._text=String(value);}get textContent(){return this._text+this.children.map(child=>child.textContent).join('');}
  set innerHTML(value){throw new Error('Archive metadata must remain literal text.');}
  append(...nodes){for(const node of nodes){node.remove();node.parentNode=this;this.children.push(node);}}
  replaceChildren(...nodes){for(const node of [...this.children])node.remove();this._text='';this.append(...nodes);}
  remove(){if(this.parentNode){const parent=this.parentNode;parent.children=parent.children.filter(child=>child!==this);this.parentNode=null;}}
  after(node){const parent=this.parentNode,index=parent.children.indexOf(this);node.remove();node.parentNode=parent;parent.children.splice(index+1,0,node);}
  setAttribute(name,value){this.attributes.set(name,String(value));}
  getAttribute(name){return this.attributes.get(name)??null;}
  focus(options){this.ownerDocument.activeElement=this;this.focusOptions=options;}
  removeAttribute(name){this.attributes.delete(name);if(name==='src')this.src='';}
  addEventListener(name,callback){if(!this.listeners.has(name))this.listeners.set(name,new Set());this.listeners.get(name).add(callback);}
  removeEventListener(name,callback){this.listeners.get(name)?.delete(callback);}
  emit(name){for(const callback of [...this.listeners.get(name)??[]])callback({target:this,preventDefault(){}});}
  querySelectorAll(selector){const matches=node=>selector[0]==='#'?node.id===selector.slice(1):selector[0]==='.'?node.classList.contains(selector.slice(1)):node.tagName===selector.toUpperCase();return this.children.flatMap(child=>[...(matches(child)?[child]:[]),...child.querySelectorAll(selector)]);}
  querySelector(selector){return this.querySelectorAll(selector)[0]??null;}
  showModal(){this.open=true;}close(){this.open=false;}
}

function documentFixture(){
  const document={ids:new Map(),createElement(tag){return new Node(tag,this);},getElementById(id){return this.ids.get(id);},querySelector(selector){if(selector==='body > header')return this.body.querySelector('header');return this.body.querySelector(selector);}};
  document.body=document.createElement('body');document.head=document.createElement('head');
  const make=(tag,id,className='')=>{const node=document.createElement(tag);if(id)node.id=id;node.className=className;return node;};
  const header=make('header'),status=make('span','status'),main=make('main'),aside=make('aside');header.append(status);
  const form=make('form','character-form'),name=make('input','character-name');name.value='Existing Robin';
  const voice=make('details','voice-settings'),preset=make('select','voice-preset'),emotion=make('select','voice-emotion');
  voice.append(preset,emotion);form.append(name,voice);aside.append(form);
  const conversation=make('section',null,'conversation'),heading=make('div',null,'conversation-heading'),tools=make('div',null,'conversation-tools');
  const record=make('button','record'),typed=make('form','typed-form'),compare=make('button','open-comparison'),messages=make('div','messages'),empty=make('div','empty-state');
  tools.append(compare);heading.append(tools);messages.append(empty);conversation.append(heading,record,typed,messages);main.append(aside,conversation);document.body.append(header,main);
  return {document,nodes:{header,status,main,aside,form,name,conversation,heading,tools,record,typed,compare,messages}};
}

function setup({fetcher=async()=>{throw new Error('No implicit network request is allowed.');},collections=()=>['national-gallery-of-art'],onIntroduce=()=>false}={}){
  const {document,nodes}=documentFixture(),statuses=[];
  const reveal={shows:[],replays:0,clears:0,destroys:0,show(url,options){this.shows.push({url,options});const image=document.getElementById('gallery-image');if(image.src!==url){image.src=url;image.complete=false;image.naturalWidth=0;}return true;},replay(){this.replays++;},clear(){this.clears++;},destroy(){this.destroys++;}};
  const gallery=new ArchiveGallery({document,fetcher,collections,onIntroduce,onImageStatus:event=>statuses.push(event),revealFactory:()=>reveal});
  const image=document.getElementById('gallery-image');
  return {gallery,document,nodes,reveal,statuses,image,
    loaded(){image.complete=true;image.naturalWidth=800;image.naturalHeight=600;image.emit('load');},
    failed(){image.complete=true;image.naturalWidth=0;image.emit('error');}};
}
const entry=(id='a'.repeat(32),extra={})=>({id,collection_id:'national-gallery-of-art',collection_label:'National Gallery of Art',
  title:'Synthetic painting',source_url:'https://www.nga.gov/collection/art-object-page.123.html',
  image_url:'https://museum.example/artwork.jpg',image_alt:'Synthetic catalogue image',excerpt:'Test-only catalogue description.',
  metadata:{artist:'Fixture artist',date:'1901',medium:'Oil on canvas',rights:'Fixture rights'},...extra});
const source=(turn=1,extra={})=>({type:'archive_sources',turn_id:turn,image_request:true,...extra});
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const page=entries=>({entries,total:entries.length,matched:entries.length,offset:0,limit:18});

test('name entry queues one introduction until a live connection is ready',()=>{
  let ready=false;const names=[];
  const {gallery,document}=setup({onIntroduce:name=>{if(!ready)return false;names.push(name);return true;}});
  assert.equal(gallery.introduce(' Aley '),false);assert.equal(gallery.pendingName,'Aley');
  ready=true;assert.equal(gallery.flushIntroduction(),true);assert.deepEqual(names,['Aley']);
  assert.equal(gallery.flushIntroduction(),false);
  gallery.setVisitorName('Aley');
  assert.equal(document.getElementById('gallery-name-status').textContent,'Hello, Aley.');
  assert.equal(document.getElementById('gallery-visitor-name').value,'Aley');
  assert.equal(document.getElementById('character-name').value,'Existing Robin');
  assert.equal(gallery.introduce('<script>'),false);assert.deepEqual(names,['Aley']);
  document.getElementById('gallery-visitor-name').value='';gallery.greetAtStart();
  assert.equal(gallery.visitorName,'');assert.equal(gallery.pendingName,'');assert.deepEqual(names,['Aley']);
});

test('galleryRecords validates source URLs, IDs and collections while retaining safe metadata-only records',()=>{
  const records=galleryRecords([entry(),entry(),entry('bad id'),entry('b'.repeat(32),{collection_id:'../private'}),
    entry('c'.repeat(32),{source_url:'javascript:alert(1)'}),entry('d'.repeat(32),{source_url:'https://user:pass@museum.example/private'}),
    entry('e'.repeat(32),{image_url:'http://127.0.0.1/private'}),entry('f'.repeat(32),{image_url:'data:image/png,private'})]);
  assert.equal(records.length,3);assert.equal(records[0].image_url,entry().image_url);
  assert.equal(records[1].image_url,'');assert.equal(records[2].image_url,'');
  assert.equal(galleryRecords(null).length,0);
  assert.equal(galleryRecords(Array.from({length:50},(_,index)=>entry(String(index).padStart(32,'0')))).length,24);
});

test('mount retains the existing profile, text, microphone and comparison nodes and moves settings into a dialog',()=>{
  const {gallery,document,nodes,reveal}=setup();let recordClicks=0;nodes.record.addEventListener('click',()=>recordClicks++);
  assert.equal(document.title,'Art Archive Studio · Gallery');assert.equal(document.body.classList.contains('gallery-page'),true);
  assert.equal(document.getElementById('character-form'),nodes.form);assert.equal(nodes.name.value,'Existing Robin');
  assert.equal(nodes.aside.parentNode.id,'gallery-settings');assert.equal(nodes.typed.parentNode,nodes.conversation);
  assert.equal(nodes.tools.parentNode.className,'gallery-session-options');assert.equal(document.getElementById('open-comparison'),nodes.compare);
  assert.equal(document.getElementById('gallery-title').parentNode.parentNode.className,'gallery-artwork-layout');
  assert.equal(nodes.conversation.parentNode.className,'gallery-conversation');
  assert.equal(nodes.conversation.parentNode.querySelector('.gallery-details'),null);
  nodes.record.emit('click');assert.equal(recordClicks,1);
  const back=document.body.querySelector('.gallery-back');assert.equal(back.href,'/archive/');
  document.getElementById('gallery-open-settings').emit('click');assert.equal(document.getElementById('gallery-settings').open,true);
  document.getElementById('gallery-replay').emit('click');assert.equal(reveal.replays,1);
  assert.equal(gallery.records.length,0);assert.equal(nodes.messages.parentNode,nodes.conversation);
});

test('gallery voice button opens the same form controls directly and character settings restore the full view',()=>{
  const {gallery,document,nodes}=setup();
  const preset=document.getElementById('voice-preset'),emotion=document.getElementById('voice-emotion');
  document.getElementById('gallery-open-voice').emit('click');
  const dialog=document.getElementById('gallery-settings');
  assert.equal(dialog.open,true);assert.equal(dialog.getAttribute('data-settings-view'),'voice');
  assert.equal(document.getElementById('gallery-settings-title').textContent,'Voice & delivery');
  assert.equal(document.getElementById('voice-settings').open,true);assert.equal(document.activeElement,preset);
  assert.equal(preset.parentNode.parentNode,nodes.form);
  dialog.close();preset.disabled=true;gallery.openVoiceSettings();assert.equal(document.activeElement,emotion);
  document.getElementById('gallery-open-settings').emit('click');
  assert.equal(dialog.getAttribute('data-settings-view'),'character');
  assert.equal(document.getElementById('gallery-settings-title').textContent,'Character & collections');
  assert.equal(document.getElementById('voice-preset'),preset);
  const brand=document.body.querySelector('.gallery-header-title');assert.equal(brand.textContent,'Art Archive Studio');assert.equal(brand.href,'/archive/');
});

test('retrieval text remains literal and bounded, with source navigation only to a vetted public URL',()=>{
  const {gallery,document}=setup();gallery.setTurn(1);
  const title='<img src=x onerror=private()>',description='<script>literal source</script>';
  gallery.accept(source(),[entry('a'.repeat(32),{title,excerpt:description,metadata:{artist:'<b>artist</b>',rights:'R'.repeat(2000)}})]);
  assert.equal(document.getElementById('gallery-title').textContent,title);
  assert.equal(document.getElementById('gallery-description').textContent,description);
  assert.match(document.getElementById('gallery-meta').textContent,/<b>artist<\/b>/);
  assert.equal(document.getElementById('gallery-rights').textContent.length,1000);
  const link=document.getElementById('gallery-source');assert.equal(link.href,entry().source_url);assert.equal(link.target,'_blank');assert.equal(link.rel,'noopener noreferrer');
});

test('stale or disconnected retrievals cannot change the current artwork or trigger image loading',()=>{
  const {gallery,reveal,document}=setup();gallery.setTurn(3);
  for(const event of [source(2),source(4),source(0),{...source(3),type:'text_done'}])assert.equal(gallery.accept(event,[entry()]),false);
  assert.equal(reveal.shows.length,0);gallery.accept(source(3),[entry()]);assert.equal(reveal.shows.length,1);
  gallery.setTurn(2);assert.equal(gallery.turnId,3);gallery.disconnect();
  assert.equal(gallery.accept(source(3),[entry('b'.repeat(32))]),false);
  assert.equal(document.getElementById('gallery-title').textContent,entry().title);
});

test('repeated retrieval of an already loaded artwork does not replay or fetch another image',()=>{
  const {gallery,reveal,loaded}=setup();gallery.setTurn(1);gallery.accept(source(),[entry()]);loaded();
  gallery.accept(source(),[entry()]);assert.equal(reveal.shows.length,1);assert.equal(gallery.records.length,1);
  gallery.setTurn(2);gallery.accept(source(2),[entry()]);assert.equal(reveal.shows.length,1);
});

test('duplicate retrieval while an image is loading keeps the existing reveal request',()=>{
  const {gallery,reveal}=setup();gallery.setTurn(1);gallery.accept(source(),[entry()]);
  gallery.accept(source(),[entry()]);assert.equal(reveal.shows.length,1);
});

test('a different record using the cached same image URL reports loaded without requiring another browser load event',()=>{
  const {gallery,loaded,statuses,document}=setup();gallery.setTurn(1);gallery.accept(source(),[entry()]);loaded();
  gallery.setTurn(2);gallery.accept(source(2),[entry('b'.repeat(32),{title:'Different catalogue record sharing this image'})]);
  assert.equal(document.getElementById('gallery-title').textContent,'Different catalogue record sharing this image');
  assert.deepEqual(statuses.at(-1),{type:'image_status',turn_id:2,status:'loaded',loaded_count:1,total_count:1});
  assert.equal(document.getElementById('gallery-replay').disabled,false);
});

test('metadata-only explicit requests accurately report unavailable zero images and retain their museum source',()=>{
  const {gallery,statuses,document,reveal}=setup();gallery.setTurn(1);
  gallery.accept(source(),[entry('a'.repeat(32),{image_url:null,title:'An artwork with no imported image'})]);
  assert.deepEqual(statuses,[{type:'image_status',turn_id:1,status:'unavailable',loaded_count:0,total_count:0}]);
  assert.equal(reveal.shows.length,0);assert.equal(document.getElementById('gallery-source').href,entry().source_url);
  assert.match(document.getElementById('gallery-empty').textContent,/No image link/);
});

test('failure of the displayed artwork resolves unavailable even when other retrieved thumbnails exist',()=>{
  const {gallery,statuses,document,reveal,failed,loaded}=setup();gallery.setTurn(1);
  gallery.accept(source(),[entry(),entry('b'.repeat(32),{image_url:'https://museum.example/second.jpg'}),
    entry('c'.repeat(32),{image_url:'https://museum.example/third.jpg'})]);
  assert.equal(document.getElementById('gallery-thumbnails').children.length,3);
  assert.deepEqual(statuses.at(-1),{type:'image_status',turn_id:1,status:'loading',loaded_count:0,total_count:1});
  assert.equal(reveal.shows.length,1);failed();
  assert.deepEqual(statuses.at(-1),{type:'image_status',turn_id:1,status:'unavailable',loaded_count:0,total_count:1});
  document.getElementById('gallery-thumbnails').children[1].emit('click');
  assert.deepEqual(statuses.at(-1),{type:'image_status',turn_id:1,status:'loading',loaded_count:0,total_count:1});
  loaded();assert.deepEqual(statuses.at(-1),{type:'image_status',turn_id:1,status:'loaded',loaded_count:1,total_count:1});
});

test('selecting a keyboard-focused thumbnail restores the matching button and rail scroll after rendering',()=>{
  const {gallery,document,nodes}=setup();gallery.setTurn(1);gallery.accept(source(),[entry(),entry('b'.repeat(32),{image_url:'https://museum.example/second.jpg'})]);
  const rail=document.getElementById('gallery-thumbnails'),previous=rail.children[1];rail.scrollTop=77;rail.scrollLeft=12;previous.focus();
  const focusedKey=previous.getAttribute('data-gallery-key');previous.emit('click');
  assert.notEqual(document.activeElement,previous);assert.equal(document.activeElement.parentNode,rail);
  assert.equal(document.activeElement.getAttribute('data-gallery-key'),focusedKey);
  assert.equal(document.activeElement.getAttribute('aria-pressed'),'true');
  assert.deepEqual(document.activeElement.focusOptions,{preventScroll:true});
  assert.equal(rail.scrollTop,77);assert.equal(rail.scrollLeft,12);
  nodes.record.focus();gallery.renderThumbs();assert.equal(document.activeElement,nodes.record);
});

test('a new explicit image request retries a transient failure once instead of keeping the failed cache forever',()=>{
  for(const repeat of [entry(),entry('b'.repeat(32),{title:'Another record sharing the failed image URL'})]){
    const {gallery,failed,loaded,statuses,document,reveal}=setup();gallery.setTurn(1);gallery.accept(source(),[entry()]);failed();
    assert.equal(statuses.at(-1).status,'unavailable');gallery.setTurn(2);gallery.accept(source(2),[repeat]);
    assert.equal(reveal.shows.length,2);
    assert.deepEqual(statuses.at(-1),{type:'image_status',turn_id:2,status:'loading',loaded_count:0,total_count:1});
    assert.equal(document.getElementById('gallery-empty').hidden,true);
    gallery.accept(source(2),[repeat]);assert.equal(reveal.shows.length,2);
    loaded();assert.equal(statuses.at(-1).status,'loaded');assert.equal(document.getElementById('gallery-image').parentNode.hidden,false);
  }
});

test('a failed retry does not spin on duplicate source events but a later explicit request can retry again',()=>{
  const {gallery,failed,statuses,reveal}=setup();gallery.setTurn(1);gallery.accept(source(),[entry()]);failed();
  gallery.setTurn(2);gallery.accept(source(2),[entry()]);failed();assert.equal(reveal.shows.length,2);
  gallery.accept(source(2),[entry()]);assert.equal(reveal.shows.length,2);assert.equal(statuses.at(-1).status,'unavailable');
  gallery.setTurn(3);gallery.accept(source(3,{image_request:false,gallery_display:true}),[entry()]);
  assert.equal(reveal.shows.length,2);assert.equal(statuses.at(-1).status,'unavailable');
  gallery.setTurn(4);gallery.accept(source(4),[entry()]);assert.equal(reveal.shows.length,3);
});

test('a load event from the previous selected source cannot mark a different URL as loaded',()=>{
  const {gallery,statuses,image,loaded,document}=setup();gallery.setTurn(1);gallery.accept(source(),[entry()]);loaded();
  const oldURL=image.src;gallery.setTurn(2);
  gallery.accept(source(2),[entry('b'.repeat(32),{image_url:'https://museum.example/second.jpg'})]);
  image.currentSrc=oldURL;const before=statuses.length;loaded();assert.equal(statuses.length,before);
  assert.equal(document.getElementById('gallery-replay').disabled,true);
  image.currentSrc=image.src;loaded();assert.equal(statuses.at(-1).status,'loaded');assert.equal(statuses.at(-1).turn_id,2);
});

test('a late error after a settled load cannot hide the displayed artwork or report it unavailable',()=>{
  const {gallery,statuses,image,loaded,document}=setup();gallery.setTurn(1);gallery.accept(source(),[entry()]);loaded();
  const before=statuses.length;image.emit('error');assert.equal(statuses.length,before);
  assert.equal(image.parentNode.hidden,false);assert.equal(document.getElementById('gallery-empty').hidden,true);
  assert.equal(document.getElementById('gallery-replay').disabled,false);
});

test('only current explicit image requests publish display statuses; browsing does not send hidden model turns',()=>{
  const fetched=[];const {gallery,statuses,loaded,document}=setup({fetcher:(...args)=>{fetched.push(args);throw new Error('Unexpected fetch');}});
  gallery.setTurn(1);gallery.accept(source(1,{image_request:false}),[entry()]);loaded();assert.deepEqual(statuses,[]);
  gallery.setTurn(2);gallery.accept(source(2),[entry()]);
  assert.deepEqual(statuses,[{type:'image_status',turn_id:2,status:'loaded',loaded_count:1,total_count:1}]);
  document.getElementById('gallery-thumbnails').children[0].emit('click');assert.equal(fetched.length,0);
  assert.ok(statuses.every(value=>Object.keys(value).join(',')==='type,turn_id,status,loaded_count,total_count'));
  gallery.disconnect();const before=statuses.length;loaded();assert.equal(statuses.length,before);
});

test('late image callbacks from a reset cannot impersonate turn one of a new call',()=>{
  const {gallery,image,statuses,loaded}=setup();gallery.setTurn(1);gallery.accept(source(),[entry()]);
  const stale=[...image.listeners.get('load')][0];gallery.reset();gallery.setTurn(1);gallery.accept(source(),[entry('b'.repeat(32))]);
  const before=statuses.length;stale();assert.equal(statuses.length,before);loaded();assert.equal(statuses.length,before+1);assert.equal(statuses.at(-1).status,'loaded');
});

test('reset clears a previous unavailable artwork as well as visible metadata and source links',()=>{
  const {gallery,document}=setup();gallery.setTurn(1);gallery.accept(source(),[entry('a'.repeat(32),{title:'PRIVATE OLD PROFILE ARTWORK',image_url:null})]);
  assert.match(document.getElementById('gallery-empty').textContent,/PRIVATE OLD PROFILE ARTWORK/);
  gallery.reset();assert.doesNotMatch(document.getElementById('gallery-empty').textContent,/PRIVATE OLD PROFILE ARTWORK/);
  assert.equal(document.getElementById('gallery-source').hidden,true);assert.equal(document.getElementById('gallery-title').textContent,'A closer look');
  assert.equal(gallery.records.length,0);assert.equal(document.getElementById('gallery-replay').disabled,true);
});

test('search uses the actual paginated entries contract and only the read-only local collections endpoint',async()=>{
  const calls=[];const {gallery,document,reveal,statuses}=setup({collections:()=>['national-gallery-of-art','qtm','national-gallery-of-art','../unsafe'],
    fetcher:async(url,options)=>{calls.push({url,options});return {ok:true,json:async()=>page([entry()])};}});
  assert.equal(await gallery.search('Monet & water lilies'),true);assert.equal(calls.length,1);
  const url=new URL(calls[0].url,'http://local.test');assert.equal(url.pathname,'/api/archive/collections/entries');
  assert.equal(url.searchParams.get('q'),'Monet & water lilies');assert.equal(url.searchParams.get('limit'),'18');
  assert.equal(url.searchParams.get('search_mode'),'literal');
  assert.equal(url.searchParams.get('ids'),'national-gallery-of-art,qtm');
  assert.equal(calls[0].options.signal instanceof AbortSignal,true);assert.match(document.getElementById('gallery-search-status').textContent,/1 catalogue matches/);
  assert.equal(reveal.shows.length,1);assert.deepEqual(statuses,[]);
});

test('new retrieval wins over a slow search response and its cancellation notice',async()=>{
  const response=deferred();let signal;
  const {gallery,document}=setup({fetcher:async(url,options)=>{signal=options.signal;return response.promise;}});
  gallery.setTurn(1);const pending=gallery.search('old search');assert.equal(signal.aborted,false);
  gallery.accept(source(),[entry('b'.repeat(32),{title:'New retrieved artwork'})]);assert.equal(signal.aborted,true);
  response.resolve({ok:true,json:async()=>page([entry('a'.repeat(32),{title:'Stale search artwork'})])});
  assert.equal(await pending,false);assert.equal(document.getElementById('gallery-title').textContent,'New retrieved artwork');
  assert.equal(gallery.records.some(item=>item.title==='Stale search artwork'),false);
  assert.doesNotMatch(document.getElementById('gallery-search-status').textContent,/could not be searched/);
});

test('reset invalidates a response whose JSON body is still pending without populating the new profile',async()=>{
  const body=deferred();let signal;
  const {gallery,document}=setup({fetcher:async(url,options)=>{signal=options.signal;return {ok:true,json:()=>body.promise};}});
  const pending=gallery.search('private prior topic');await Promise.resolve();gallery.reset();assert.equal(signal.aborted,true);
  body.resolve(page([entry('a'.repeat(32),{title:'Stale private result'})]));assert.equal(await pending,false);
  assert.equal(gallery.records.length,0);assert.equal(document.getElementById('gallery-title').textContent,'A closer look');
  assert.equal(document.getElementById('gallery-search-status').textContent,'');
});

test('latest manual search wins and destroying the view prevents late mutations',async()=>{
  const first=deferred(),second=deferred();let calls=0;
  const {gallery,document,reveal}=setup({fetcher:()=>++calls===1?first.promise:second.promise});
  const older=gallery.search('first'),newer=gallery.search('second');second.resolve({ok:true,json:async()=>page([entry('b'.repeat(32),{title:'Second'})])});
  assert.equal(await newer,true);first.resolve({ok:true,json:async()=>page([entry('a'.repeat(32),{title:'First'})])});assert.equal(await older,false);
  assert.equal(document.getElementById('gallery-title').textContent,'Second');
  gallery.destroy();assert.equal(reveal.destroys,1);gallery.setTurn(1);assert.equal(gallery.select(gallery.records[0]),false);
});

test('missing collections, empty results and failed search do not invent an artwork or call a model',async()=>{
  let calls=0;const none=setup({collections:()=>[],fetcher:async()=>{calls++;throw new Error('No request expected');}});
  assert.equal(await none.gallery.search('Monet'),false);assert.equal(calls,0);assert.match(none.document.getElementById('gallery-search-status').textContent,/No saved art collections/);
  const empty=setup({fetcher:async()=>({ok:true,json:async()=>page([])})});assert.equal(await empty.gallery.search('Missing'),true);assert.equal(empty.gallery.records.length,0);
  const failed=setup({fetcher:async()=>({ok:false,status:503})});assert.equal(await failed.gallery.search('Monet'),false);
  assert.match(failed.document.getElementById('gallery-search-status').textContent,/could not be searched/);assert.equal(failed.reveal.shows.length,0);
});

test('gallery selection feedback names the central artwork among several retrieved candidates',()=>{
  const {gallery,loaded,statuses,document}=setup();gallery.setTurn(1);
  const records=[entry('a'.repeat(32),{title:'The Expulsion from Eden'}),
    entry('b'.repeat(32),{title:'Musicians (Musikanten)',image_url:'https://museum.example/woodcut.jpg'})];
  gallery.accept(source(1,{image_request:false,gallery_display:true}),records);loaded();
  assert.deepEqual(statuses.at(-1),{type:'image_status',turn_id:1,status:'loaded',loaded_count:1,total_count:1,
    selected:{collection_id:'national-gallery-of-art',id:'a'.repeat(32)}});
  document.getElementById('gallery-thumbnails').children[1].emit('click');loaded();
  assert.deepEqual(statuses.at(-1).selected,{collection_id:'national-gallery-of-art',id:'b'.repeat(32)});
  assert.equal(document.getElementById('gallery-title').textContent,'Musicians (Musikanten)');
});

test('a resolved woodcut followup replaces Eden with the selected woodcut and reports that exact record',()=>{
  const {gallery,loaded,statuses,document,reveal}=setup();gallery.setTurn(1);
  gallery.accept(source(1,{gallery_display:true}),[entry('a'.repeat(32),{title:'The Expulsion from Eden'})]);loaded();
  gallery.setTurn(2);
  const woodcut=entry('b'.repeat(32),{title:'Musicians (Musikanten)',image_url:'https://museum.example/musicians.jpg',metadata:{medium:'woodcut'}});
  gallery.accept(source(2,{gallery_display:true}),[woodcut]);loaded();
  assert.equal(document.getElementById('gallery-title').textContent,'Musicians (Musikanten)');
  assert.equal(reveal.shows.at(-1).url,'https://museum.example/musicians.jpg');
  assert.deepEqual(statuses.at(-1).selected,{collection_id:woodcut.collection_id,id:woodcut.id});
  assert.equal(document.getElementById('gallery-thumbnails').children.length,2);
});

test('an unmatched requested selection clears unrelated central art while retaining visit thumbnails',()=>{
  const {gallery,loaded,statuses,document,reveal,image}=setup();gallery.setTurn(1);
  gallery.accept(source(),[entry('a'.repeat(32),{title:'The Expulsion from Eden'})]);loaded();
  const oldCallback=[...image.listeners.get('load')][0];
  gallery.setTurn(2);const before=statuses.length;
  assert.equal(gallery.accept(source(2),[]),true);
  assert.equal(gallery.selected,null);assert.equal(image.parentNode.hidden,true);
  assert.equal(document.getElementById('gallery-title').textContent,'No artwork selected');
  assert.equal(document.getElementById('gallery-source').hidden,true);
  assert.equal(document.getElementById('gallery-thumbnails').children.length,1);
  assert.match(document.getElementById('gallery-empty').textContent,/No matching artwork/);
  oldCallback();assert.equal(statuses.length,before);assert.equal(reveal.clears,1);
});

test('same candidates in a new rank preserve the server-confirmed central artwork identity',()=>{
  const {gallery,loaded,document,reveal}=setup();gallery.setTurn(1);
  const first=entry('a'.repeat(32),{title:'The Expulsion from Eden'}),second=entry('b'.repeat(32),{title:'Musicians (Musikanten)',image_url:'https://museum.example/woodcut.jpg'});
  gallery.accept(source(1,{gallery_display:true}),[first,second]);loaded();
  document.getElementById('gallery-thumbnails').children[1].emit('click');loaded();
  const before=reveal.shows.length;gallery.setTurn(2);
  gallery.accept(source(2,{gallery_display:true,gallery_selected:{collection_id:second.collection_id,id:second.id}}),[first,second]);
  assert.equal(document.getElementById('gallery-title').textContent,second.title);
  assert.equal(reveal.shows.length,before);
  // An injected selection outside these records never opens an arbitrary URL.
  gallery.setTurn(3);gallery.accept(source(3,{gallery_display:true,gallery_selected:{collection_id:'other',id:'private'}}),[first,second]);
  assert.equal(document.getElementById('gallery-title').textContent,second.title);
});


test('a selected metadata-only record reports its own missing link without marking other candidate images unavailable',()=>{
  const {gallery,statuses,document}=setup();gallery.setTurn(1);
  const available=entry(),missing=entry('b'.repeat(32),{title:'Record without an image',image_url:null});
  gallery.accept(source(1,{gallery_display:true,gallery_selected:{collection_id:missing.collection_id,id:missing.id}}),[available,missing]);
  assert.deepEqual(statuses,[{type:'image_status',turn_id:1,status:'unavailable',loaded_count:0,total_count:0,
    selected:{collection_id:missing.collection_id,id:missing.id}}]);
  assert.equal(document.getElementById('gallery-title').textContent,missing.title);
  assert.equal(document.getElementById('gallery-image').parentNode.hidden,true);
});

test('all metadata-only gallery results send a single identity-specific zero-image status',()=>{
  const {gallery,statuses}=setup();gallery.setTurn(1);
  const first=entry('a'.repeat(32),{image_url:null}),selected=entry('b'.repeat(32),{image_url:null});
  gallery.accept(source(1,{gallery_display:true,gallery_selected:{collection_id:selected.collection_id,id:selected.id}}),[first,selected]);
  assert.deepEqual(statuses,[{type:'image_status',turn_id:1,status:'unavailable',loaded_count:0,total_count:0,
    selected:{collection_id:selected.collection_id,id:selected.id}}]);
});
