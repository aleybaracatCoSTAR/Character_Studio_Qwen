import {displayArchiveImageURL,publicArchiveURL} from './live-archive.mjs';

const validTurn=id=>Number.isSafeInteger(id)&&id>0;
const text=(value,limit)=>typeof value==='string'?value.slice(0,limit):'';
const validId=value=>typeof value==='string'&&/^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$/.test(value);
const validCollection=value=>typeof value==='string'&&/^[a-z0-9][a-z0-9-]{0,63}$/.test(value);

export function artworkImageViewURL(value){
  const url=publicArchiveURL(value);if(!url)return null;
  const gallery=/^https:\/\/api\.nga\.gov\/iiif\/([a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})\/full\/!200,200\/0\/default\.jpg$/.exec(url);
  return gallery?`https://api.nga.gov/iiif/${gallery[1]}/full/!1200,1200/0/default.jpg`:displayArchiveImageURL(value);
}

// Inline archive images are part of the conversation, so they survive the
// next turn. Only an explicit link click opens a separate image view.
export class ArtworkChat {
  constructor({document,container,empty}){
    this.document=document;this.container=container;this.empty=empty;this.turnId=0;this.rendered=new Map();this.current=null;
  }
  setTurn(turnId){
    if(!validTurn(turnId)||turnId<this.turnId)return false;
    if(turnId!==this.turnId)this.current=null;
    this.turnId=turnId;return true;
  }
  disconnect(){this.turnId=0;this.current=null;}
  reset(){
    for(const node of this.rendered.values())node.remove();
    this.rendered.clear();this.turnId=0;this.current=null;
  }
  hasCurrent(){
    const current=this.current;
    if(!current||current.turnId!==this.turnId||!validTurn(this.turnId)||this.container.isConnected===false)return false;
    const anchor=current.anchor;
    if(!anchor||anchor.isConnected===false)return false;
    if(typeof this.container.contains==='function')return this.container.contains(anchor);
    // The small DOM fixtures expose `parent`; browsers expose `parentNode`.
    let parent=anchor;
    for(let depth=0;parent&&depth<20;depth++,parent=parent.parentNode??parent.parent){
      if(parent===this.container)return true;
    }
    return false;
  }
  scrollForReply(){
    if(!this.hasCurrent())return false;
    const anchor=this.current.anchor;
    let top;
    if(typeof anchor.getBoundingClientRect==='function'&&typeof this.container.getBoundingClientRect==='function'){
      const target=anchor.getBoundingClientRect(),viewport=this.container.getBoundingClientRect();
      if(Number.isFinite(target.top)&&Number.isFinite(viewport.top))
        top=(Number.isFinite(this.container.scrollTop)?this.container.scrollTop:0)+target.top-viewport.top-(this.container.clientTop||0);
    }
    if(!Number.isFinite(top)&&Number.isFinite(anchor.offsetTop))top=anchor.offsetTop;
    if(!Number.isFinite(top))return false;
    this.container.scrollTop=Math.max(0,top);return true;
  }
  accept(event,validatedMatches){
    if(event?.type!=='archive_sources'||event.image_request!==true||!validTurn(event.turn_id)||event.turn_id!==this.turnId||!Array.isArray(validatedMatches))return false;
    // A turn may carry a request identifier, but it still produces one inline
    // image response. Duplicate delivery cannot add the same cards twice.
    const request=typeof event.request_id==='string'?event.request_id.slice(0,128):Number.isSafeInteger(event.request_id)?String(event.request_id):'';
    const key=String(event.turn_id)+':'+request;
    if(this.rendered.has(key))return false;
    const seen=new Set(),matches=[];
    for(const entry of validatedMatches.slice(0,24)){
      if(!entry||!validId(entry.id)||!validCollection(entry.collection_id))continue;
      const id=entry.collection_id+'|'+entry.id,image=displayArchiveImageURL(entry.image_url),source=publicArchiveURL(entry.source_url);
      if(seen.has(id)||!image||!source)continue;
      seen.add(id);matches.push({entry,image,source});if(matches.length===3)break;
    }
    const node=this.document.createElement('section');node.className='message assistant artwork-message';node.setAttribute('aria-label','Requested archive images');
    node.setAttribute('data-turn-id',String(event.turn_id));
    const speaker=this.document.createElement('span');speaker.className='speaker';speaker.textContent='Archive images';node.append(speaker);
    const note=this.document.createElement('p');note.className='artwork-message-note';
    note.textContent=text(event.image_message,500)||(matches.length>1?'These archive images match your request. Choose an image to open it separately.':matches.length?'Image from the retrieved archive record.':'No image was available in the retrieved records for this request.');node.append(note);
    let anchor=null;
    if(matches.length){
      const list=this.document.createElement('div');list.className='artwork-chat-cards';
      for(const {entry,image,source} of matches){const card=this.card(entry,image,source);if(!anchor)anchor=card;list.append(card);}node.append(list);
    }
    this.rendered.set(key,node);this.container.append(node);if(this.empty)this.empty.hidden=true;
    this.current=anchor?{turnId:this.turnId,anchor}:null;
    if(!this.scrollForReply())this.container.scrollTop=this.container.scrollHeight;return true;
  }
  link(label,url){
    const anchor=this.document.createElement('a');anchor.textContent=label;anchor.href=url;anchor.target='_blank';anchor.rel='noopener noreferrer';return anchor;
  }
  card(entry,imageURL,sourceURL){
    const card=this.document.createElement('article');card.className='artwork-chat-card';
    const title=text(entry.title,120)||'Archive image',image=this.document.createElement('img'),fallback=this.document.createElement('p');
    image.src=imageURL;image.alt=text(entry.image_alt,300)||title;image.loading='lazy';image.decoding='async';image.referrerPolicy='no-referrer';
    fallback.className='hint small';fallback.textContent='This image could not be loaded. You can still open its original source.';fallback.hidden=true;
    image.addEventListener('error',()=>{image.hidden=true;fallback.hidden=false;});card.append(image,fallback);
    const heading=this.document.createElement('h3');heading.textContent=title;card.append(heading);
    const caption=this.document.createElement('p');caption.className='artwork-chat-caption';
    caption.textContent=[text(entry.metadata?.artist,200),text(entry.metadata?.date,100),text(entry.metadata?.medium,200)].filter(Boolean).join(' · ');if(caption.textContent)card.append(caption);
    const collection=text(entry.collection_label,80);if(collection){const label=this.document.createElement('p');label.className='artwork-chat-collection';label.textContent=collection;card.append(label);}
    const actions=this.document.createElement('div');actions.className='artwork-chat-actions';actions.append(this.link('Open image',artworkImageViewURL(entry.image_url)||imageURL),this.link('Original source',sourceURL));card.append(actions);
    const rights=text(entry.metadata?.rights,1000);if(rights){const notice=this.document.createElement('p');notice.className='archive-rights';notice.textContent=rights;card.append(notice);}
    return card;
  }
}
