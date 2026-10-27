// Successful playback can dismiss a transient voice-generation notice without
// erasing a connection error or another useful message that replaced it.
export class CallFeedback {
  constructor(element){this.element=element;this.kind='general';}
  show(message='',kind='general'){
    this.element.textContent=message;this.kind=kind;
  }
  clearVoiceNotice(){
    if(this.kind==='voice_notice')this.show();
  }
}
