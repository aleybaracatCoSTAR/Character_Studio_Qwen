// Interim words are temporary listening feedback, never conversation history.
export class ListeningFeedback {
  constructor({label,partial,onFinalizing=()=>{},onListening=()=>{},onError=()=>{}}){
    this.label=label;this.partial=partial;this.onFinalizing=onFinalizing;
    this.onListening=onListening;this.onError=onError;this.enabled=false;this.reset();
  }
  setMode(mode){
    this.enabled=mode==='nemotron-smart-turn';
    this.label.hidden=!this.enabled;
    this.label.textContent=this.enabled?'Experimental listening · Nemotron + Smart Turn':'';
    if(!this.enabled)this.clear();
  }
  clear(){this.phase='idle';this.partial.textContent='';this.partial.hidden=true;}
  reset(){this.inputId=0;this.clear();}
  accept(event){
    if(!this.enabled||!['listening_state','listening_partial'].includes(event?.type)
        ||!Number.isSafeInteger(event.input_id)||event.input_id<1||event.input_id<this.inputId)return false;
    if(event.type==='listening_partial'){
      if(event.input_id!==this.inputId||this.phase!=='listening'||typeof event.text!=='string'||event.text.length>4000)return false;
      this.partial.textContent=event.text?'Hearing: '+event.text:'';this.partial.hidden=!event.text;return true;
    }
    if(!['listening','finalizing','idle','error'].includes(event.phase))return false;
    this.inputId=event.input_id;this.clear();this.phase=event.phase;
    if(event.phase==='finalizing')this.onFinalizing();
    if(event.phase==='listening'||event.phase==='idle')this.onListening();
    if(event.phase==='error')this.onError(typeof event.message==='string'?event.message.slice(0,400):'Listening failed. Try again.');
    return true;
  }
}
