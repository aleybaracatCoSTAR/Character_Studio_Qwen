import {displayArchiveImageURL, publicArchiveURL} from './live-archive.mjs';
import {artworkImageViewURL} from './live-artwork.mjs';
import {PixelateReveal} from './pixelate-reveal.mjs';

const text=(value,max=1000)=>typeof value==='string'?value.slice(0,max):'';
const validId=value=>typeof value==='string'&&/^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$/.test(value);
const validCollection=value=>typeof value==='string'&&/^[a-z0-9][a-z0-9-]{0,63}$/.test(value);
const key=entry=>entry.collection_id+'|'+entry.id;
export function galleryRecords(entries){
  const seen=new Set();
  return (Array.isArray(entries)?entries:[]).filter(entry=>entry&&validId(entry.id)&&validCollection(entry.collection_id)
    &&publicArchiveURL(entry.source_url)&&!seen.has(key(entry))&&(seen.add(key(entry)),true)).slice(0,24).map(entry=>({
      id:entry.id,collection_id:entry.collection_id,title:text(entry.title,300)||'Archive record',
      collection_label:text(entry.collection_label,100),source_url:publicArchiveURL(entry.source_url),
      image_url:publicArchiveURL(entry.image_url)||'',image_alt:text(entry.image_alt,500),
      description:text(entry.excerpt??entry.text,4000),
      metadata:Object.fromEntries(['artist','date','medium','rights','dimensions'].map(name=>[name,text(entry.metadata?.[name])]))
    }));
}

// This view consumes the same vetted retrieval events as the original archive.
// Browsing is independent: choosing a thumbnail never sends a hidden model turn.
export class ArchiveGallery{
  constructor({document,fetcher=(...args)=>fetch(...args),collections=()=>[],onImageStatus=()=>{},revealFactory=options=>new PixelateReveal(options)}){
    this.document=document;this.fetcher=fetcher;this.collections=collections;this.onImageStatus=onImageStatus;
    this.records=[];this.selected=null;this.turnId=0;this.request=null;this.searchEpoch=0;this.imageEpoch=0;this.controller=null;this.disposed=false;
    this.mount();
    this.reveal=revealFactory({image:this.get('gallery-image'),canvas:this.get('gallery-canvas')});
    this.get('gallery-replay').addEventListener('click',()=>this.reveal.replay());
    this.get('gallery-search-form').addEventListener('submit',event=>{event.preventDefault();void this.search(this.get('gallery-search').value);});
  }
  get(id){return this.document.getElementById(id);}
  node(tag,className,content){const node=this.document.createElement(tag);if(className)node.className=className;if(content)node.textContent=content;return node;}
  mount(){
    const doc=this.document;doc.body.classList.add('gallery-page');doc.title='Character Studio · Archive gallery';
    const style=this.node('link');style.rel='stylesheet';style.href='/static/gallery.css';doc.head.append(style);
    const main=doc.querySelector('main'),aside=main.querySelector('aside'),conversation=main.querySelector('.conversation'),header=doc.querySelector('body > header');
    const settings=this.node('dialog');settings.id='gallery-settings';settings.setAttribute('aria-labelledby','gallery-settings-title');
    const settingsHeading=this.node('div','inspection-heading'),settingsTitle=this.node('h2','','Character & collections');settingsTitle.id='gallery-settings-title';
    const close=this.node('button','secondary','Done');close.type='button';close.addEventListener('click',()=>settings.close());settingsHeading.append(settingsTitle,close);settings.append(settingsHeading,aside);doc.body.append(settings);
    const left=this.node('div','gallery-header-left'),back=this.node('a','gallery-back','← Archive');back.href='/archive/';
    const open=this.node('button','quiet','Character & collections');open.id='gallery-open-settings';open.type='button';open.setAttribute('aria-haspopup','dialog');open.addEventListener('click',()=>settings.showModal());left.append(back,open);
    const brand=this.node('a','gallery-header-title','Character Studio');brand.href='/';
    const right=this.node('div','gallery-header-right');right.append(this.get('status'));header.replaceChildren(left,brand,right);
    const rail=this.node('nav','gallery-rail');rail.setAttribute('aria-label','Artworks in this visit');rail.append(this.node('span','gallery-rail-label','YOUR VISIT'));
    const thumbs=this.node('div');thumbs.id='gallery-thumbnails';rail.append(thumbs);
    const stage=this.node('section','gallery-stage');stage.setAttribute('aria-label','Selected artwork');
    const top=this.node('div','gallery-stage-top'),search=this.node('form','gallery-search');search.id='gallery-search-form';search.setAttribute('role','search');
    const input=this.node('input');input.id='gallery-search';input.type='search';input.maxLength=200;input.placeholder='Find an artist, artwork or subject…';input.setAttribute('aria-label','Search saved art collections');
    const submit=this.node('button','quiet','Search');submit.type='submit';search.append(input,submit);
    const replay=this.node('button','quiet','↻ Replay reveal');replay.id='gallery-replay';replay.type='button';replay.disabled=true;top.append(search,replay);
    const status=this.node('p','hint small');status.id='gallery-search-status';status.setAttribute('role','status');stage.append(top,status);
    const picture=this.node('figure','gallery-picture');picture.hidden=true;
    const image=this.node('img');image.id='gallery-image';image.decoding='async';image.referrerPolicy='no-referrer';
    const canvas=this.node('canvas');canvas.id='gallery-canvas';canvas.hidden=true;canvas.setAttribute('aria-hidden','true');picture.append(image,canvas);
    const empty=this.node('article');empty.id='gallery-empty';empty.append(this.node('span','gallery-kicker','THE ARCHIVE, IN CONVERSATION'),this.node('h1','','Let a conversation lead you to an artwork.'),this.node('p','','Ask your character about the collection, or search above. Retrieved artworks will unfold here.'));
    const caption=this.node('p');caption.id='gallery-caption';caption.setAttribute('aria-live','polite');stage.append(empty,picture,caption);
    const column=this.node('div','gallery-conversation'),details=this.node('section','gallery-details');details.setAttribute('aria-label','Artwork details');
    details.append(this.node('p','gallery-kicker','IN THE COLLECTION'));
    for(const [id,tag,content] of [['gallery-title','h2','A closer look'],['gallery-meta','p',''],['gallery-collection','p',''],['gallery-description','p','Choose an artwork to see its catalogue details and original museum source.']]){const node=this.node(tag,'',content);node.id=id;details.append(node);}
    const source=this.node('a','','View museum record ↗');source.id='gallery-source';source.target='_blank';source.rel='noopener noreferrer';source.hidden=true;details.append(source);
    const rights=this.node('p');rights.id='gallery-rights';
    const catalogue=this.node('details','gallery-catalogue'),catalogueLabel=this.node('summary','','Catalogue notes & rights');
    catalogue.append(catalogueLabel,details.querySelector('#gallery-description'),rights);details.append(catalogue);
    const tools=conversation.querySelector('.conversation-tools');if(tools){const options=this.node('details','gallery-session-options'),summary=this.node('summary','','Session options');options.append(summary,tools);conversation.querySelector('.conversation-heading').after(options);}
    const hint=this.get('empty-state');hint.replaceChildren(this.node('h3','','What catches your eye?'),this.node('p','','Talk about an artwork, a feeling or a question. Images found in the collection appear beside our conversation.'));
    column.append(details,conversation);main.className='gallery-shell';main.replaceChildren(rail,stage,column);
  }
  setTurn(id){if(Number.isSafeInteger(id)&&id>0&&id>=this.turnId){this.turnId=id;this.request=null;}}
  disconnect(){this.turnId=0;this.request=null;}
  reset(){
    this.searchEpoch++;this.controller?.abort();this.imageEpoch++;this.imageCleanup?.();this.reveal.clear();this.turnId=0;this.request=null;this.selected=null;this.records=[];
    this.get('gallery-thumbnails').replaceChildren();this.get('gallery-image').removeAttribute('src');this.get('gallery-image').hidden=true;
    this.get('gallery-canvas').hidden=true;this.get('gallery-image').parentNode.hidden=true;this.get('gallery-empty').hidden=false;this.get('gallery-replay').disabled=true;
    this.get('gallery-title').textContent='A closer look';this.get('gallery-title').title='';this.get('gallery-title').removeAttribute('data-long-title');this.get('gallery-description').textContent='Choose an artwork to see its catalogue details and original museum source.';
    this.get('gallery-empty').replaceChildren(this.node('span','gallery-kicker','THE ARCHIVE, IN CONVERSATION'),this.node('h1','','Let a conversation lead you to an artwork.'),this.node('p','','Ask your character about the collection, or search above. Retrieved artworks will unfold here.'));
    for(const id of ['gallery-meta','gallery-collection','gallery-rights','gallery-caption','gallery-search-status'])this.get(id).textContent='';this.get('gallery-source').hidden=true;
  }
  accept(event,matches){
    if(event?.type!=='archive_sources'||!this.turnId||event.turn_id!==this.turnId)return false;
    const records=galleryRecords(matches);if(!records.length)return false;
    // A newer retrieval supersedes a slower manual search, but ordinary text
    // updates and duplicate source events never replay the animation.
    this.searchEpoch++;this.controller?.abort();
    this.get('gallery-search-status').textContent=`${records.length} record${records.length===1?'':'s'} retrieved in this conversation`;
    this.request=event.image_request===true?{turn:this.turnId,keys:new Set(records.filter(item=>item.image_url).map(key)),last:''}:null;
    if(this.request&&!this.request.keys.size){try{this.onImageStatus({type:'image_status',turn_id:this.turnId,status:'unavailable',loaded_count:0,total_count:0});}catch(error){}}
    this.add(records);const selected=records.find(item=>item.image_url)||records[0];
    this.select(selected);return true;
  }
  add(records){
    const fresh=new Map(records.map(entry=>[key(entry),entry]));
    this.records=[...records,...this.records.filter(entry=>!fresh.has(key(entry)))].slice(0,24);this.renderThumbs();
  }
  renderThumbs(){
    const list=this.get('gallery-thumbnails'),top=list.scrollTop||0,left=list.scrollLeft||0,focused=this.document.activeElement?.getAttribute?.('data-gallery-key');list.replaceChildren();
    for(const entry of this.records){
      const button=this.node('button','gallery-thumbnail');button.type='button';button.title=entry.title;button.setAttribute('aria-label','View '+entry.title);button.setAttribute('aria-pressed',String(key(entry)===this.selected));
      button.setAttribute('data-gallery-key',key(entry));
      const url=displayArchiveImageURL(entry.image_url);
      if(url){const image=this.node('img');image.src=url;image.alt='';image.loading='lazy';image.referrerPolicy='no-referrer';image.addEventListener('error',()=>{image.hidden=true;});button.append(image);}
      else button.append(this.node('span','gallery-no-thumbnail','◇'));
      button.append(this.node('small','',entry.title));button.addEventListener('click',()=>this.select(entry));list.append(button);
    }
    if(focused)for(const button of list.children)if(button.getAttribute('data-gallery-key')===focused)button.focus?.({preventScroll:true});
    list.scrollTop=top;list.scrollLeft=left;
  }
  select(entry){
    if(this.disposed)return false;const id=key(entry),same=id===this.selected&&this.imageURL===entry.image_url;
    this.selected=id;this.imageURL=entry.image_url;this.renderThumbs();
    this.get('gallery-title').textContent=entry.title;this.get('gallery-title').title=entry.title;this.get('gallery-title').setAttribute('data-long-title',String(entry.title.length>75));
    this.get('gallery-meta').textContent=[entry.metadata.artist,entry.metadata.date,entry.metadata.medium].filter(Boolean).join(' · ');
    this.get('gallery-collection').textContent=entry.collection_label;
    this.get('gallery-description').textContent=entry.description;this.get('gallery-description').hidden=!entry.description;
    this.get('gallery-rights').textContent=entry.metadata.rights;
    const source=this.get('gallery-source');source.href=entry.source_url;source.hidden=false;
    this.get('gallery-caption').textContent=entry.title;
    const image=this.get('gallery-image'),empty=this.get('gallery-empty'),picture=image.parentNode;
    if(!entry.image_url){this.imageEpoch++;this.imageCleanup?.();this.reveal.clear();picture.hidden=true;empty.hidden=false;empty.replaceChildren(this.node('span','gallery-kicker','CATALOGUE RECORD'),this.node('h1','',entry.title),this.node('p','','No image link is available in this imported record. You can still explore its details or open the museum source.'));this.get('gallery-replay').disabled=true;this.report(entry,'unavailable');return true;}
    if(same&&image.complete&&image.naturalWidth){this.report(entry,'loaded');return true;}
    if(same&&image.complete&&!image.naturalWidth){this.report(entry,'unavailable');return true;}
    if(same&&!image.complete){this.report(entry,'loading');return true;}
    const epoch=++this.imageEpoch;picture.hidden=false;image.hidden=false;empty.hidden=true;this.get('gallery-replay').disabled=true;
    const loaded=()=>{if(epoch!==this.imageEpoch||this.disposed)return;this.get('gallery-replay').disabled=false;this.report(entry,'loaded');};
    const failed=()=>{if(epoch!==this.imageEpoch||this.disposed)return;picture.hidden=true;empty.hidden=false;empty.replaceChildren(this.node('span','gallery-kicker','IMAGE UNAVAILABLE'),this.node('h1','',entry.title),this.node('p','','The museum image could not be loaded. Its catalogue details and original source are still available.'));this.get('gallery-replay').disabled=true;this.report(entry,'unavailable');};
    if(this.imageCleanup)this.imageCleanup();
    image.addEventListener('load',loaded);image.addEventListener('error',failed);this.imageCleanup=()=>{image.removeEventListener('load',loaded);image.removeEventListener('error',failed);};
    const url=artworkImageViewURL(entry.image_url)||displayArchiveImageURL(entry.image_url);
    this.report(entry,'loading');this.reveal.show(url,{alt:entry.image_alt||entry.title});
    if(image.complete&&new URL(image.currentSrc||image.src,this.document.baseURI).href===new URL(url,this.document.baseURI).href){if(image.naturalWidth)loaded();else failed();}
    return true;
  }
  report(entry,status){
    const batch=this.request;if(!batch||batch.turn!==this.turnId||!batch.keys.has(key(entry)))return;
    // The stage displays one image at a time. Unselected thumbnail candidates
    // are not pending central images, and their absence must not hold this
    // request in 'loading' after the selected museum image fails.
    const token=key(entry)+':'+status;if(token===batch.last)return;batch.last=token;
    try{this.onImageStatus({type:'image_status',turn_id:batch.turn,status,loaded_count:status==='loaded'?1:0,total_count:1});}catch(error){/* A closed call must not break gallery browsing. */}
  }
  async search(query=''){
    const epoch=++this.searchEpoch;this.controller?.abort();this.controller=new AbortController();this.request=null;
    const ids=[...new Set(this.collections().filter(validCollection))].slice(0,8),status=this.get('gallery-search-status');
    if(!ids.length){status.textContent='No saved art collections yet. Open Character & collections to add one.';return false;}
    status.textContent='Searching saved collections…';
    try{
      const response=await this.fetcher('/api/archive/collections/entries?'+new URLSearchParams({ids:ids.join(','),q:text(query,200),limit:'18',search_mode:'literal'}),{signal:this.controller.signal});
      if(!response.ok)throw new Error('Search unavailable');const result=await response.json();
      if(epoch!==this.searchEpoch||this.disposed)return false;const records=galleryRecords(result.entries);
      status.textContent=records.length?`${records.length} catalogue matches · choose a thumbnail`:'No matching artworks in these saved collections. Try an artist or a shorter title.';
      if(records.length){this.add(records);this.select(records.find(entry=>entry.image_url)||records[0]);}return true;
    }catch(error){if(epoch===this.searchEpoch&&!this.disposed)status.textContent='The saved collections could not be searched. Check the server and try again.';return false;}
  }
  destroy(){this.disposed=true;this.searchEpoch++;this.controller?.abort();this.imageEpoch++;this.imageCleanup?.();this.reveal.destroy();}
}
