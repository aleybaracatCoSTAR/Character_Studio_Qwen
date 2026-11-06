import {displayArchiveImageURL,publicArchiveURL} from './live-archive.mjs';

const validTurn=id=>Number.isSafeInteger(id)&&id>0;
const text=(value,limit)=>typeof value==='string'?value.slice(0,limit):'';
const validId=value=>typeof value==='string'&&/^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$/.test(value);
const validCollection=value=>typeof value==='string'&&/^[a-z0-9][a-z0-9-]{0,63}$/.test(value);
function inConversation(container,node){
  if(!node||container.isConnected===false||node.isConnected===false)return false;
  if(typeof container.contains==='function')return container.contains(node);
  for(let depth=0;node&&depth<20;depth++,node=node.parentNode??node.parent)if(node===container)return true;
  return false;
}

export function artworkImageViewURL(value){
  const url=publicArchiveURL(value);if(!url)return null;
  const gallery=/^https:\/\/api\.nga\.gov\/iiif\/([a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})\/full\/!200,200\/0\/default\.jpg$/.exec(url);
  return gallery?`https://api.nga.gov/iiif/${gallery[1]}/full/!1200,1200/0/default.jpg`:displayArchiveImageURL(value);
}

// Inline archive images are part of the conversation, so they survive the
// next turn. Only an explicit link click opens a separate image view.
export class ArtworkChat {
  constructor({document,container,empty,onImageStatus=()=>{}}){
    this.document=document;this.container=container;this.empty=empty;this.onImageStatus=onImageStatus;
    this.turnId=0;this.rendered=new Map();this.current=null;this.statusEpoch=0;this.statusBatch=null;
  }
  setTurn(turnId){
    if(!validTurn(turnId)||turnId<this.turnId)return false;
    if(turnId!==this.turnId){this.current=null;this.statusBatch=null;this.statusEpoch++;}
    this.turnId=turnId;return true;
  }
  disconnect(){this.turnId=0;this.current=null;this.statusBatch=null;this.statusEpoch++;}
  reset(){
    for(const node of this.rendered.values())node.remove();
    this.rendered.clear();this.turnId=0;this.current=null;this.statusBatch=null;this.statusEpoch++;
  }
  hasCurrent(){
    const current=this.current;
    return !!current&&current.turnId===this.turnId&&validTurn(this.turnId)&&inConversation(this.container,current.anchor);
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
      if(seen.has(id)||!source)continue;
      seen.add(id);matches.push({entry,image,source});if(matches.length===3)break;
    }
    const node=this.document.createElement('section');node.className='message assistant artwork-message';node.setAttribute('aria-label','Requested archive images');
    node.setAttribute('data-turn-id',String(event.turn_id));
    const speaker=this.document.createElement('span');speaker.className='speaker';speaker.textContent='Archive images';node.append(speaker);
    const note=this.document.createElement('p');note.className='artwork-message-note';
    const imageCount=matches.filter(match=>match.image).length;
    note.textContent=text(event.image_message,500)||(!matches.length?'No image was available in the retrieved records for this request.':!imageCount?'These catalogue records have no image link in this import. You can open their original museum sources.':imageCount<matches.length?'These catalogue matches include records without an image link. Open an image or its original source below.':matches.length>1?'These archive images match your request. Choose an image to open it separately.':'Image from the retrieved archive record.');node.append(note);
    const batch={turnId:this.turnId,epoch:this.statusEpoch,total:imageCount,loaded:0,failed:0,settled:new Set(),published:false,last:null};
    let anchor=null,imageIndex=0;
    if(matches.length){
      const list=this.document.createElement('div');list.className='artwork-chat-cards';
      for(const {entry,image,source} of matches){
        const index=image?imageIndex++:null;
        const card=this.card(entry,image,source,result=>this.imageSettled(batch,index,result));if(image&&!anchor)anchor=card;list.append(card);
      }node.append(list);
    }
    this.rendered.set(key,node);this.container.append(node);if(this.empty)this.empty.hidden=true;
    batch.node=node;batch.published=true;this.statusBatch=batch;this.imageStatus(batch);
    this.current=anchor?{turnId:this.turnId,anchor}:null;
    if(!this.scrollForReply())this.container.scrollTop=this.container.scrollHeight;return true;
  }
  imageSettled(batch,index,result){
    if(!Number.isInteger(index)||index<0||index>=batch.total||!['loaded','unavailable'].includes(result)||batch.settled.has(index))return;
    batch.settled.add(index);if(result==='loaded')batch.loaded++;else batch.failed++;
    this.imageStatus(batch);
  }
  imageStatus(batch){
    if(!batch.published||this.statusBatch!==batch||batch.epoch!==this.statusEpoch||batch.turnId!==this.turnId
        ||!validTurn(this.turnId)||!inConversation(this.container,batch.node))return;
    const status=batch.loaded?'loaded':batch.failed===batch.total?'unavailable':'loading';
    const state=status+':'+batch.loaded+':'+batch.total;if(state===batch.last)return;
    batch.last=state;
    try{this.onImageStatus({type:'image_status',turn_id:batch.turnId,status,loaded_count:batch.loaded,total_count:batch.total});}catch(error){/* Image display must survive a closed or unavailable status connection. */}
  }
  link(label,url){
    const anchor=this.document.createElement('a');anchor.textContent=label;anchor.href=url;anchor.target='_blank';anchor.rel='noopener noreferrer';return anchor;
  }
  card(entry,imageURL,sourceURL,onSettled=()=>{}){
    const card=this.document.createElement('article');card.className='artwork-chat-card';
    const title=text(entry.title,120)||'Archive record',fallback=this.document.createElement('p');fallback.className='hint small';
    if(imageURL){
      const image=this.document.createElement('img');image.alt=text(entry.image_alt,300)||title;image.loading='lazy';image.decoding='async';image.referrerPolicy='no-referrer';
      fallback.textContent='This image could not be loaded. You can still open its original source.';fallback.hidden=true;
      image.addEventListener('load',()=>onSettled('loaded'));
      image.addEventListener('error',()=>{image.hidden=true;fallback.hidden=false;onSettled('unavailable');});image.src=imageURL;card.append(image);
    }else fallback.textContent='No image link in this imported record.';
    card.append(fallback);
    const heading=this.document.createElement('h3');heading.textContent=title;card.append(heading);
    const caption=this.document.createElement('p');caption.className='artwork-chat-caption';
    caption.textContent=[text(entry.metadata?.artist,200),text(entry.metadata?.date,100),text(entry.metadata?.medium,200)].filter(Boolean).join(' · ');if(caption.textContent)card.append(caption);
    const collection=text(entry.collection_label,80);if(collection){const label=this.document.createElement('p');label.className='artwork-chat-collection';label.textContent=collection;card.append(label);}
    const actions=this.document.createElement('div');actions.className='artwork-chat-actions';
    if(imageURL)actions.append(this.link('Open image',artworkImageViewURL(entry.image_url)||imageURL));
    actions.append(this.link('Original source',sourceURL));card.append(actions);
    const rights=text(entry.metadata?.rights,1000);if(rights){const notice=this.document.createElement('p');notice.className='archive-rights';notice.textContent=rights;card.append(notice);}
    return card;
  }
}
