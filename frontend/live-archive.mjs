const MAP='https://www.queeringthemap.com/';
const LICENSE='https://creativecommons.org/licenses/by-nc-sa/4.0/';
const MAX_ENTRIES=50;
const PAGE_SIZE=25;
export const MAX_ARCHIVE_IMPORT_BYTES=128*1024*1024;
const count=value=>Number.isSafeInteger(value)&&value>=0?value:0;
const validId=value=>typeof value==='string'&&/^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$/.test(value);

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
    this.entries=[];this.selectedIds=new Set();this.loaded=false;this.busy=false;this.pending=false;this.profileEpoch=0;this.turnId=0;
    this.libraryMode=false;this.total=0;this.matched=0;this.offset=0;this.limit=PAGE_SIZE;this.query='';
    this.scope='selected';this.refreshNeeded=false;this.selectionNeeded=false;
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
      this.scope=this.get('archive-scope').value==='library'?'library':'selected';this.resetSources();this.notify();this.render();
    });
    this.get('archive-search-submit').addEventListener('click',()=>void this.search());
    this.get('archive-search').addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();void this.search();}});
    this.get('archive-previous').addEventListener('click',()=>void this.load({offset:Math.max(0,this.offset-this.limit)}));
    this.get('archive-next').addEventListener('click',()=>void this.load({offset:this.offset+this.limit}));
    this.get('archive-import-file').addEventListener('change',()=>this.controls());
    this.get('archive-import').addEventListener('click',()=>void this.upload());
    this.enabled=false;this.apply({enabled:false,entry_ids:[]});
  }
  get(id){return this.document.getElementById(id);}
  settings(){return {enabled:!!this.enabled,scope:this.scope,entry_ids:[...this.selectedIds]};}
  notify(){this.onChange(this.settings());}
  apply(settings={}){
    this.profileEpoch++;
    this.enabled=!!settings.enabled;this.get('archive-enabled').checked=this.enabled;
    this.scope=settings.scope==='library'?'library':'selected';this.get('archive-scope').value=this.scope;
    this.selectedIds=new Set((Array.isArray(settings.entry_ids)?settings.entry_ids:[]).filter(validId).slice(0,MAX_ENTRIES));
    if(this.loaded&&!this.libraryMode)this.filterSelection();
    this.selectionNeeded=this.libraryMode;if(this.pending)this.refreshNeeded=true;
    if(this.dialog.open)this.dialog.close();else this.clearForm();
    this.get('archive-import-file').value='';this.resetSources();this.render();
    if(this.loaded&&this.libraryMode&&!this.busy&&!this.pending)void this.validateSelection();
  }
  setBusy(busy){
    this.busy=!!busy;if(this.busy&&this.dialog.open)this.dialog.close();this.controls();
    if(!this.busy&&!this.pending)this.followUp();
  }
  controls(){
    const blocked=this.busy||this.pending;
    this.get('archive-enabled').disabled=blocked;
    this.get('archive-add').disabled=blocked||(!this.libraryMode&&this.entries.length>=MAX_ENTRIES);
    this.get('archive-submit').disabled=blocked;
    this.get('archive-reload').disabled=blocked;
    for(const id of ['archive-scope','archive-search','archive-search-submit','archive-import-file'])this.get(id).disabled=blocked;
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
    if(this.busy||this.pending||(!this.libraryMode&&this.entries.length>=MAX_ENTRIES))return false;
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
  setEntries(entries){
    if(!Array.isArray(entries))throw new Error('The archive server returned an unreadable story list.');
    const seen=new Set();
    this.entries=entries.filter(entry=>entry&&validId(entry.id)&&typeof entry.text==='string'&&!seen.has(entry.id)
      &&(seen.add(entry.id),true)).slice(0,MAX_ENTRIES).map(entry=>({id:entry.id,title:typeof entry.title==='string'?entry.title.slice(0,120):'Saved passage',
        text:entry.text.slice(0,12000),source_url:archiveSourceURL(entry.source_url)||'',source_entry_id:typeof entry.source_entry_id==='string'?entry.source_entry_id:null,
        provenance:['fetched','imported'].includes(entry.provenance)?entry.provenance:'pasted'}));
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
    const path=!query&&!offset?'/api/archive/entries':'/api/archive/entries?'+new URLSearchParams({q:query,offset:String(offset),limit:String(PAGE_SIZE)});
    try{
      const result=await this.response(path);if(!this.current(epoch))return false;
      this.query=query;this.get('archive-search').value=query;this.setPage(result);
      if(this.libraryMode)await this.validateSelected(epoch);
      else{const before=JSON.stringify([...this.selectedIds]);this.filterSelection();if(before!==JSON.stringify([...this.selectedIds]))this.notify();}
      if(!this.current(epoch))return false;
      this.get('archive-status').textContent='';this.get('archive-reload').hidden=true;return true;
    }catch(error){if(this.current(epoch)){this.get('archive-status').textContent=error.message;this.get('archive-reload').hidden=false;}return false;}
    finally{this.finish();}
  }
  async search(){return this.load({offset:0,query:this.get('archive-search').value});}
  async submit(){
    if(this.busy||this.pending)return false;
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
    if(this.busy||this.pending)return false;
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
    if(this.busy||this.pending||!this.entries.some(entry=>entry.id===id))return false;
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
    const source=archiveSourceURL(entry.source_url);
    const fetched=entry.provenance==='fetched';
    const label=this.document.createElement('p');label.className='hint small archive-provenance';
    label.textContent=entry.provenance==='imported'?'Imported passage · not individually verified · Queering the Map · CC BY-NC-SA 4.0':fetched?'Queering the Map · CC BY-NC-SA 4.0 · fetched entry':source?'Pasted passage · unverified source · Queering the Map · CC BY-NC-SA 4.0':'Pasted passage · supplied by you';container.append(label);
    const links=this.document.createElement('div');links.className='archive-links';
    if(source)this.link(links,fetched&&entry.source_entry_id?'Original entry':'Supplied source link',source);
    if(entry.source_entry_id||source||entry.provenance==='imported'){this.link(links,'Open map',MAP);this.link(links,'License',LICENSE);}
    container.append(links);
  }
  render(){
    this.get('archive-count').textContent=this.scope==='library'?'Searching the whole local archive':this.libraryMode?`${this.selectedIds.size} stories selected`:`${this.selectedIds.size} of ${this.entries.length} stories selected`;
    this.get('archive-library-count').textContent=`${this.total.toLocaleString()} stories in your archive`;
    this.get('archive-page').textContent=this.matched?`${this.offset+1}–${Math.min(this.offset+this.entries.length,this.matched)} of ${this.matched.toLocaleString()}${this.query?' matches':' stories'}`:'0 results';
    this.get('archive-empty').hidden=!!this.entries.length;
    this.get('archive-empty').textContent=this.query?'No stories match this search.':'No stories added yet. Import a collection, choose an entry, or paste a passage.';
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
      remove.addEventListener('click',()=>void this.remove(entry.id));row.append(label,remove);card.append(row);
      this.provenance(card,entry);
      const details=this.document.createElement('details'),summary=this.document.createElement('summary'),text=this.document.createElement('p');
      summary.textContent='Read saved story';text.className='archive-story-text';text.textContent=entry.text;details.append(summary,text);card.append(details);list.append(card);
    }
    this.controls();
  }
  setTurn(turn_id){
    if(!Number.isInteger(turn_id)||turn_id<=0)return;
    if(turn_id!==this.turnId){this.resetSources();this.turnId=turn_id;}
  }
  resetSources(){
    this.turnId=0;this.get('archive-reply-sources').hidden=true;this.get('archive-sources-list').replaceChildren();
    this.get('archive-sources-summary').textContent='Sources for this reply';this.get('archive-sources-note').textContent='';
    this.get('archive-reply-sources').open=false;
  }
  showSources(event){
    if(event.type!=='archive_sources'||!this.enabled||!this.turnId||event.turn_id!==this.turnId||event.scope!==this.scope||!Array.isArray(event.matches))return false;
    const seen=new Set();
    const matches=event.matches.filter(entry=>entry&&validId(entry.id)&&!seen.has(entry.id)&&(this.scope==='library'||this.selectedIds.has(entry.id))&&typeof (entry.excerpt??entry.text)==='string'&&archiveSourceURL(entry.source_url)!==null&&(seen.add(entry.id),true)).slice(0,3);
    const list=this.get('archive-sources-list');list.replaceChildren();
    for(const entry of matches){
      const card=this.document.createElement('article'),title=this.document.createElement('h3'),text=this.document.createElement('p');
      card.className='archive-source';title.textContent=typeof entry.title==='string'?entry.title.slice(0,120):'Saved passage';
      text.className='archive-story-text';text.textContent=(entry.excerpt??entry.text).slice(0,12000);card.append(title,text);this.provenance(card,entry);list.append(card);
    }
    const size=count(event.selected_count),collection=this.scope==='library'?'your local archive':'your selected stories';
    this.get('archive-sources-summary').textContent=`Sources for this reply · ${matches.length}`;
    this.get('archive-sources-note').textContent=matches.length
      ?`Passages from ${collection} (${size.toLocaleString()} stories). These excerpts are source material, not the character's memories.`
      :size?`No matching passage from ${collection} was supplied for this reply.`:this.scope==='library'?'No stories were available in the archive for this call.':'No stories were selected for this call.';
    this.get('archive-reply-sources').hidden=false;return true;
  }
}
