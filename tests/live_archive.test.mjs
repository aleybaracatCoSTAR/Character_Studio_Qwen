import test from 'node:test';
import assert from 'node:assert/strict';
import {ArchivePanel,archiveEntryId,archiveSourceURL} from '../frontend/live-archive.mjs';

function element(tag='div'){
  return {tagName:tag.toUpperCase(),children:[],listeners:new Map(),attributes:new Map(),hidden:false,disabled:false,open:false,checked:false,value:'',textContent:'',
    set innerHTML(value){throw new Error('Archive text must never become HTML.');},
    append(...children){this.children.push(...children);},replaceChildren(...children){this.children=[...children];},
    setAttribute(key,value){this.attributes.set(key,value);},addEventListener(type,listener){this.listeners.set(type,listener);},
    dispatch(type){return this.listeners.get(type)?.({target:this,preventDefault(){}});},
    querySelectorAll(selector){const tags=selector.split(',').map(value=>value.trim().toUpperCase());return this.children.flatMap(child=>[...(tags.includes(child.tagName)?[child]:[]),...child.querySelectorAll(selector)]);},
    showModal(){this.open=true;},close(){this.open=false;this.dispatch('close');}
  };
}
const entry=(id='qtm-123',extra={})=>({id,title:'Test story',text:'A test passage supplied only by the test fixture.',
  source_url:'https://www.queeringthemap.com/moment/123',source_entry_id:'123',provenance:'fetched',...extra});
function setup(handler,{useDefaultFetch=false}={}){
  const ids=['archive-dialog','archive-enabled','archive-add','archive-close','archive-entry-form','archive-reload',
    'archive-submit','archive-entries','archive-entry-id','archive-paste-title','archive-paste-text','archive-paste-source',
    'archive-paste','archive-dialog-error','archive-status','archive-count','archive-empty','archive-limit',
    'archive-reply-sources','archive-sources-list','archive-sources-summary','archive-sources-note'];
  const nodes=Object.fromEntries(ids.map(id=>[id,element()])),calls=[],changes=[];
  const document={getElementById:id=>nodes[id],createElement:element};
  const fetch=async(path,options)=>{
    calls.push({path,options});
    const result=handler?await handler(path,options):{entries:[]};
    return result?.status?{ok:false,status:result.status,json:async()=>{throw new Error('Private response body must not be read.');}}
      :{ok:true,status:200,json:async()=>result};
  };
  const panel=new ArchivePanel({document,...(useDefaultFetch?{}:{fetch}),onChange:value=>changes.push(value)});
  return {panel,nodes,calls,changes};
}
function text(node){return [node.textContent,...node.children.map(text)].join('\n');}
function sources(turn_id,matches,selected_count=1){return {type:'archive_sources',turn_id,scope:'selected',matches,selected_count};}

test('collection starts empty and settings contain only enabled state and selected IDs',()=>{
  const {panel,nodes,calls}=setup();
  assert.deepEqual(panel.settings(),{enabled:false,entry_ids:[]});assert.equal(nodes['archive-empty'].hidden,false);
  assert.equal(nodes['archive-count'].textContent,'0 of 0 stories selected');assert.deepEqual(calls,[]);
  assert.equal(nodes['archive-reply-sources'].hidden,true);
});

test('default browser fetch retains global receiver binding while injected fetch remains supported',async()=>{
  const previous=globalThis.fetch;let called=false;
  globalThis.fetch=function(path,options){
    called=true;assert.equal(this,globalThis);assert.equal(path,'/api/archive/entries');
    assert.equal(options.redirect,'error');
    return Promise.resolve({ok:true,status:200,json:async()=>({entries:[]})});
  };
  try{
    const {panel,calls}=setup(undefined,{useDefaultFetch:true});
    assert.equal(await panel.load(),true);assert.equal(called,true);assert.deepEqual(calls,[]);
  }finally{globalThis.fetch=previous;}
  const injected=setup(()=>({entries:[entry()]}));
  assert.equal(await injected.panel.load(),true);assert.equal(injected.calls.length,1);
});

test('entry import accepts only numeric IDs and exact official moment URLs, not the homepage or invented query URLs',()=>{
  assert.equal(archiveEntryId('123'),'123');assert.equal(archiveEntryId(' https://www.queeringthemap.com/moment/456 '),'456');
  assert.equal(archiveEntryId('000123'),'123');assert.equal(archiveEntryId('https://queeringthemap.com/moment/456/'),'456');
  for(const value of ['https://www.queeringthemap.com/','https://www.queeringthemap.com/?id=123',
    'https://example.com/moment/123','http://www.queeringthemap.com/moment/123','https://www.queeringthemap.com/moment/123?x=1',
    'https://www.queeringthemap.com/moment/123#x','javascript:alert(1)','../123','0','0000','1'.repeat(13)])assert.equal(archiveEntryId(value),null);
});

test('source links allow only HTTPS Queering the Map origin without embedded credentials',()=>{
  assert.equal(archiveSourceURL(''),'');assert.equal(archiveSourceURL('https://www.queeringthemap.com/moment/123'),'https://www.queeringthemap.com/moment/123');
  for(const value of ['http://www.queeringthemap.com/moment/1','https://elsewhere.example/story','//www.queeringthemap.com/moment/1',
    'javascript:alert(1)','data:text/html,story','https://name:password@www.queeringthemap.com/moment/1',
    'https://www.queeringthemap.com.evil.example/moment/1','https://www.queeringthemap.com:444/moment/1',
    'https://www.queeringthemap.com:443/moment/1','https://www.queeringthemap.com/other',
    'https://www.queeringthemap.com/other/../moment/1','https://www.queeringthemap.com/moment/%31',
    'https://www.queeringthemap.com/moment/1?','https://www.queeringthemap.com/moment/1#'])assert.equal(archiveSourceURL(value),null);
});

test('loading selected collection preserves valid applied IDs, removes missing IDs and never invents stories',async()=>{
  const {panel,nodes,calls,changes}=setup(()=>({entries:[entry()]}));
  panel.apply({enabled:true,entry_ids:['qtm-123','missing','qtm-123','../unsafe']});
  assert.deepEqual(panel.settings().entry_ids,['qtm-123','missing']);assert.equal(await panel.load(),true);
  assert.equal(calls[0].path,'/api/archive/entries');assert.equal(calls[0].options.redirect,'error');
  assert.deepEqual(panel.settings(),{enabled:true,entry_ids:['qtm-123']});assert.equal(nodes['archive-count'].textContent,'1 of 1 stories selected');
  assert.deepEqual(changes.at(-1),panel.settings());assert.match(text(nodes['archive-entries']),/Test story/);
});

test('import posts exactly one selected story ID and selects only the returned saved entry',async()=>{
  const imported=entry();
  const {panel,nodes,calls,changes}=setup(()=>({entry:imported,entries:[imported]}));
  panel.apply({enabled:true,entry_ids:[]});panel.open();nodes['archive-entry-id'].value='https://www.queeringthemap.com/moment/123';
  assert.equal(await panel.submit(),true);
  assert.deepEqual(JSON.parse(calls[0].options.body),{entry_id:'123'});assert.equal(calls[0].options.method,'POST');
  assert.deepEqual(calls[0].options.headers,{'Content-Type':'application/json'});
  assert.deepEqual(panel.settings().entry_ids,['qtm-123']);assert.equal(nodes['archive-dialog'].open,false);
  assert.equal(nodes['archive-entry-id'].value,'');assert.deepEqual(changes.at(-1),panel.settings());
});

test('pasting a passage preserves its text, labels it separately and never treats its source as a map-wide import',async()=>{
  const pasted=entry('manual-1',{title:'Pasted title',text:'Words pasted by the user.',source_url:'',source_entry_id:null,provenance:'pasted'});
  const {panel,nodes,calls}=setup(()=>({entry:pasted,entries:[pasted]}));
  nodes['archive-paste-title'].value='Pasted title';nodes['archive-paste-text'].value=pasted.text;
  assert.equal(await panel.submit(),true);
  assert.deepEqual(JSON.parse(calls[0].options.body),{text:pasted.text,title:'Pasted title',source_url:''});
  assert.match(text(nodes['archive-entries']),/Pasted passage · supplied by you/);
  assert.doesNotMatch(JSON.stringify(panel.settings()),/Words pasted by/);
});

test('invalid or ambiguous imports are rejected before any request and text/source bounds are enforced',async()=>{
  const {panel,nodes,calls}=setup();
  nodes['archive-entry-id'].value='https://www.queeringthemap.com/';assert.equal(await panel.submit(),false);
  nodes['archive-entry-id'].value='123';nodes['archive-paste-text'].value='A passage';assert.equal(await panel.submit(),false);
  nodes['archive-entry-id'].value='';nodes['archive-paste-source'].value='https://elsewhere.example/story';assert.equal(await panel.submit(),false);
  nodes['archive-paste-source'].value='';nodes['archive-paste-text'].value='x'.repeat(12001);assert.equal(await panel.submit(),false);
  nodes['archive-paste-text'].value='A passage';nodes['archive-paste-title'].value='x'.repeat(121);assert.equal(await panel.submit(),false);
  assert.deepEqual(calls,[]);assert.ok(nodes['archive-dialog-error'].textContent);
});

test('removal uses only the saved collection entry ID and updates selected IDs from the returned collection',async()=>{
  const {panel,calls,changes}=setup(()=>({entries:[]}));panel.setEntries([entry('qtm:123')]);panel.apply({enabled:true,entry_ids:['qtm:123']});
  assert.equal(await panel.remove('qtm:123'),true);assert.equal(calls[0].path,'/api/archive/entries/qtm%3A123');
  assert.equal(calls[0].options.method,'DELETE');assert.deepEqual(changes.at(-1),{enabled:true,entry_ids:[]});
  assert.equal(await panel.remove('../other'),false);assert.equal(calls.length,1);
});

test('active call blocks edits, import, removal and toggles, and closes an open draft dialog',async()=>{
  const {panel,nodes,calls}=setup();panel.setEntries([entry()]);panel.apply({enabled:true,entry_ids:['qtm-123']});
  panel.open();nodes['archive-paste-text'].value='Private unsaved draft';panel.setBusy(true);
  assert.equal(nodes['archive-dialog'].open,false);assert.equal(nodes['archive-paste-text'].value,'');
  assert.equal(nodes['archive-add'].disabled,true);assert.equal(nodes['archive-enabled'].disabled,true);
  assert.equal(panel.open(),false);assert.equal(await panel.submit(),false);assert.equal(await panel.remove('qtm-123'),false);
  const checkbox=nodes['archive-entries'].querySelectorAll('input')[0];checkbox.checked=false;checkbox.dispatch('change');
  assert.deepEqual(panel.settings().entry_ids,['qtm-123']);assert.equal(checkbox.checked,true);
  nodes['archive-enabled'].checked=false;nodes['archive-enabled'].dispatch('change');assert.equal(panel.settings().enabled,true);
  assert.deepEqual(calls,[]);panel.setBusy(false);assert.equal(nodes['archive-add'].disabled,false);
});

test('fifty-story limit is bounded and does not send a fifty-first import',async()=>{
  const {panel,nodes,calls}=setup();panel.setEntries(Array.from({length:60},(_,index)=>entry(`saved-${index}`)));
  panel.apply({enabled:true,entry_ids:Array.from({length:70},(_,index)=>`saved-${index}`)});
  assert.equal(panel.entries.length,50);assert.equal(panel.settings().entry_ids.length,50);
  assert.equal(nodes['archive-add'].disabled,true);assert.equal(nodes['archive-limit'].hidden,false);
  nodes['archive-entry-id'].value='123';assert.equal(await panel.submit(),false);assert.deepEqual(calls,[]);
});

test('errors are safe, old-server 404 keeps applied settings and offers a retry',async()=>{
  const {panel,nodes}=setup(()=>({status:404}));panel.apply({enabled:true,entry_ids:['qtm-123']});
  assert.equal(await panel.load(),false);assert.deepEqual(panel.settings().entry_ids,['qtm-123']);
  assert.match(nodes['archive-status'].textContent,/Update, restart and refresh/);assert.equal(nodes['archive-reload'].hidden,false);
  assert.equal(nodes['archive-add'].disabled,false);
});

test('in-flight import cannot select a passage for a different character or active call',async()=>{
  let finish;
  const {panel,nodes,changes}=setup(()=>new Promise(resolve=>{finish=resolve;}));
  panel.apply({enabled:true,entry_ids:[]});nodes['archive-entry-id'].value='123';const importing=panel.submit();
  panel.apply({enabled:false,entry_ids:[]});panel.setBusy(true);finish({entry:entry(),entries:[entry()]});
  assert.equal(await importing,true);assert.deepEqual(panel.settings(),{enabled:false,entry_ids:[]});assert.deepEqual(changes,[]);
});

test('citations show only current-turn selected passages with safe original links, provenance and license',()=>{
  const {panel,nodes}=setup();panel.apply({enabled:true,entry_ids:['qtm-123']});panel.setTurn(4);
  const match={...entry(),excerpt:'Exact excerpt for this reply.'};
  assert.equal(panel.showSources(sources(4,[match])),true);assert.equal(nodes['archive-reply-sources'].hidden,false);
  assert.match(text(nodes['archive-sources-list']),/Exact excerpt for this reply/);
  assert.match(text(nodes['archive-sources-list']),/Queering the Map · CC BY-NC-SA 4.0/);
  const links=nodes['archive-sources-list'].querySelectorAll('a');
  assert.deepEqual(links.map(link=>[link.textContent,link.href]),[['Original entry',match.source_url],['Open map','https://www.queeringthemap.com/'],['License','https://creativecommons.org/licenses/by-nc-sa/4.0/']]);
  assert.equal(links.every(link=>link.target==='_blank'&&link.rel==='noopener noreferrer'),true);
  assert.equal(panel.showSources(sources(3,[{...match,excerpt:'STALE WORDS'}])),false);
  assert.doesNotMatch(text(nodes['archive-sources-list']),/STALE WORDS/);
  panel.setTurn(5);assert.equal(nodes['archive-reply-sources'].hidden,true);
  assert.equal(panel.showSources(sources(4,[match])),false);
});

test('unselected sources and map-wide claims cannot appear, while no-match results remain honest',()=>{
  const {panel,nodes}=setup();panel.apply({enabled:true,entry_ids:['qtm-123']});panel.setTurn(1);
  assert.equal(panel.showSources({...sources(1,[entry()]),scope:'whole_map'}),false);
  assert.equal(panel.showSources(sources(1,[entry('unselected',{excerpt:'DO NOT DISPLAY'})])),true);
  assert.doesNotMatch(text(nodes['archive-sources-list']),/DO NOT DISPLAY/);
  assert.match(nodes['archive-sources-note'].textContent,/No matching passage from your selected stories/);
});

test('story markup remains literal and unsafe source URLs never become clickable',()=>{
  const {panel,nodes}=setup();const unsafe='<script>alert(1)</script><img src=x onerror=bad()>';
  panel.setEntries([entry('manual-1',{title:unsafe,text:unsafe,source_entry_id:null,source_url:'javascript:alert(1)'})]);panel.render();
  assert.match(text(nodes['archive-entries']),/<script>alert\(1\)<\/script>/);
  assert.deepEqual(nodes['archive-entries'].querySelectorAll('a'),[]);
});

test('profile switch/reset clears source context and prevents late citation reuse',()=>{
  const {panel,nodes}=setup();panel.apply({enabled:true,entry_ids:['qtm-123']});panel.setTurn(2);panel.showSources(sources(2,[entry()]));
  panel.apply({enabled:false,entry_ids:[]});assert.equal(panel.turnId,0);assert.equal(nodes['archive-reply-sources'].hidden,true);
  assert.equal(panel.showSources(sources(2,[entry()])),false);
  panel.apply({enabled:true,entry_ids:['qtm-123']});panel.setTurn(3);panel.resetSources();
  assert.equal(panel.showSources(sources(3,[entry()])),false);
});

test('a pasted numeric permalink remains explicitly unverified even when it carries a source entry ID',()=>{
  const {panel,nodes}=setup();const pasted=entry('manual-1',{provenance:'pasted'});
  panel.setEntries([pasted]);panel.apply({enabled:true,entry_ids:['manual-1']});panel.setTurn(1);
  panel.showSources(sources(1,[{...pasted,excerpt:'User-provided words, not a verified original.'}]));
  assert.match(text(nodes['archive-sources-list']),/Pasted passage · unverified source/);
  assert.doesNotMatch(text(nodes['archive-sources-list']),/fetched entry/);
  const links=nodes['archive-sources-list'].querySelectorAll('a');
  assert.equal(links[0].textContent,'Supplied source link');assert.equal(links[0].href,pasted.source_url);
});
