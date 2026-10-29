const MAP='https://www.queeringthemap.com/';
const LICENSE='https://creativecommons.org/licenses/by-nc-sa/4.0/';
const MAX_ENTRIES=50;
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

// This panel manages the explicitly selected server collection. It never
// persists story text in the browser or treats the whole map as searchable.
export class ArchivePanel {
  constructor({document,fetch=(...args)=>globalThis.fetch(...args),onChange=()=>{}}){
    this.document=document;this.fetch=fetch;this.onChange=onChange;
    this.entries=[];this.selectedIds=new Set();this.loaded=false;this.busy=false;this.pending=false;this.profileEpoch=0;this.turnId=0;
    this.dialog=this.get('archive-dialog');
    this.get('archive-enabled').addEventListener('change',()=>{
      if(this.busy){this.get('archive-enabled').checked=this.enabled;return;}
      this.enabled=this.get('archive-enabled').checked;this.notify();
    });
    this.get('archive-add').addEventListener('click',()=>this.open());
    this.get('archive-close').addEventListener('click',()=>this.dialog.close());
    this.dialog.addEventListener('close',()=>this.clearForm());
    this.get('archive-entry-form').addEventListener('submit',event=>{event.preventDefault();void this.submit();});
    this.get('archive-reload').addEventListener('click',()=>void this.load());
    this.enabled=false;this.apply({enabled:false,entry_ids:[]});
  }
  get(id){return this.document.getElementById(id);}
  settings(){return {enabled:!!this.enabled,entry_ids:[...this.selectedIds]};}
  notify(){this.onChange(this.settings());}
  apply(settings={}){
    this.profileEpoch++;
    this.enabled=!!settings.enabled;this.get('archive-enabled').checked=this.enabled;
    this.selectedIds=new Set((Array.isArray(settings.entry_ids)?settings.entry_ids:[]).filter(validId).slice(0,MAX_ENTRIES));
    if(this.loaded)this.filterSelection();
    if(this.dialog.open)this.dialog.close();else this.clearForm();
    this.resetSources();this.render();
  }
  setBusy(busy){
    this.busy=!!busy;if(this.busy&&this.dialog.open)this.dialog.close();this.controls();
  }
  controls(){
    const blocked=this.busy||this.pending;
    this.get('archive-enabled').disabled=blocked;
    this.get('archive-add').disabled=blocked||this.entries.length>=MAX_ENTRIES;
    this.get('archive-submit').disabled=blocked;
    this.get('archive-reload').disabled=blocked;
    for(const node of this.get('archive-entries').querySelectorAll('input,button'))node.disabled=blocked;
    for(const id of ['archive-entry-id','archive-paste-title','archive-paste-text','archive-paste-source'])this.get(id).disabled=blocked;
  }
  clearForm(){
    for(const id of ['archive-entry-id','archive-paste-title','archive-paste-text','archive-paste-source'])this.get(id).value='';
    this.get('archive-paste').open=false;this.get('archive-dialog-error').textContent='';
  }
  open(){
    if(this.busy||this.pending||this.entries.length>=MAX_ENTRIES)return false;
    this.clearForm();if(!this.dialog.open)this.dialog.showModal();return true;
  }
  async response(path,options){
    let response;
    try{response=await this.fetch(path,{redirect:'error',...options});}
    catch(error){throw new Error('Could not reach the archive server. Check the local server and try again.');}
    if(!response.ok)throw new Error(response.status===404
      ?'Archive tools need the latest server. Update, restart and refresh; your character is unchanged.'
      :'The story could not be saved or loaded. Check the entry and try again.');
    try{return await response.json();}
    catch(error){throw new Error('The archive server returned an unreadable response. Please try again.');}
  }
  setEntries(entries){
    if(!Array.isArray(entries))throw new Error('The archive server returned an unreadable story list.');
    const seen=new Set();
    this.entries=entries.filter(entry=>entry&&validId(entry.id)&&typeof entry.text==='string'&&!seen.has(entry.id)
      &&(seen.add(entry.id),true)).slice(0,MAX_ENTRIES).map(entry=>({id:entry.id,title:typeof entry.title==='string'?entry.title:'Selected passage',
        text:entry.text,source_url:archiveSourceURL(entry.source_url)||'',source_entry_id:typeof entry.source_entry_id==='string'?entry.source_entry_id:null,
        provenance:entry.provenance==='fetched'?'fetched':'pasted'}));
    this.loaded=true;
  }
  filterSelection(){
    const available=new Set(this.entries.map(entry=>entry.id));
    this.selectedIds=new Set([...this.selectedIds].filter(id=>available.has(id)));
  }
  async load(){
    if(this.pending||this.busy)return false;
    this.pending=true;this.controls();
    try{
      const result=await this.response('/api/archive/entries');this.setEntries(result.entries);
      const before=JSON.stringify([...this.selectedIds]);this.filterSelection();
      if(before!==JSON.stringify([...this.selectedIds]))this.notify();
      this.get('archive-status').textContent='';this.get('archive-reload').hidden=true;return true;
    }catch(error){this.get('archive-status').textContent=error.message;this.get('archive-reload').hidden=false;return false;}
    finally{this.pending=false;this.render();}
  }
  async submit(){
    if(this.busy||this.pending)return false;
    if(this.entries.length>=MAX_ENTRIES){this.get('archive-dialog-error').textContent='The collection is full. Remove a story before adding another.';return false;}
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
      this.setEntries(result.entries);
      if(epoch===this.profileEpoch&&!this.busy&&validId(result.entry?.id)){
        this.selectedIds.add(result.entry.id);this.filterSelection();this.notify();
        if(this.dialog.open)this.dialog.close();this.get('archive-status').textContent='Story added and selected.';
      }else this.get('archive-status').textContent=this.busy?'Story added to the collection. End the call to change the selection.':'Story added to the collection. Select it for this character when you are ready.';
      return true;
    }catch(error){
      this.get('archive-dialog-error').textContent=error.message;
      if(!this.dialog.open)this.get('archive-status').textContent=error.message;
      return false;
    }
    finally{this.pending=false;this.render();}
  }
  async remove(id){
    if(this.busy||this.pending||!this.entries.some(entry=>entry.id===id))return false;
    this.pending=true;this.controls();
    try{
      const result=await this.response(`/api/archive/entries/${encodeURIComponent(id)}`,{method:'DELETE'});
      this.setEntries(result.entries);this.filterSelection();this.notify();
      this.get('archive-status').textContent='Story removed from the collection.';return true;
    }catch(error){this.get('archive-status').textContent=error.message;return false;}
    finally{this.pending=false;this.render();}
  }
  link(container,label,url){
    const anchor=this.document.createElement('a');anchor.textContent=label;anchor.href=url;anchor.target='_blank';anchor.rel='noopener noreferrer';container.append(anchor);
  }
  provenance(container,entry){
    const source=archiveSourceURL(entry.source_url);
    const fetched=entry.provenance==='fetched';
    const label=this.document.createElement('p');label.className='hint small archive-provenance';
    label.textContent=fetched?'Queering the Map · CC BY-NC-SA 4.0 · fetched entry':source?'Pasted passage · unverified source · Queering the Map · CC BY-NC-SA 4.0':'Pasted passage · supplied by you';container.append(label);
    const links=this.document.createElement('div');links.className='archive-links';
    if(source)this.link(links,fetched&&entry.source_entry_id?'Original entry':'Supplied source link',source);
    if(entry.source_entry_id||source){this.link(links,'Open map',MAP);this.link(links,'License',LICENSE);}
    container.append(links);
  }
  render(){
    this.get('archive-count').textContent=`${this.selectedIds.size} of ${this.entries.length} stories selected`;
    this.get('archive-empty').hidden=!!this.entries.length;
    this.get('archive-limit').hidden=this.entries.length<MAX_ENTRIES;
    const list=this.get('archive-entries');list.replaceChildren();
    for(const entry of this.entries){
      const card=this.document.createElement('li');card.className='archive-entry';
      const row=this.document.createElement('div');row.className='archive-entry-row';
      const label=this.document.createElement('label'),checkbox=this.document.createElement('input'),title=this.document.createElement('span');
      checkbox.type='checkbox';checkbox.checked=this.selectedIds.has(entry.id);title.textContent=entry.title||'Selected passage';
      checkbox.addEventListener('change',()=>{
        if(this.busy||this.pending){checkbox.checked=this.selectedIds.has(entry.id);return;}
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
    if(event.type!=='archive_sources'||!this.enabled||!this.turnId||event.turn_id!==this.turnId||event.scope!=='selected'||!Array.isArray(event.matches))return false;
    const matches=event.matches.filter(entry=>entry&&validId(entry.id)&&this.selectedIds.has(entry.id)&&typeof (entry.excerpt??entry.text)==='string').slice(0,5);
    const list=this.get('archive-sources-list');list.replaceChildren();
    for(const entry of matches){
      const card=this.document.createElement('article'),title=this.document.createElement('h3'),text=this.document.createElement('p');
      card.className='archive-source';title.textContent=typeof entry.title==='string'?entry.title:'Selected passage';
      text.className='archive-story-text';text.textContent=entry.excerpt??entry.text;card.append(title,text);this.provenance(card,entry);list.append(card);
    }
    const count=Number.isInteger(event.selected_count)&&event.selected_count>=0?Math.min(event.selected_count,MAX_ENTRIES):0;
    this.get('archive-sources-summary').textContent=`Sources for this reply · ${matches.length}`;
    this.get('archive-sources-note').textContent=matches.length
      ?`Passages from your selected collection (${count} stories). These excerpts are source material, not the character's memories.`
      :count?'No matching passage from your selected stories was supplied for this reply.':'No stories were selected for this call.';
    this.get('archive-reply-sources').hidden=false;return true;
  }
}
