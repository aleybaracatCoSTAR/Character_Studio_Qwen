// A voice-only retry keeps the existing text/turn. Stale turn errors must not
// stop playback or change the recovery control for a newer reply.
export class VoiceRetry {
  constructor({button,send,onRetry,onError,onUnavailable=onError,canRetry}){
    this.button=button;this.send=send;this.onRetry=onRetry;this.onError=onError;this.onUnavailable=onUnavailable;this.canRetry=canRetry;
    this.failedTurn=null;this.clear();
    button.addEventListener('click',()=>this.retry());
  }
  clear(){
    this.failedTurn=null;this.button.hidden=true;this.button.disabled=true;
  }
  handleError(error,currentTurn){
    if(Object.hasOwn(error,'turn_id')&&(!Number.isInteger(error.turn_id)||error.turn_id!==currentTurn))return false;
    this.clear();
    if(error.can_retry_voice===true&&Number.isInteger(error.turn_id)&&error.turn_id>0&&this.canRetry()){
      this.failedTurn=error.turn_id;this.button.hidden=false;this.button.disabled=false;
    }
    this.onError(error);return true;
  }
  handleUnavailable(event,currentTurn){
    if(!Number.isInteger(event.turn_id)||event.turn_id!==currentTurn)return false;
    this.clear();this.onUnavailable(event);return true;
  }
  retry(){
    const turn=this.failedTurn;
    if(turn===null||!this.canRetry()){this.clear();return false;}
    this.clear();this.onRetry();this.send({type:'retry_voice',turn_id:turn});return true;
  }
}
