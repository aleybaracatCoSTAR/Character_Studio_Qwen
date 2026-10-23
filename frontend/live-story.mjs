// Script editing stays outside the live character biography and conversation history.
const emotions=['neutral','calm','excited','sad','angry','anxious','whisper'];
export class StoryEditor {
  constructor(container,onChange){this.container=container;this.onChange=onChange;}
  load(beats){this.container.replaceChildren();for(const beat of beats)this.add(beat,false);}
  values(){return [...this.container.children].map(card=>Object.fromEntries([...card.querySelectorAll('[data-beat-field]')].map(input=>[input.dataset.beatField,input.type==='number'?Number(input.value):input.value])));}
  add(beat={},notify=true){
    if(this.container.children.length>=12)return;
    const card=document.createElement('details');card.className='beat-editor-card';card.open=this.container.children.length===0;
    const top=document.createElement('summary');top.className='beat-editor-top';
    const number=document.createElement('strong');number.textContent=`${this.container.children.length+1}. ${beat.title||'New beat'}`;
    const remove=document.createElement('button');remove.type='button';remove.className='quiet';remove.textContent='Remove';
    remove.addEventListener('click',event=>{event.preventDefault();event.stopPropagation();card.remove();this.renumber();this.onChange();});top.append(number,remove);card.append(top);
    const field=(key,label,kind,max,fallback)=>{
      const wrap=document.createElement('label');wrap.textContent=label;
      const input=document.createElement(kind==='textarea'?'textarea':kind==='select'?'select':'input');
      if(kind==='select')for(const emotion of emotions){const option=document.createElement('option');option.value=emotion;option.textContent=emotion;input.append(option);}
      else if(kind==='number'){input.type='number';input.min=key==='max_seconds'?'5':'0';input.max='1800';input.step='1';}
      else if(max)input.maxLength=max;
      if(kind==='textarea')input.rows=2;
      input.dataset.beatField=key;input.value=beat[key]??fallback??'';
      input.addEventListener('input',()=>{this.renumber();this.onChange();});wrap.append(input);return wrap;
    };
    card.append(field('title','Title','text',80,'New beat'),field('goal','What needs to happen?','textarea',400),field('note','Private entry note','textarea',500));
    const detail=document.createElement('details'),summary=document.createElement('summary');summary.textContent='Feeling & timing';detail.append(summary);
    detail.append(field('emotion','Feeling','select',0,'neutral'),field('delivery','Vocal direction','textarea',200));
    const times=document.createElement('div');times.className='voice-controls';
    times.append(field('min_seconds','Minimum seconds','number',0,15),field('max_seconds','Maximum seconds','number',0,90));detail.append(times);card.append(detail);
    this.container.append(card);if(notify)this.onChange();
  }
  renumber(){[...this.container.children].forEach((card,index)=>card.querySelector('strong').textContent=`${index+1}. ${card.querySelector('[data-beat-field=title]').value||'New beat'}`);}
}
