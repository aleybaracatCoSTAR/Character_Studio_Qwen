const MAP='https://www.queeringthemap.com/';
const LICENSE='https://creativecommons.org/licenses/by-nc-sa/4.0/';
const MAX_ENTRIES=50;
const PAGE_SIZE=25;
export const MAX_ARCHIVE_IMPORT_BYTES=128*1024*1024;
const count=value=>Number.isSafeInteger(value)&&value>=0?value:0;
const validId=value=>typeof value==='string'&&/^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$/.test(value);
const validCollectionId=value=>typeof value==='string'&&/^[a-z0-9][a-z0-9-]{0,63}$/.test(value);
const recordKey=entry=>(validCollectionId(entry.collection_id)?entry.collection_id:'qtm')+'|'+entry.id;
const collectionIds=values=>[...new Set(Array.isArray(values)?values.filter(validCollectionId):[])].slice(0,8);
const METADATA=['artist','date','medium','dimensions','credit_line','accession_number','classification','rights'];
const MUSEUM_SOURCES=[
  {id:'met',label:'The Metropolitan Museum of Art'},
  {id:'aic',label:'Art Institute of Chicago'},
  {id:'cleveland',label:'Cleveland Museum of Art'},
  {id:'rijksmuseum',label:'Rijksmuseum'},
  {id:'getty',label:'J. Paul Getty Museum'}
];
const museumSource=id=>MUSEUM_SOURCES.find(source=>source.id===id);
export function publicArchiveURL(value){
  if(!value)return '';
  if(typeof value!=='string'||value.length>2048||/[\s\u0000-\u001f\u007f]/.test(value))return null;
  try{
    const url=new URL(value),host=url.hostname.toLowerCase();
    if(url.protocol!=='https:'||url.username||url.password||url.port||!host.includes('.')||host.includes(':')
        ||/^[\d.]+$/.test(host)||/(?:^|\.)(?:localhost|local|internal|lan|home)$/.test(host)
        ||!host.split('.').every(label=>/^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/.test(label)))return null;
    return url.href;
  }catch(error){return null;}
}
export function displayArchiveImageURL(value){
  const url=publicArchiveURL(value);if(!url)return url;
  const aic=/^https:\/\/www\.artic\.edu\/iiif\/2\/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})\/full\/400,\/0\/default\.jpg$/.exec(url);
  return aic?'/api/archive/museum-images/aic/'+aic[1]:url;
}
const entryKind=entry=>['qtm','nga','custom'].includes(entry.source_kind)?entry.source_kind
  :entry.collection_id==='national-gallery-of-art'?'nga':entry.collection_id&&entry.collection_id!=='qtm'?'custom':'qtm';
const entryURL=entry=>entryKind(entry)==='qtm'?archiveSourceURL(entry.source_url):publicArchiveURL(entry.source_url);
const metadata=value=>Object.fromEntries(METADATA.filter(key=>typeof value?.[key]==='string').map(key=>[key,value[key].slice(0,1000)]));

export function archiveEntryId(value){
  const input=String(value??'').trim();
  if(/^\d{1,12}$/.test(input))return input.replace(/^0+/,'')||null;
  const source=archiveSourceURL(input);
  return source?.startsWith(MAP+'moment/')?source.slice((MAP+'moment/').length):null;
}
export function archiveSourceURL(value){
  if(!value)return '';
  try{
    if(typeof value!=='string'||/[\s\u0000-\u001f]/.test(value)||value.includes('?')||value.includes('#'))return null;
    const url=new URL(value);
    if(url.protocol!=='https:'||!['www.queeringthemap.com','queeringthemap.com'].includes(url.hostname)||url.port||url.username||url.password)return null;
    // Reject even an explicitly supplied standard port; source permalinks are
    // canonical official URLs rather than arbitrary same-host navigation.
    const match=/^https:\/\/(?:www\.)?queeringthemap\.com(?:\/moment\/(\d{1,12})\/?|\/?)$/.exec(value);
    if(!match)return null;
    if(match[1]===undefined)return MAP;
    const id=match[1].replace(/^0+/,'');
    return id?MAP+'moment/'+id:null;
  }catch(error){return null;}
}

// Story text stays on the server; character settings contain scope and IDs.
// Search pages must never determine which off-page stories remain selected.
export class ArchivePanel {
  constructor({document,fetch=(...args)=>globalThis.fetch(...args),onChange=()=>{}}){
    this.document=document;this.fetch=fetch;this.onChange=onChange;
    this.entries=[];this.selectedIds=new Set();this.loaded=false;this.busy=false;this.pending=false;this.profileEpoch=0;this.turnId=0;this._sourceMatches=[];
    this.libraryMode=false;this.total=0;this.matched=0;this.offset=0;this.limit=PAGE_SIZE;this.query='';
    this.scope='selected';this.refreshNeeded=false;this.selectionNeeded=false;
    this.collectionIds=[];this.collections=[{id:'qtm',label:'Queering the Map',source_kind:'qtm',count:0,has_images:false}];this.collectionsSupported=null;
    this.museumSources=MUSEUM_SOURCES.map(source=>({...source}));this.museumsSupported=null;this.museumLoadNotice='';
    this.dialog=this.get('archive-dialog');
    this.get('archive-enabled').addEventListener('change',()=>{
      if(this.busy||this.pending){this.get('archive-enabled').checked=this.enabled;return;}
      this.enabled=this.get('archive-enabled').checked;this.resetSources();this.notify();
    });
    this.get('archive-add').addEventListener('click',()=>this.open());
    this.get('archive-close').addEventListener('click',()=>this.dialog.close());
    this.dialog.addEventListener('close',()=>this.clearForm());
    this.get('archive-entry-form').addEventListener('submit',event=>{event.preventDefault();void this.submit();});
    this.get('archive-reload').addEventListener('click',()=>void this.load());
    this.get('archive-scope').addEventListener('change',()=>{
      if(this.busy||this.pending){this.get('archive-scope').value=this.scope;return;}
      if(this.collectionIds.length){this.get('archive-scope').value='library';return;}
      this.scope=this.get('archive-scope').value==='library'?'library':'selected';this.resetSources();this.notify();this.render();
    });
    this.get('archive-search-submit').addEventListener('click',()=>void this.search());
    this.get('archive-search').addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();void this.search();}});
    this.get('archive-previous').addEventListener('click',()=>void this.load({offset:Math.max(0,this.offset-this.limit)}));
    this.get('archive-next').addEventListener('click',()=>void this.load({offset:this.offset+this.limit}));
    this.get('archive-import-file').addEventListener('change',()=>this.controls());
    this.get('archive-import').addEventListener('click',()=>void this.upload());
    this.get('archive-add-collection').addEventListener('click',()=>this.openCollection());
    this.get('archive-collection-close').addEventListener('click',()=>this.get('archive-collection-dialog').close());
    this.get('archive-collection-dialog').addEventListener('close',()=>this.clearCollectionForm());
    this.get('archive-collection-form').addEventListener('submit',event=>{event.preventDefault();void this.createCollection();});
    this.get('archive-add-museum')?.addEventListener('click',()=>void this.openMuseum());
    this.get('archive-museum-close')?.addEventListener('click',()=>this.get('archive-museum-dialog').close());
    this.get('archive-museum-dialog')?.addEventListener('close',()=>this.clearMuseumForm());
    this.get('archive-museum-form')?.addEventListener('submit',event=>{event.preventDefault();void this.importMuseum();});
    this.enabled=false;this.apply({enabled:false,entry_ids:[]});
  }
  get(id){return this.document.getElementById(id);}
  get sourceMatches(){return this._sourceMatches.map(entry=>({...entry,metadata:{...entry.metadata}}));}
  settings(){return {enabled:!!this.enabled,scope:this.scope,entry_ids:[...this.selectedIds],...(this.collectionIds.length?{collection_ids:[...this.collectionIds]}:{})};}
  notify(){this.onChange(this.settings());}
  apply(settings={}){
    this.profileEpoch++;
    this.museumLoadNotice='';
    const previous=this.collectionIds.join(',');this.collectionIds=collectionIds(settings.collection_ids);
    this.enabled=!!settings.enabled;this.get('archive-enabled').checked=this.enabled;
    this.scope=this.collectionIds.length||settings.scope==='library'?'library':'selected';this.get('archive-scope').value=this.scope;
    this.selectedIds=new Set(this.collectionIds.length?[]:(Array.isArray(settings.entry_ids)?settings.entry_ids:[]).filter(validId).slice(0,MAX_ENTRIES));
    if(this.loaded&&!this.libraryMode)this.filterSelection();
    this.selectionNeeded=this.libraryMode;if(this.pending)this.refreshNeeded=true;
    if(this.loaded&&previous!==this.collectionIds.join(',')){
      this.refreshNeeded=true;this.query='';this.offset=0;this.get('archive-search').value='';
    }
    if(this.dialog.open)this.dialog.close();else this.clearForm();
    if(this.get('archive-collection-dialog').open)this.get('archive-collection-dialog').close();else this.clearCollectionForm();
    if(this.get('archive-museum-dialog')?.open)this.get('archive-museum-dialog').close();else this.clearMuseumForm();
    this.get('archive-import-file').value='';this.resetSources();this.render();
    if(this.loaded&&!this.busy&&!this.pending){if(this.refreshNeeded)this.followUp();else if(this.libraryMode)void this.validateSelection();}
  }
  setBusy(busy){
    this.busy=!!busy;if(this.busy&&this.dialog.open)this.dialog.close();this.controls();
    if(this.busy&&this.get('archive-collection-dialog').open)this.get('archive-collection-dialog').close();
    if(this.busy&&this.get('archive-museum-dialog')?.open)this.get('archive-museum-dialog').close();
    if(!this.busy&&!this.pending)this.followUp();
  }
  controls(){
    const blocked=this.busy||this.pending;
    this.get('archive-enabled').disabled=blocked;
    this.get('archive-add').disabled=blocked||(!this.libraryMode&&this.entries.length>=MAX_ENTRIES);
    this.get('archive-submit').disabled=blocked;
    this.get('archive-reload').disabled=blocked;
    for(const id of ['archive-scope','archive-search','archive-search-submit','archive-import-file'])this.get(id).disabled=blocked;
    this.get('archive-scope').disabled=blocked||this.collectionIds.length>0;
    this.get('archive-add').hidden=this.collectionIds.length>0;this.get('archive-qtm-import').hidden=this.collectionIds.length>0;
    this.get('archive-qtm-note').hidden=this.collectionIds.length>0;
    this.get('archive-qtm-credit').hidden=this.collectionIds.length>0;
    this.get('archive-add-collection').disabled=blocked||this.collectionsSupported!==true;
    if(this.get('archive-add-museum'))this.get('archive-add-museum').disabled=blocked||this.collectionsSupported!==true;
    this.get('archive-collection-tools').hidden=this.collectionsSupported===false;
    for(const id of ['archive-collection-name','archive-collection-file','archive-collection-submit'])this.get(id).disabled=blocked;
    for(const id of ['archive-museum-source','archive-museum-query','archive-museum-limit'])if(this.get(id))this.get(id).disabled=blocked;
    if(this.get('archive-museum-submit'))this.get('archive-museum-submit').disabled=blocked||this.museumsSupported!==true;
    this.get('archive-museum-form')?.setAttribute('aria-busy',String(this.pending));
    for(const node of this.get('archive-collections').querySelectorAll('input'))node.disabled=blocked||(!node.checked&&this.collectionIds.length>=8);
    this.get('archive-import').disabled=blocked||!this.get('archive-import-file').files?.length;
    this.get('archive-previous').disabled=blocked||!this.libraryMode||this.offset===0;
    this.get('archive-next').disabled=blocked||!this.libraryMode||this.offset+this.entries.length>=this.matched;
    for(const node of this.get('archive-entries').querySelectorAll('input,button'))node.disabled=blocked||
      (node.tagName==='INPUT'&&(this.scope==='library'||(!node.checked&&this.selectedIds.size>=MAX_ENTRIES)));
    for(const id of ['archive-entry-id','archive-paste-title','archive-paste-text','archive-paste-source'])this.get(id).disabled=blocked;
  }
  clearForm(){
    for(const id of ['archive-entry-id','archive-paste-title','archive-paste-text','archive-paste-source'])this.get(id).value='';
    this.get('archive-paste').open=false;this.get('archive-dialog-error').textContent='';
  }
  open(){
    if(this.busy||this.pending||this.collectionIds.length||(!this.libraryMode&&this.entries.length>=MAX_ENTRIES))return false;
    this.clearForm();if(!this.dialog.open)this.dialog.showModal();return true;
  }
  async response(path,options){
    let response;
    try{response=await this.fetch(path,{redirect:'error',...options});}
    catch(error){throw new Error('Could not reach the archive server. Check the local server and try again.');}
    if(!response.ok)throw new Error(response.status===404
      ?'Archive tools need the latest server. Update, restart and refresh; your character is unchanged.'
      :response.status===413?'The collection file is too large. Use a file up to 128 MiB.':'The archive request could not be completed. Check the file or entry and try again.');
    try{return await response.json();}
    catch(error){throw new Error('The archive server returned an unreadable response. Please try again.');}
  }
  setCollections(values){
    if(!Array.isArray(values))throw new Error('The archive server returned an unreadable collection list.');
    const seen=new Set();
    this.collections=values.filter(value=>value&&validCollectionId(value.id)&&!seen.has(value.id)&&(seen.add(value.id),true)).slice(0,500)
      .map(value=>({id:value.id,label:typeof value.label==='string'?value.label.slice(0,80):value.id,
        source_kind:['qtm','nga','custom'].includes(value.source_kind)?value.source_kind:'custom',
        source_url:publicArchiveURL(value.source_url)||'',count:count(value.count),has_images:value.has_images===true}));
    this.collectionsSupported=true;
  }
  async loadCollections(){
    if(this.busy||this.pending)return false;
    this.pending=true;this.controls();
    try{const result=await this.response('/api/archive/collections');this.setCollections(result.collections);return true;}
    catch(error){this.collectionsSupported=false;return false;}
    finally{this.finish();}
  }
  renderCollections(){
    const list=this.get('archive-collections');list.replaceChildren();
    const selected=new Set(this.collectionIds.length?this.collectionIds:['qtm']);
    const values=[...this.collections,...[...selected].filter(id=>!this.collections.some(value=>value.id===id))
      .map(id=>({id,label:'Unavailable collection',count:0,source_kind:'custom'}))];
    for(const collection of values){
      const label=this.document.createElement('label'),checkbox=this.document.createElement('input'),text=this.document.createElement('span');
      checkbox.type='checkbox';checkbox.checked=selected.has(collection.id);
      text.textContent=`${collection.label} · ${collection.count.toLocaleString()} records`;
      checkbox.addEventListener('change',()=>{
        if(this.busy||this.pending){checkbox.checked=selected.has(collection.id);return;}
        const next=new Set(this.collectionIds.length?this.collectionIds:['qtm']);
        if(checkbox.checked)next.add(collection.id);else next.delete(collection.id);
        if(!next.size||next.size>8){checkbox.checked=selected.has(collection.id);this.get('archive-status').textContent='Choose between 1 and 8 collections, or turn off Consult an archive.';return;}
        this.collectionIds=next.size===1&&next.has('qtm')?[]:[...next];this.scope='library';this.get('archive-scope').value='library';
        this.selectedIds.clear();this.query='';this.resetSources();this.notify();this.render();void this.load({offset:0,query:''});
      });
      label.append(checkbox,text);list.append(label);
    }
  }
  clearCollectionForm(){
    this.get('archive-collection-name').value='';this.get('archive-collection-file').value='';this.get('archive-collection-error').textContent='';
  }
  openCollection(){
    if(this.busy||this.pending||this.collectionsSupported!==true)return false;
    this.clearCollectionForm();this.get('archive-collection-dialog').showModal();return true;
  }
  async createCollection(){
    if(this.busy||this.pending||this.collectionsSupported!==true)return false;
    const name=this.get('archive-collection-name').value.trim(),file=this.get('archive-collection-file').files?.[0];
    if(!name||name.length>80){this.get('archive-collection-error').textContent='Give the collection a name up to 80 characters.';return false;}
    if(!file||typeof file.name!=='string'||!/^.{1,200}\.(json|jsonl|csv)$/i.test(file.name)||/[\u0000-\u001f\u007f]/.test(file.name)
        ||!Number.isSafeInteger(file.size)||file.size<=0||file.size>MAX_ARCHIVE_IMPORT_BYTES){
      this.get('archive-collection-error').textContent='Choose a JSON, JSONL or CSV file, larger than zero and up to 128 MiB.';return false;
    }
    const epoch=this.profileEpoch;this.pending=true;this.controls();this.get('archive-collection-error').textContent='Adding collection…';
    try{
      const result=await this.response('/api/archive/collections/import',{method:'POST',headers:{'Content-Type':'application/octet-stream',
        'X-Archive-Filename':encodeURIComponent(file.name),'X-Collection-Name':encodeURIComponent(name)},body:file});
      if(!validCollectionId(result.collection?.id))throw new Error('The archive server returned an unreadable collection.');
      this.setCollections([...this.collections.filter(value=>value.id!==result.collection.id),result.collection]);
      if(!this.current(epoch))return true;
      this.collectionIds=[result.collection.id];this.scope='library';this.get('archive-scope').value='library';this.selectedIds.clear();this.query='';
      this.notify();this.resetSources();this.refreshNeeded=true;this.get('archive-collection-dialog').close();
      this.get('archive-status').textContent=`Added ${typeof result.collection.label==='string'?result.collection.label.slice(0,80):name} · ${count(result.added).toLocaleString()} records imported.`;return true;
    }catch(error){if(this.current(epoch)){this.get('archive-collection-error').textContent=error.message;if(!this.get('archive-collection-dialog').open)this.get('archive-status').textContent=error.message;}return false;}
    finally{this.finish();}
  }
  clearMuseumForm(){
    for(const [id,value] of [['archive-museum-query',''],['archive-museum-limit','25'],['archive-museum-error','']]){
      const node=this.get(id);if(node){if(id==='archive-museum-error')node.textContent=value;else node.value=value;}
    }
  }
  renderMuseumSources(){
    const select=this.get('archive-museum-source');if(!select)return;
    const previous=select.value;select.replaceChildren();
    for(const source of this.museumSources){const option=this.document.createElement('option');option.value=source.id;option.textContent=source.label;select.append(option);}
    select.value=this.museumSources.some(source=>source.id===previous)?previous:this.museumSources[0]?.id||'';
  }
  async loadMuseumSources(){
    if(this.busy||this.pending)return false;
    const epoch=this.profileEpoch;this.pending=true;this.controls();
    try{
      const result=await this.response('/api/archive/museums');
      if(!Array.isArray(result.sources))throw new Error('The archive server returned an unreadable museum list.');
      const available=new Set(result.sources.filter(source=>museumSource(source?.id)).map(source=>source.id));
      if(!available.size)throw new Error('No museum sources are available on this server.');
      this.museumSources=MUSEUM_SOURCES.filter(source=>available.has(source.id)).map(source=>({...source}));this.museumsSupported=true;
      if(this.current(epoch))this.renderMuseumSources();return true;
    }catch(error){this.museumsSupported=false;if(this.current(epoch))this.get('archive-museum-error').textContent=error.message;return false;}
    finally{this.finish();}
  }
  async openMuseum(){
    if(this.busy||this.pending||this.collectionsSupported!==true||!this.get('archive-museum-dialog'))return false;
    this.clearMuseumForm();this.renderMuseumSources();this.get('archive-museum-dialog').showModal();
    return this.loadMuseumSources();
  }
  museumErrors(values){
    if(!Array.isArray(values))return '';
    return values.filter(value=>museumSource(value?.source)).slice(0,5).map(value=>{
      const label=museumSource(value.source).label;
      const message=value.reason==='no_results'?'No matching catalogue records were found.':value.reason==='rate_limited'?'The museum is busy. Please try again later.':value.reason==='timeout'?'The museum took too long to respond. Please try again.':'The museum import could not be completed. Try another search or try again.';
      return label+': '+message;
    }).join(' ');
  }
  async importMuseum(){
    if(this.busy||this.pending||this.collectionsSupported!==true||this.museumsSupported!==true)return false;
    const source=this.get('archive-museum-source').value,query=this.get('archive-museum-query').value.trim(),limit=Number(this.get('archive-museum-limit').value);
    const errorNode=this.get('archive-museum-error');
    if(!museumSource(source)){errorNode.textContent='Choose one of the available museum sources.';return false;}
    if(!query||query.length>200||/[\u0000-\u001f\u007f]/.test(query)){errorNode.textContent='Enter an artist or subject up to 200 characters.';return false;}
    if(![25,50,100,200].includes(limit)){errorNode.textContent='Choose a record limit of 25, 50, 100 or 200.';return false;}
    const epoch=this.profileEpoch,previous=this.collectionIds.length?[...this.collectionIds]:['qtm'];
    this.pending=true;this.controls();errorNode.textContent='Importing catalogue records…';
    try{
      const result=await this.response('/api/archive/museums/import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source,query,limit})});
      if(!Array.isArray(result.results)||!Array.isArray(result.errors))throw new Error('The archive server returned an unreadable museum import.');
      const imported=result.results.filter(value=>value?.source===source&&validCollectionId(value.collection?.id)).slice(0,5);
      if(result.results.length&&!imported.length)throw new Error('The archive server returned an unreadable museum import.');
      this.setCollections([...this.collections.filter(value=>!imported.some(item=>item.collection.id===value.id)),...imported.map(value=>value.collection)]);
      // Importing adds records globally. Only the character that started this
      // request may acquire the newly imported collections.
      try{const refreshed=await this.response('/api/archive/collections');this.setCollections(refreshed.collections);}catch(error){/* The import response still supplies the new collection metadata. */}
      if(!this.current(epoch))return imported.length>0;
      const warnings=this.museumErrors(result.errors),next=[...new Set([...imported.map(value=>value.collection.id),...previous])];
      if(imported.length){
        const canSelect=next.length<=8;
        if(canSelect){this.collectionIds=next;this.scope='library';this.selectedIds.clear();this.query=query;this.get('archive-search').value=query;this.get('archive-scope').value='library';this.notify();this.resetSources();this.refreshNeeded=true;}
        const added=imported.reduce((total,value)=>total+count(value.added),0);
        const status=`Imported ${added.toLocaleString()} catalogue records from ${museumSource(source).label}. This is a search subset, not the entire museum.`+(canSelect?'':' Your eight selected collections were kept. Uncheck a collection before selecting this museum.')+(warnings?' '+warnings:'');
        this.museumLoadNotice=canSelect?status:'';this.get('archive-museum-dialog').close();this.get('archive-status').textContent=status;return true;
      }
      errorNode.textContent=warnings||'No catalogue records were imported. Try another artist or subject.';return false;
    }catch(error){if(this.current(epoch)){errorNode.textContent=error.message;if(!this.get('archive-museum-dialog').open)this.get('archive-status').textContent=error.message;}return false;}
    finally{this.finish();}
  }
  setEntries(entries){
    if(!Array.isArray(entries))throw new Error('The archive server returned an unreadable story list.');
    const seen=new Set();
    this.entries=entries.filter(entry=>entry&&validId(entry.id)&&typeof entry.text==='string'&&!seen.has(recordKey(entry))
      &&(seen.add(recordKey(entry)),true)).slice(0,MAX_ENTRIES).map(entry=>({id:entry.id,title:typeof entry.title==='string'?entry.title.slice(0,120):'Saved passage',
        text:entry.text.slice(0,12000),source_entry_id:typeof entry.source_entry_id==='string'?entry.source_entry_id:null,
        provenance:['fetched','imported'].includes(entry.provenance)?entry.provenance:'pasted',
        source_kind:entryKind(entry),collection_id:validCollectionId(entry.collection_id)?entry.collection_id:'qtm',
        collection_label:typeof entry.collection_label==='string'?entry.collection_label.slice(0,80):'',
        source_url:entryURL(entry)||'',image_url:publicArchiveURL(entry.image_url)||'',image_alt:typeof entry.image_alt==='string'?entry.image_alt.slice(0,300):'',metadata:metadata(entry.metadata)}));
    this.loaded=true;
    if(!this.libraryMode){this.total=this.matched=this.entries.length;this.offset=0;this.limit=MAX_ENTRIES;}
  }
  filterSelection(){
    const available=new Set(this.entries.map(entry=>entry.id));
    this.selectedIds=new Set([...this.selectedIds].filter(id=>available.has(id)));
  }
  setPage(result){
    const modern=result.scope==='library'&&Number.isSafeInteger(result.total)&&Number.isSafeInteger(result.matched);
    this.libraryMode=modern;this.setEntries(result.entries);
    if(modern){this.entries=this.entries.slice(0,PAGE_SIZE);this.total=count(result.total);this.matched=Math.min(this.total,count(result.matched));
      this.offset=count(result.offset);this.limit=Number.isInteger(result.limit)&&result.limit>0?Math.min(PAGE_SIZE,result.limit):PAGE_SIZE;}
  }
  current(epoch){return epoch===this.profileEpoch&&!this.busy;}
  finish(){this.pending=false;this.render();this.followUp();}
  followUp(){
    if(this.busy||this.pending)return;
    if(this.refreshNeeded){this.refreshNeeded=false;void this.load({offset:0});}
    else if(this.selectionNeeded&&this.libraryMode)void this.validateSelection();
  }
  async validateSelected(epoch){
    if(!this.selectedIds.size){this.selectionNeeded=false;return;}
    const wanted=new Set(this.selectedIds);
    const result=await this.response('/api/archive/selection?ids='+encodeURIComponent([...wanted].join(',')));
    if(!Array.isArray(result.entries))throw new Error('The archive server returned an unreadable selection.');
    const available=new Set(result.entries.filter(entry=>entry&&validId(entry.id)&&typeof entry.text==='string').slice(0,MAX_ENTRIES).map(entry=>entry.id));
    if(!this.current(epoch))return;
    const before=JSON.stringify([...this.selectedIds]);this.selectedIds=new Set([...wanted].filter(id=>available.has(id)));this.selectionNeeded=false;
    if(before!==JSON.stringify([...this.selectedIds]))this.notify();
  }
  async validateSelection(){
    if(this.busy||this.pending||!this.libraryMode)return false;
    const epoch=this.profileEpoch;this.pending=true;this.selectionNeeded=false;this.controls();
    try{await this.validateSelected(epoch);return true;}
    catch(error){if(this.current(epoch))this.get('archive-status').textContent=error.message;return false;}
    finally{this.finish();}
  }
  async load({offset=this.offset,query=this.query}={}){
    if(this.pending||this.busy)return false;
    query=String(query).trim().slice(0,200);offset=count(offset);
    const epoch=this.profileEpoch;this.pending=true;this.controls();
    const path=this.collectionIds.length?'/api/archive/collections/entries?'+new URLSearchParams({ids:this.collectionIds.join(','),q:query,offset:String(offset),limit:String(PAGE_SIZE)})
      :!query&&!offset?'/api/archive/entries':'/api/archive/entries?'+new URLSearchParams({q:query,offset:String(offset),limit:String(PAGE_SIZE)});
    try{
      const result=await this.response(path);if(!this.current(epoch))return false;
      this.query=query;this.get('archive-search').value=query;this.setPage(result);
      if(this.libraryMode)await this.validateSelected(epoch);
      else{const before=JSON.stringify([...this.selectedIds]);this.filterSelection();if(before!==JSON.stringify([...this.selectedIds]))this.notify();}
      if(!this.current(epoch))return false;
      this.get('archive-status').textContent=this.museumLoadNotice;this.museumLoadNotice='';this.get('archive-reload').hidden=true;return true;
    }catch(error){if(this.current(epoch)){this.museumLoadNotice='';this.get('archive-status').textContent=error.message;this.get('archive-reload').hidden=false;}return false;}
    finally{this.finish();}
  }
  async search(){return this.load({offset:0,query:this.get('archive-search').value});}
  async submit(){
    if(this.busy||this.pending||this.collectionIds.length)return false;
    if(!this.libraryMode&&this.entries.length>=MAX_ENTRIES){this.get('archive-dialog-error').textContent='The collection is full. Remove a story before adding another.';return false;}
    const input=this.get('archive-entry-id').value.trim(),text=this.get('archive-paste-text').value.trim();
    let payload;
    if(text){
      if(input){this.get('archive-dialog-error').textContent='Use an entry URL or paste a story, one at a time.';return false;}
      const title=this.get('archive-paste-title').value.trim(),source=archiveSourceURL(this.get('archive-paste-source').value.trim());
      if(text.length>12000||title.length>120||source===null||(source&&source.length>500)){
        this.get('archive-dialog-error').textContent='Use a title up to 120 characters, a passage up to 12,000 characters and an optional HTTPS Queering the Map source link.';return false;
      }
      payload={text,title:title||'Pasted passage',source_url:source};
    }else{
      const entry_id=archiveEntryId(input);
      if(!entry_id){this.get('archive-dialog-error').textContent='Enter a numeric story ID or an exact https://www.queeringthemap.com/moment/123 entry URL, or paste the passage below.';return false;}
      payload={entry_id};
    }
    const epoch=this.profileEpoch;
    this.pending=true;this.controls();this.get('archive-dialog-error').textContent='Adding story…';
    try{
      const result=await this.response('/api/archive/entries',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      if(!this.current(epoch))return true;
      this.query='';this.get('archive-search').value='';this.setPage(result);
      if(validId(result.entry?.id)&&this.selectedIds.size<MAX_ENTRIES)this.selectedIds.add(result.entry.id);
      if(!this.libraryMode)this.filterSelection();this.notify();
      if(this.dialog.open)this.dialog.close();this.get('archive-status').textContent=this.selectedIds.has(result.entry?.id)?'Story added and selected.':'Story added to the archive. The 50-story selection is full.';
      return true;
    }catch(error){if(this.current(epoch)){this.get('archive-dialog-error').textContent=error.message;if(!this.dialog.open)this.get('archive-status').textContent=error.message;}return false;}
    finally{this.finish();}
  }
  async upload(){
    if(this.busy||this.pending||this.collectionIds.length)return false;
    const file=this.get('archive-import-file').files?.[0];
    if(!file||typeof file.name!=='string'||!/^.{1,200}\.(json|jsonl|csv)$/i.test(file.name)||/[\u0000-\u001f\u007f]/.test(file.name)){
      this.get('archive-status').textContent='Choose a JSON, JSONL or CSV collection file.';return false;
    }
    if(!Number.isSafeInteger(file.size)||file.size<=0||file.size>MAX_ARCHIVE_IMPORT_BYTES){
      this.get('archive-status').textContent='Use a collection file larger than zero and up to 128 MiB.';return false;
    }
    const epoch=this.profileEpoch;this.pending=true;this.controls();this.get('archive-status').textContent='Importing collection…';
    try{
      const result=await this.response('/api/archive/import',{method:'POST',headers:{'Content-Type':'application/octet-stream',
        'X-Archive-Filename':encodeURIComponent(file.name)},body:file});
      if(!this.current(epoch))return true;
      this.query='';this.get('archive-search').value='';this.setPage(result);this.get('archive-import-file').value='';
      this.get('archive-status').textContent=`Imported ${count(result.added).toLocaleString()} stories · ${count(result.duplicates).toLocaleString()} duplicates skipped · ${this.total.toLocaleString()} in archive.`;
      return true;
    }catch(error){if(this.current(epoch))this.get('archive-status').textContent=error.message;return false;}
    finally{this.finish();}
  }
  async remove(id){
    if(this.busy||this.pending||this.collectionIds.length||!this.entries.some(entry=>entry.id===id))return false;
    const epoch=this.profileEpoch;this.pending=true;this.controls();
    try{
      const result=await this.response(`/api/archive/entries/${encodeURIComponent(id)}`,{method:'DELETE'});
      if(!this.current(epoch))return false;
      this.query='';this.get('archive-search').value='';this.setPage(result);this.selectedIds.delete(id);
      if(!this.libraryMode)this.filterSelection();this.notify();
      this.get('archive-status').textContent='Story removed from the archive.';return true;
    }catch(error){if(this.current(epoch))this.get('archive-status').textContent=error.message;return false;}
    finally{this.finish();}
  }
  link(container,label,url){
    const anchor=this.document.createElement('a');anchor.textContent=label;anchor.href=url;anchor.target='_blank';anchor.rel='noopener noreferrer';container.append(anchor);
  }
  provenance(container,entry){
    if(entryKind(entry)!=='qtm'){
      const label=this.document.createElement('p');label.className='hint small archive-provenance';
      label.textContent=(typeof entry.collection_label==='string'?entry.collection_label.slice(0,80):'')||(entryKind(entry)==='nga'?'National Gallery of Art':'Imported collection');container.append(label);
      const details=this.document.createElement('p');details.className='hint small archive-metadata';
      details.textContent=['artist','date','medium'].map(key=>metadata(entry.metadata)[key]).filter(Boolean).join(' · ');if(details.textContent)container.append(details);
      const rights=metadata(entry.metadata).rights;if(rights){const notice=this.document.createElement('p');notice.className='hint small archive-rights';notice.textContent=rights;container.append(notice);}
      const source=publicArchiveURL(entry.source_url);if(source){const links=this.document.createElement('div');links.className='archive-links';this.link(links,'Original source',source);container.append(links);}return;
    }
    const source=archiveSourceURL(entry.source_url);
    const fetched=entry.provenance==='fetched';
    const label=this.document.createElement('p');label.className='hint small archive-provenance';
    label.textContent=entry.provenance==='imported'?'Imported passage · not individually verified · Queering the Map · CC BY-NC-SA 4.0':fetched?'Queering the Map · CC BY-NC-SA 4.0 · fetched entry':source?'Pasted passage · unverified source · Queering the Map · CC BY-NC-SA 4.0':'Pasted passage · supplied by you';container.append(label);
    const links=this.document.createElement('div');links.className='archive-links';
    if(source)this.link(links,fetched&&entry.source_entry_id?'Original entry':'Supplied source link',source);
    if(entry.source_entry_id||source||entry.provenance==='imported'){this.link(links,'Open map',MAP);this.link(links,'License',LICENSE);}
    container.append(links);
  }
  image(container,entry,className='archive-thumbnail'){
    const url=displayArchiveImageURL(entry.image_url);if(!url)return false;
    const frame=this.document.createElement('div'),image=this.document.createElement('img'),fallback=this.document.createElement('span');
    frame.className=className;image.src=url;image.alt=typeof entry.image_alt==='string'&&entry.image_alt?entry.image_alt.slice(0,300):String(entry.title||'Archive artwork').slice(0,120);
    image.loading='lazy';image.decoding='async';image.referrerPolicy='no-referrer';fallback.textContent='Image unavailable';fallback.hidden=true;
    image.addEventListener('error',()=>{image.hidden=true;fallback.hidden=false;});frame.append(image,fallback);container.append(frame);return true;
  }
  renderArtworks(matches){
    const list=this.get('archive-artworks-list');list.replaceChildren();
    for(const entry of matches.filter(value=>publicArchiveURL(value.image_url)).slice(0,3)){
      const card=this.document.createElement('article'),title=this.document.createElement('h3'),caption=this.document.createElement('p');
      card.className='archive-artwork';this.image(card,entry,'archive-artwork-image');title.textContent=typeof entry.title==='string'?entry.title.slice(0,120):'Archive artwork';
      caption.textContent=['artist','date','medium'].map(key=>metadata(entry.metadata)[key]).filter(Boolean).join(' · ');card.append(title,caption);
      const source=publicArchiveURL(entry.source_url);if(source)this.link(card,'View original source',source);
      const rights=metadata(entry.metadata).rights;if(rights){const notice=this.document.createElement('p');notice.className='archive-rights';notice.textContent=rights;card.append(notice);}list.append(card);
    }
    this.get('archive-artworks').hidden=!list.children.length;
  }
  render(){
    this.renderCollections();
    const generic=this.collectionIds.length>0;
    this.get('archive-search-label').textContent=generic?'Find records':'Find stories';
    this.get('archive-search-form').setAttribute('aria-label',generic?'Search saved records':'Search saved stories');
    this.get('archive-entries').setAttribute('aria-label',generic?'Saved records':'Saved stories');
    this.get('archive-count').textContent=generic?'Searching selected collections':this.scope==='library'?'Searching the whole local archive':this.libraryMode?`${this.selectedIds.size} stories selected`:`${this.selectedIds.size} of ${this.entries.length} stories selected`;
    this.get('archive-library-count').textContent=`${this.total.toLocaleString()} ${generic?'records in selected collections':'stories in your archive'}`;
    this.get('archive-page').textContent=this.matched?`${this.offset+1}–${Math.min(this.offset+this.entries.length,this.matched)} of ${this.matched.toLocaleString()}${this.query?' matches':generic?' records':' stories'}`:'0 results';
    this.get('archive-empty').hidden=!!this.entries.length;
    this.get('archive-empty').textContent=this.query?generic?'No records match this search.':'No stories match this search.':generic?'These collections have no records on this server yet.':'No stories added yet. Import a collection, choose an entry, or paste a passage.';
    this.get('archive-limit').hidden=this.scope==='library'||(this.libraryMode?this.selectedIds.size<MAX_ENTRIES:this.entries.length<MAX_ENTRIES);
    const list=this.get('archive-entries');list.replaceChildren();
    for(const entry of this.entries){
      const card=this.document.createElement('li');card.className='archive-entry';
      const row=this.document.createElement('div');row.className='archive-entry-row';
      const label=this.document.createElement('label'),checkbox=this.document.createElement('input'),title=this.document.createElement('span');
      checkbox.type='checkbox';checkbox.checked=this.selectedIds.has(entry.id);checkbox.hidden=this.scope==='library';title.textContent=entry.title||'Selected passage';
      checkbox.addEventListener('change',()=>{
        if(this.busy||this.pending||this.scope==='library'||(checkbox.checked&&!this.selectedIds.has(entry.id)&&this.selectedIds.size>=MAX_ENTRIES)){checkbox.checked=this.selectedIds.has(entry.id);return;}
        if(checkbox.checked)this.selectedIds.add(entry.id);else this.selectedIds.delete(entry.id);this.notify();this.render();
      });
      label.append(checkbox,title);
      const remove=this.document.createElement('button');remove.type='button';remove.className='quiet';remove.textContent='Remove';remove.setAttribute('aria-label',`Remove ${entry.title||'selected passage'}`);
      remove.addEventListener('click',()=>void this.remove(entry.id));row.append(label);if(!generic)row.append(remove);card.append(row);
      this.image(card,entry);
      this.provenance(card,entry);
      const details=this.document.createElement('details'),summary=this.document.createElement('summary'),text=this.document.createElement('p');
      summary.textContent=generic?'Read record':'Read saved story';text.className='archive-story-text';text.textContent=entry.text;details.append(summary,text);card.append(details);list.append(card);
    }
    this.controls();
  }
  setTurn(turn_id){
    if(!Number.isInteger(turn_id)||turn_id<=0)return;
    if(turn_id!==this.turnId){this.resetSources();this.turnId=turn_id;}
  }
  resetSources(){
    this.turnId=0;this._sourceMatches=[];this.get('archive-reply-sources').hidden=true;this.get('archive-sources-list').replaceChildren();
    this.get('archive-sources-summary').textContent='Sources for this reply';this.get('archive-sources-note').textContent='';
    this.get('archive-reply-sources').open=false;
    this.get('archive-artworks').hidden=true;this.get('archive-artworks-list').replaceChildren();
  }
  showSources(event){
    if(event.type!=='archive_sources'||!this.enabled||!this.turnId||event.turn_id!==this.turnId||event.scope!==this.scope||!Array.isArray(event.matches))return false;
    if(event.collection_ids!==undefined&&(!Array.isArray(event.collection_ids)||event.collection_ids.length>8||event.collection_ids.some(id=>!validCollectionId(id))))return false;
    const incoming=collectionIds(event.collection_ids);
    if(this.collectionIds.length?(incoming.length!==this.collectionIds.length||!this.collectionIds.every(id=>incoming.includes(id))):incoming.length)return false;
    const seen=new Set();
    const matches=event.matches.filter(entry=>entry&&validId(entry.id)&&!seen.has(recordKey(entry))&&(this.scope==='library'||this.selectedIds.has(entry.id))
      &&(this.collectionIds.length?this.collectionIds.includes(entry.collection_id):!entry.collection_id||entry.collection_id==='qtm')
      &&typeof (entry.excerpt??entry.text)==='string'&&entryURL(entry)!==null&&(seen.add(recordKey(entry)),true)).slice(0,3).map(entry=>({
        id:entry.id,title:typeof entry.title==='string'?entry.title.slice(0,120):'Saved passage',excerpt:(entry.excerpt??entry.text).slice(0,12000),
        source_url:entryURL(entry)||'',source_entry_id:typeof entry.source_entry_id==='string'?entry.source_entry_id.slice(0,128):null,
        collection_id:validCollectionId(entry.collection_id)?entry.collection_id:'qtm',collection_label:typeof entry.collection_label==='string'?entry.collection_label.slice(0,80):'',
        source_kind:entryKind(entry),provenance:['fetched','imported'].includes(entry.provenance)?entry.provenance:'pasted',
        image_url:publicArchiveURL(entry.image_url)||'',image_alt:typeof entry.image_alt==='string'?entry.image_alt.slice(0,300):'',metadata:metadata(entry.metadata)}));
    this._sourceMatches=matches;
    const list=this.get('archive-sources-list');list.replaceChildren();
    for(const entry of matches){
      const card=this.document.createElement('article'),title=this.document.createElement('h3'),text=this.document.createElement('p');
      card.className='archive-source';title.textContent=typeof entry.title==='string'?entry.title.slice(0,120):'Saved passage';
      text.className='archive-story-text';text.textContent=(entry.excerpt??entry.text).slice(0,12000);card.append(title,text);this.provenance(card,entry);list.append(card);
    }
    this.renderArtworks(event.image_request===true?[]:matches);
    const size=count(event.selected_count),generic=this.collectionIds.length>0,collection=generic?'your selected collections':this.scope==='library'?'your local archive':'your selected stories';
    this.get('archive-sources-summary').textContent=`Sources for this reply · ${matches.length}`;
    this.get('archive-sources-note').textContent=matches.length
      ?`Passages from ${collection} (${size.toLocaleString()} ${generic?'records':'stories'}). These excerpts are source material, not the character's memories.`
      :size?`No matching passage from ${collection} was supplied for this reply.`:generic?'No records were available in the selected collections for this call.':this.scope==='library'?'No stories were available in the archive for this call.':'No stories were selected for this call.';
    this.get('archive-reply-sources').hidden=false;return true;
  }
}
