import test from 'node:test';
import assert from 'node:assert/strict';
import {PixelateReveal} from '../frontend/pixelate-reveal.mjs';

function setup({width=600,height=400,naturalWidth=1200,naturalHeight=600,reducedMotion=false,contextAvailable=true}={}){
  let time=0,next=0;
  const callbacks=new Map(),cancelled=[],allFrames=[];
  const operations=[];
  const context=name=>({name,imageSmoothingEnabled:true,
    clearRect(...args){operations.push({name,type:'clear',args});},
    drawImage(image,...args){operations.push({name,type:'draw',image,args,smoothing:this.imageSmoothingEnabled,
      sourceWidth:image.width,sourceHeight:image.height});},
    getImageData(){throw new Error('Pixel reads are forbidden.');}});
  const main=context('main'),tiny=context('tiny');
  const buffer={width:0,height:0,getContext:()=>tiny,toDataURL(){throw new Error('Canvas export is forbidden.');}};
  const document={baseURI:'https://studio.example/characters/',defaultView:{devicePixelRatio:1},
    createElement(tag){assert.equal(tag,'canvas');return buffer;}};
  const canvas={ownerDocument:document,hidden:false,width:300,height:150,
    getBoundingClientRect:()=>({width,height}),getContext:()=>contextAvailable?main:null,setAttribute(){},
    toDataURL(){throw new Error('Canvas export is forbidden.');}};
  const listeners=new Map();
  const image={ownerDocument:document,style:{opacity:''},src:'',currentSrc:'',complete:false,naturalWidth,naturalHeight,
    addEventListener(type,handler){if(!listeners.has(type))listeners.set(type,new Set());listeners.get(type).add(handler);},
    removeEventListener(type,handler){listeners.get(type)?.delete(handler);},
    dispatch(type){for(const handler of [...listeners.get(type)||[]])handler({target:this});},
    load(url=this.src){this.currentSrc=url;this.complete=true;this.dispatch('load');}};
  const reveal=new PixelateReveal({canvas,image,reducedMotion,now:()=>time,
    requestFrame(callback){const id=next++;callbacks.set(id,callback);allFrames.push(callback);return id;},
    cancelFrame(id){cancelled.push(id);callbacks.delete(id);}});
  return{reveal,canvas,image,buffer,main,tiny,operations,cancelled,callbacks,allFrames,listeners,
    tick(ms){time=ms;const frames=[...callbacks.values()];callbacks.clear();for(const frame of frames)frame(time);},
    resize(w,h){width=w;height=h;}};
}

test('a loaded image begins as a coarse mosaic and becomes the unmodified original',()=>{
  const env=setup();const{reveal,image,canvas,buffer,operations}=env;
  assert.equal(canvas.hidden,true);assert.equal(reveal.show('https://museum.example/art.jpg',{alt:'A painting.'}),true);
  assert.equal(image.alt,'A painting.');assert.equal(image.style.opacity,'');assert.equal(operations.length,0);
  image.load();assert.equal(canvas.hidden,false);assert.equal(image.style.opacity,'0');
  assert.deepEqual([buffer.width,buffer.height],[8,4]);
  let last=operations.at(-1);assert.equal(last.image,buffer);assert.equal(last.smoothing,false);
  env.tick(450);assert.ok(buffer.width>8&&buffer.width<600);
  env.tick(900);assert.equal(canvas.hidden,false);
  env.tick(1250);last=operations.at(-1);
  assert.equal(last.image,image);assert.equal(last.smoothing,true);
  assert.equal(canvas.hidden,true);assert.equal(image.style.opacity,'');assert.equal(env.callbacks.size,0);
});

test('the mosaic keeps the actual responsive frame and uses the same content bounds as the clear image',()=>{
  const env=setup({width:600,height:400});
  env.canvas.getBoundingClientRect=()=>({left:10,top:20,width:600,height:400});
  env.image.getBoundingClientRect=()=>({left:60,top:70,width:500,height:300});
  env.canvas.ownerDocument.defaultView.getComputedStyle=()=>({borderLeftWidth:'9px',borderRightWidth:'9px',borderTopWidth:'9px',borderBottomWidth:'9px',borderTopColor:'rgb(231, 221, 202)'});
  const frames=[];env.main.fillRect=(...args)=>frames.push(args);
  env.reveal.show('https://museum.example/frame.jpg');env.image.load();
  assert.deepEqual(frames[0],[50,50,500,300]);
  assert.deepEqual(env.operations.at(-1).args.slice(-4),[59,59,482,282]);
  env.tick(1250);
  assert.deepEqual(env.operations.at(-1).args,[59,59,482,282]);
  assert.equal(env.image.style.opacity,'');
});

test('contain geometry preserves wide and portrait artwork without stretching or crop',()=>{
  for(const dimensions of [[1200,600,[0,50,600,300]],[400,1200,[700/3,0,400/3,400]]]){
    const[naturalWidth,naturalHeight,expected]=dimensions;
    const env=setup({naturalWidth,naturalHeight});env.reveal.show('https://museum.example/art.jpg');env.image.load();
    const actual=env.operations.at(-1).args.slice(-4);
    actual.forEach((value,index)=>assert.ok(Math.abs(value-expected[index])<1e-8));
    assert.ok(Math.abs(actual[2]/actual[3]-naturalWidth/naturalHeight)<1e-8);
  }
});

test('resize recomputes letterboxing and clears pixels left by the old stage',()=>{
  const env=setup();env.reveal.show('https://museum.example/art.jpg');env.image.load();
  env.resize(300,600);env.tick(300);
  assert.deepEqual([env.canvas.width,env.canvas.height],[300,600]);
  assert.deepEqual(env.operations.at(-1).args.slice(-4),[0,225,300,150]);
  const clear=env.operations.filter(value=>value.type==='clear').at(-1);
  assert.deepEqual(clear.args,[0,0,300,600]);
});

test('reduced motion displays the original immediately and never requests animation',()=>{
  const env=setup({reducedMotion:true});env.reveal.show('https://museum.example/art.jpg');env.image.load();
  assert.equal(env.canvas.hidden,true);assert.equal(env.image.style.opacity,'');
  assert.equal(env.operations.length,0);assert.equal(env.callbacks.size,0);
});

test('a changing reduced-motion preference safely ends a reveal already in progress',()=>{
  let reduced=false;const env=setup({reducedMotion:()=>reduced});
  env.reveal.show('https://museum.example/art.jpg');env.image.load();reduced=true;env.tick(100);
  assert.equal(env.canvas.hidden,true);assert.equal(env.image.style.opacity,'');assert.equal(env.callbacks.size,0);
});

test('cached existing images reveal without reassigning their source or starting another request',()=>{
  const env=setup();const url='https://museum.example/cached.jpg';let assignments=0;
  env.image.currentSrc=url;env.image.complete=true;
  Object.defineProperty(env.image,'src',{get:()=>url,set:()=>assignments++});
  assert.equal(env.reveal.revealLoaded(),true);assert.equal(assignments,0);assert.equal(env.canvas.hidden,false);
  env.tick(1250);assert.equal(env.reveal.replay(),true);assert.equal(assignments,0);
  env.reveal.show(url);assert.equal(assignments,0);assert.equal(env.canvas.hidden,false);
});

test('an old load event or cancelled animation cannot overwrite a newer image',()=>{
  const env=setup();const first='https://museum.example/first.jpg',second='https://museum.example/second.jpg';
  env.reveal.show(first);env.image.load();const staleFrame=env.allFrames.at(-1);
  env.reveal.show(second);assert.deepEqual(env.cancelled,[0]);assert.equal(env.canvas.hidden,true);
  env.image.currentSrc=first;env.image.dispatch('load');
  const count=env.operations.length;assert.equal(env.canvas.hidden,true);assert.equal(env.callbacks.size,0);
  env.image.load(second);assert.equal(env.canvas.hidden,false);
  const currentCount=env.operations.length;staleFrame();
  assert.equal(env.operations.length,currentCount);assert.ok(currentCount>count);
  assert.equal(env.image.style.opacity,'0');env.tick(1250);assert.equal(env.image.style.opacity,'');
});

test('load failure, missing canvas and draw failure preserve the real image fallback',()=>{
  for(const mode of ['image-error','no-context','draw-error']){
    const env=setup({contextAvailable:mode!=='no-context'});
    env.image.style.opacity='';
    if(mode==='draw-error')env.main.drawImage=()=>{throw new Error('Unavailable canvas drawing.');};
    env.reveal.show('https://museum.example/art.jpg');
    if(mode==='image-error')env.image.dispatch('error');else env.image.load();
    assert.equal(env.canvas.hidden,true);assert.equal(env.image.style.opacity,'');assert.equal(env.callbacks.size,0);
  }
});

test('cross-origin artwork is drawn locally with no pixel reads, exports or crossOrigin change',()=>{
  const env=setup();env.image.crossOrigin='';
  env.reveal.show('https://other-museum.example/art.jpg');env.image.load();env.tick(450);env.tick(1250);
  assert.equal(env.image.crossOrigin,'');assert.equal(env.canvas.hidden,true);
  assert.ok(env.operations.some(operation=>operation.image===env.image));
});

test('destroy cancels RAF including id zero, restores opacity and removes all load handlers',()=>{
  const env=setup();env.reveal.opacity='.7';env.reveal.show('https://museum.example/art.jpg');env.image.load();
  const stale=env.allFrames.at(-1);env.reveal.destroy();
  assert.deepEqual(env.cancelled,[0]);assert.equal(env.image.style.opacity,'.7');assert.equal(env.canvas.hidden,true);
  assert.equal(env.listeners.get('load').size,0);assert.equal(env.listeners.get('error').size,0);
  const count=env.operations.length;stale();assert.equal(env.operations.length,count);
  assert.equal(env.reveal.replay(),false);assert.equal(env.reveal.show('https://museum.example/later.jpg'),false);
});

test('clear cancels both pending loading and old frames while allowing the next artwork',()=>{
  const env=setup();env.reveal.show('https://museum.example/first.jpg');env.image.load();
  const stale=env.allFrames.at(-1);env.reveal.clear();
  assert.equal(env.canvas.hidden,true);assert.equal(env.image.style.opacity,'');
  const count=env.operations.length;stale();assert.equal(env.operations.length,count);
  env.reveal.show('https://museum.example/pending.jpg');env.reveal.clear();env.image.load();
  assert.equal(env.canvas.hidden,true);assert.equal(env.operations.length,count);
  env.reveal.show('https://museum.example/next.jpg');env.image.load();
  assert.equal(env.canvas.hidden,false);assert.equal(env.image.style.opacity,'0');
});

test('an externally changed source stops the old animation rather than drawing a different image',()=>{
  const env=setup();env.reveal.show('https://museum.example/first.jpg');env.image.load();
  const count=env.operations.length;env.image.currentSrc='https://museum.example/new.jpg';env.tick(20);
  assert.equal(env.operations.length,count);assert.equal(env.canvas.hidden,true);assert.equal(env.image.style.opacity,'');
});

test('a mid-animation drawing failure restores the original and retains its alternative text',()=>{
  const env=setup();env.reveal.show('https://museum.example/art.jpg',{alt:'Original accessible description.'});env.image.load();
  env.tiny.drawImage=()=>{throw new Error('Decode or canvas failure.');};env.tick(200);
  assert.equal(env.canvas.hidden,true);assert.equal(env.image.style.opacity,'');
  assert.equal(env.image.alt,'Original accessible description.');assert.equal(env.callbacks.size,0);
});
