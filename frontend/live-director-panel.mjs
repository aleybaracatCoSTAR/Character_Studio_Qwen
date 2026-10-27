// Server updates change the compact summary, never the user's disclosure state.
export class DirectorPanel {
  constructor({button,content,summary,empty}){
    this.button=button;this.content=content;this.summary=summary;this.empty=empty;
    this.expanded=false;this.hasStory=false;this.hasNote=false;
    button.setAttribute('aria-controls',content.id);
    button.addEventListener('click',()=>this.setExpanded(!this.expanded));
    this.setExpanded(false);this.updateStory({enabled:false});
  }
  setExpanded(expanded){
    this.expanded=!!expanded;this.content.hidden=!this.expanded;
    this.button.setAttribute('aria-expanded',String(this.expanded));
    this.button.textContent=this.expanded?'Hide director controls':'Show director controls';
  }
  updateStory(story){
    this.hasStory=!!story.enabled;
    let text='Director';
    if(this.hasStory){
      const progress=`${story.index+1}/${story.count}`;
      text=story.finished?`Story complete · ${progress}`:`${progress} · ${story.current?.title||'Director'}${story.paused?' · Held':''}`;
    }
    this.summary.textContent=text;this.summary.title=text;this.updateEmpty();
  }
  noteAvailable(){this.hasNote=true;this.updateEmpty();}
  resetNote(){this.hasNote=false;this.updateEmpty();}
  updateEmpty(){this.empty.hidden=this.hasStory||this.hasNote;}
}
