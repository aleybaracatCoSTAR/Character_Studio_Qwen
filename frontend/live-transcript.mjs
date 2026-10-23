// Turn numbers restart at 1 for every call. Clear their DOM records before a new call.
export class Conversation {
  constructor(container,empty){this.container=container;this.empty=empty;this.replies=new Map();}
  reset(){
    this.replies.clear();
    for(const node of [...this.container.children])if(node!==this.empty)node.remove();
    this.empty.hidden=false;
  }
  message(speaker,text,kind='assistant'){
    this.empty.hidden=true;
    const node=document.createElement('div');node.className=`message ${kind}`;
    const label=document.createElement('span');label.className='speaker';label.textContent=speaker;
    const content=document.createElement('div');content.textContent=text;node.append(label,content);this.container.append(node);
    this.container.scrollTop=this.container.scrollHeight;return {node,content};
  }
}
