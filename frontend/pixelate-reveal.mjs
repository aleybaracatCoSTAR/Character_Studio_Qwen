// A short, local canvas reveal. Cross-origin artwork may taint the canvas,
// which is harmless here: no pixels are read, exported or uploaded.
export class PixelateReveal{
  constructor({canvas,image,reducedMotion,requestFrame,cancelFrame,now,duration=900}={}){
    this.canvas=canvas;this.image=image;this.destroyed=false;this.generation=0;
    this.frame=null;this.pending=null;this.source='';this.buffer=null;
    this.opacity=image?.style?.opacity??'';
    this.requestFrame=requestFrame??globalThis.requestAnimationFrame?.bind(globalThis);
    this.cancelFrame=cancelFrame??globalThis.cancelAnimationFrame?.bind(globalThis);
    this.now=now??(()=>globalThis.performance?.now?.()??Date.now());
    this.reducedMotion=typeof reducedMotion==='function'?reducedMotion:
      ()=>reducedMotion??Boolean(globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches);
    this.duration=Number.isFinite(duration)?Math.max(100,Math.min(3000,duration)):900;
    this.loaded=()=>{
      const pending=this.pending;
      if(!pending||pending.generation!==this.generation||!this.matches(pending.source))return;
      this.pending=null;this.start(pending.generation,pending.source);
    };
    this.failed=()=>{
      if(this.destroyed)return;
      const expected=this.pending?.source||this.source;
      if(expected&&!this.matches(expected))return;
      this.pending=null;this.cancel();
    };
    this.image?.addEventListener?.('load',this.loaded);
    this.image?.addEventListener?.('error',this.failed);
    this.canvas?.setAttribute?.('aria-hidden','true');
    this.original();
  }

  sourceOfImage(){return this.image?.currentSrc||this.image?.src||'';}
  normalURL(value){
    try{return new URL(value,this.image?.ownerDocument?.baseURI||globalThis.location?.href).href;}
    catch{return String(value);}
  }
  matches(source){return this.normalURL(this.sourceOfImage())===this.normalURL(source);}
  ready(){return Boolean(this.image?.complete&&this.image.naturalWidth>0&&this.image.naturalHeight>0);}
  original(){
    if(this.canvas)this.canvas.hidden=true;
    if(this.image?.style)this.image.style.opacity=this.opacity;
  }
  cancel(){
    this.generation+=1;
    if(this.frame!==null){this.cancelFrame?.(this.frame);this.frame=null;}
    this.original();
  }

  // Use show() when this component owns image loading. For an image whose
  // load handler already exists, call revealLoaded() there instead.
  show(url,{alt}={}){
    if(this.destroyed||!this.image||typeof url!=='string'||!url.trim())return false;
    this.cancel();
    const generation=this.generation,source=url.trim();
    this.pending={generation,source};
    if(typeof alt==='string')this.image.alt=alt;
    if(this.normalURL(this.image.src||'')!==this.normalURL(source))this.image.src=source;
    if(this.ready()&&this.matches(source))this.loaded();
    return true;
  }

  revealLoaded(){
    if(this.destroyed)return false;
    this.cancel();this.pending=null;
    return this.start(this.generation,this.sourceOfImage());
  }
  replay(){return this.revealLoaded();}
  clear(){
    this.pending=null;this.source='';this.cancel();
  }

  start(generation,source){
    if(this.destroyed||!this.ready()||!this.matches(source))return false;
    if(this.reducedMotion()||typeof this.requestFrame!=='function')return false;
    try{
      const context=this.canvas?.getContext?.('2d');
      if(!context)return false;
      const document=this.canvas.ownerDocument||this.image.ownerDocument||globalThis.document;
      this.buffer??=document?.createElement?.('canvas');
      const tiny=this.buffer?.getContext?.('2d');
      if(!tiny)return false;
      this.source=source;
      this.canvas.hidden=false;
      this.paint(context,tiny,0);
      // Hide the original only after a mosaic has actually been drawn.
      // Opacity keeps the image's layout and alternative text available.
      if(this.image.style)this.image.style.opacity='0';
      const started=this.now();
      const tick=()=>{
        if(this.destroyed||generation!==this.generation)return;
        this.frame=null;
        if(!this.matches(source)||!this.ready()||this.reducedMotion()){
          this.cancel();return;
        }
        const elapsed=Math.max(0,this.now()-started);
        const progress=Math.min(1,elapsed/this.duration);
        try{
          this.paint(context,tiny,progress);
          if(progress===1){this.original();return;}
          this.frame=this.requestFrame(tick);
        }catch{this.cancel();}
      };
      this.frame=this.requestFrame(tick);
      return true;
    }catch{
      this.cancel();return false;
    }
  }

  paint(context,tiny,progress){
    const image=this.image,canvas=this.canvas;
    const bounds=canvas.getBoundingClientRect?.();
    const imageBounds=image.getBoundingClientRect?.();
    const width=bounds?.width||canvas.clientWidth||imageBounds?.width||image.clientWidth||canvas.width||image.naturalWidth;
    const height=bounds?.height||canvas.clientHeight||imageBounds?.height||image.clientHeight||canvas.height||image.naturalHeight;
    const ratio=Math.max(1,Math.min(2,Number(canvas.ownerDocument?.defaultView?.devicePixelRatio||globalThis.devicePixelRatio)||1));
    const scale=Math.min(ratio,4096/Math.max(width,height));
    const backingWidth=Math.max(1,Math.round(width*scale)),backingHeight=Math.max(1,Math.round(height*scale));
    if(canvas.width!==backingWidth)canvas.width=backingWidth;
    if(canvas.height!==backingHeight)canvas.height=backingHeight;
    const fit=Math.min(backingWidth/image.naturalWidth,backingHeight/image.naturalHeight);
    const drawWidth=image.naturalWidth*fit,drawHeight=image.naturalHeight*fit;
    const x=(backingWidth-drawWidth)/2,y=(backingHeight-drawHeight)/2;
    context.clearRect(0,0,backingWidth,backingHeight);
    if(progress>=1){
      context.imageSmoothingEnabled=true;
      context.drawImage(image,x,y,drawWidth,drawHeight);
      return;
    }
    const sourceMax=Math.max(image.naturalWidth,image.naturalHeight);
    const maximum=Math.max(8,Math.min(sourceMax,Math.max(drawWidth,drawHeight)));
    const cells=Math.min(maximum,Math.max(1,Math.round(8*Math.pow(maximum/8,progress))));
    this.buffer.width=Math.max(1,Math.round(image.naturalWidth/sourceMax*cells));
    this.buffer.height=Math.max(1,Math.round(image.naturalHeight/sourceMax*cells));
    tiny.imageSmoothingEnabled=true;
    tiny.drawImage(image,0,0,this.buffer.width,this.buffer.height);
    context.imageSmoothingEnabled=false;
    context.drawImage(this.buffer,0,0,this.buffer.width,this.buffer.height,x,y,drawWidth,drawHeight);
  }

  destroy(){
    if(this.destroyed)return;
    this.destroyed=true;this.clear();
    this.image?.removeEventListener?.('load',this.loaded);
    this.image?.removeEventListener?.('error',this.failed);
    this.buffer=null;
  }
}
