const STAGES={actor:'Character actor',director:'Director',trigger:'Emotion check',tts:'Drama 3 voice'};

const copied=value=>JSON.parse(JSON.stringify(value));
const printable=value=>typeof value==='string'?value:JSON.stringify(value,null,2);
const identifier=value=>Number.isSafeInteger(value)&&value>=0?String(value).padStart(3,'0'):'000';

export function requestFilename(request,extension='txt'){
  const stage=Object.hasOwn(STAGES,request?.stage)?request.stage:'request';
  const suffix=extension==='json'?'json':'txt';
  return `character-studio-turn-${identifier(request?.turn_id)}-${stage}-request-${identifier(request?.id)}.${suffix}`;
}

export function requestText(request){
  const snapshot=copied(request),payload=snapshot.payload??{};
  const lines=['CHARACTER STUDIO — MODEL INPUT SNAPSHOT',
    `Request: ${snapshot.id}`,`Turn: ${snapshot.turn_id}`,
    `Stage: ${STAGES[snapshot.stage]??snapshot.stage}`,`Model: ${snapshot.model??''}`,
    `Phase: ${snapshot.phase??'reply'}`,'',
    'This is the captured request. Voice-reference audio and connection details are omitted.'];
  if(snapshot.notice)lines.push('', 'REQUEST NOTICE',snapshot.notice);
  if(snapshot.omitted_fields?.length)lines.push('', 'OMITTED FIELDS',snapshot.omitted_fields.join(', '));
  if(snapshot.reference_audio)lines.push('', 'VOICE REFERENCE METADATA',printable(snapshot.reference_audio));
  if(Array.isArray(payload.messages))for(const [index,message] of payload.messages.entries()){
    lines.push('',`MESSAGE ${index+1} — ${String(message.role??'message').toUpperCase()}`,
      printable(message.content??''));
  }
  if(Object.hasOwn(payload,'input'))lines.push('', 'SPOKEN TEXT AND DELIVERY CUE',printable(payload.input));
  if(Object.hasOwn(payload,'ref_text'))lines.push('', 'VOICE REFERENCE TRANSCRIPT',printable(payload.ref_text));
  const settings=Object.fromEntries(Object.entries(payload).filter(([key])=>!['messages','input','ref_text'].includes(key)));
  lines.push('', 'GENERATION SETTINGS',printable(settings),'','REQUEST JSON',JSON.stringify(payload,null,2));
  return lines.join('\n')+'\n';
}

export function recentJSON(requests){
  return JSON.stringify({export_format:'character-studio-model-inputs',version:1,
    exported_at:new Date().toISOString(),scope:'recent_requests',
    retention:{max_requests:24,max_turns:5,max_characters:256000},
    note:'Only the recent captured requests are included. Explicit omission metadata is retained; voice-reference audio and connection details are omitted.',
    requests:copied(requests)},null,2)+'\n';
}

export function downloadFile(document,{filename,content,mime},
  {URL=globalThis.URL,Blob=globalThis.Blob,schedule=globalThis.setTimeout}={}){
  const type=/charset=/i.test(mime)?mime:`${mime};charset=utf-8`;
  const blob=new Blob([content],{type});
  const url=URL.createObjectURL(blob);
  let anchor;
  try{
    anchor=document.createElement('a');anchor.href=url;anchor.download=filename;anchor.hidden=true;
    document.body.append(anchor);anchor.click();
  }finally{
    try{anchor?.remove();}
    finally{
      // Keep the URL valid while the browser starts its download, then release
      // the memory. No request, recording or browser-storage entry is created.
      try{schedule(()=>URL.revokeObjectURL(url),1000);}
      catch(error){URL.revokeObjectURL(url);throw error;}
    }
  }
}

export async function downloadViaServer(document,file,
  {fetch=globalThis.fetch}={}){
  // An ordinary same-origin HTTP attachment works in browsers that do not
  // expose local Blob downloads. The server keeps only a small, expiring copy.
  let response,result;
  try{
    response=await fetch('/api/inspection/downloads',{
      method:'POST',redirect:'error',headers:{'Content-Type':'application/json'},body:JSON.stringify(file)});
  }catch(error){throw new Error('The model-input download could not be prepared.');}
  if(!response.ok)throw new Error(response.status===404
    ?'Update and restart the studio server to enable downloads.'
    :'The model-input download could not be prepared.');
  try{result=await response.json();}
  catch(error){throw new Error('The studio returned an invalid download link.');}
  if(!result||typeof result!=='object'||Array.isArray(result)
      ||typeof result.download_url!=='string'||!/^\/api\/inspection\/downloads\/[a-f0-9]{48}$/.test(result.download_url)){
    throw new Error('The studio returned an invalid download link.');
  }
  const anchor=document.createElement('a');
  try{
    anchor.href=result.download_url;anchor.download=file.filename;anchor.hidden=true;
    document.body.append(anchor);anchor.click();
  }finally{anchor.remove();}
}
