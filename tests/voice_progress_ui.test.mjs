import test from 'node:test';
import assert from 'node:assert/strict';
import {VoiceProgress,formatVoiceMetrics} from '../frontend/live-voice-progress.mjs';

function setup(){
  const states=[],timings=[],texts=[];
  const progress=new VoiceProgress({onState:value=>states.push(value),onTiming:value=>timings.push(value),onText:value=>texts.push(value)});
  return {progress,states,timings,texts};
}

test('canonical text replaces the provisional reply once actor streaming has finished',()=>{
  const {progress,texts}=setup();progress.begin(4);
  const content={textContent:'An obsolete streamed beginning.',set innerHTML(_){throw new Error('Do not interpret model text as HTML.');}};
  progress.onText=value=>{content.textContent=value;texts.push(value);};
  const canonical='I can help. <script>This remains literal text.</script>';
  assert.equal(progress.accept({type:'text_done',turn_id:4,text:canonical}),true);
  assert.equal(content.textContent,canonical);assert.deepEqual(texts,[canonical]);
  for(const text of ['', '   ', null, 7, 'x'.repeat(16001)])assert.equal(progress.accept({type:'text_done',turn_id:4,text}),false);
  assert.equal(content.textContent,canonical);
});

test('voice phases show fixed generation and checking labels without microphone or playback side effects',()=>{
  const {progress,states}=setup();let physicalEffects=0;
  progress.onResumeMicrophone=()=>physicalEffects++;progress.onPlayback=()=>physicalEffects++;
  progress.begin(1);
  for(const [stage,attempt] of [['generating',1],['checking',1],['retrying',2],['generating',2],['checking',2],['ready',2]]){
    assert.equal(progress.accept({type:'voice_progress',turn_id:1,stage,attempt,
      message:'PRIVATE_MODEL_TEXT',synthesis_ms:1500,verification_ms:50,first_audio_ms:200}),true);
  }
  assert.deepEqual(states,['Generating voice… · Mic paused','Checking speech… · Mic paused','Retrying voice… · Mic paused',
    'Generating voice… · Mic paused','Checking speech… · Mic paused','Voice ready… · Mic paused']);
  assert.equal(physicalEffects,0);assert.equal(states.some(value=>value.includes('Speaking')||value.includes('PRIVATE')),false);
});

test('old turn events and older attempts cannot overwrite the current reply, status or timings',()=>{
  const {progress,states,timings,texts}=setup();progress.begin(7);
  assert.equal(progress.accept({type:'voice_progress',turn_id:7,stage:'checking',attempt:2}),true);
  for(const turn_id of [6,8,'7',0,null]){
    for(const type of ['voice_progress','metrics','text_done'])assert.equal(progress.accept({type,turn_id,stage:'ready',attempt:2,text:'STALE'}),false);
  }
  assert.equal(progress.accept({type:'voice_progress',turn_id:7,stage:'ready',attempt:1}),false);
  assert.equal(progress.accept({type:'voice_progress',turn_id:7,stage:'generating',attempt:2}),false);
  assert.equal(progress.begin(6),false);assert.deepEqual(states,['Checking speech… · Mic paused']);
  assert.deepEqual(timings,['']);assert.deepEqual(texts,[]);
});

test('new turns and same-turn voice retries clear timing while preserving the existing dialogue',()=>{
  const {progress,timings,texts}=setup();progress.begin(2);
  progress.accept({type:'text_done',turn_id:2,text:'The existing answer.'});
  progress.accept({type:'metrics',turn_id:2,voice_failed:true,voice_synthesis_ms:4000,voice_attempts:2});
  assert.match(timings.at(-1),/Voice failed/);
  progress.finish(2);progress.begin(2);assert.equal(timings.at(-1),'');assert.deepEqual(texts,['The existing answer.']);
  assert.equal(progress.accept({type:'voice_progress',turn_id:2,stage:'generating',attempt:1}),true);
  progress.begin(3);assert.equal(timings.at(-1),'');assert.equal(progress.accept({type:'metrics',turn_id:2,voice_synthesis_ms:999}),false);
  progress.clear();assert.equal(progress.turnId,0);assert.equal(progress.accept({type:'metrics',turn_id:3,voice_synthesis_ms:999}),false);
  assert.equal(progress.begin(1),true);
});

test('success, interruption and errors stop late phase changes while allowing final current-turn failure timings',()=>{
  for(const reason of ['success','interruption','error']){
    const {progress,states,timings,texts}=setup();progress.begin(3);progress.finish(3);
    assert.equal(progress.accept({type:'voice_progress',turn_id:3,stage:'checking',attempt:1}),false,reason);
    assert.equal(progress.accept({type:'text_done',turn_id:3,text:'Late replacement'}),false,reason);
    assert.equal(progress.accept({type:'metrics',turn_id:3,voice_failed:true,voice_attempts:2,voice_verification_ms:900}),true,reason);
    assert.deepEqual(states,[]);assert.deepEqual(texts,[]);assert.match(timings.at(-1),/^Voice failed/);
  }
});

test('malformed phases or attempt counters do not create a voice status',()=>{
  const {progress,states}=setup();progress.begin(1);
  for(const attempt of [0,-1,3,Infinity,NaN,1.5,'1',true,null])assert.equal(progress.accept({type:'voice_progress',turn_id:1,stage:'generating',attempt}),false);
  for(const stage of ['PRIVATE_TEXT','__proto__',null,{},[]])assert.equal(progress.accept({type:'voice_progress',turn_id:1,stage,attempt:1}),false);
  assert.deepEqual(states,[]);
});

test('metrics distinguish actual synthesis and checking from model first bytes and server delivery',()=>{
  const value=formatVoiceMetrics({speech_end_to_first_audio_ms:3200,endpoint_ms:300,stt_ms:200,streaming_asr:true,
    endpoint_reason:'smart_turn',director_ms:100,llm_total_ms:400,tts_first_chunk_ms:2200,
    voice_synthesis_ms:1500,voice_verification_ms:700,voice_attempts:2,voice_model_first_audio_ms:350});
  assert.equal(value,'Server to audio delivery: 3200 ms · Final pause: 300 ms · Recognition wait: 200 ms (streaming) · Endpoint: Smart Turn · Director: 100 ms · Reply text: 400 ms · Voice generation: 1500 ms · Speech checking: 700 ms · Voice attempts: 2 · Model first bytes: 350 ms');
  assert.doesNotMatch(value,/audible|playback|Voice preparation|Complete voice take/i);
});

test('failure-only timing data omits absent actor and endpoint totals without undefined or NaN',()=>{
  const value=formatVoiceMetrics({voice_failed:true,voice_synthesis_ms:9000,voice_verification_ms:1200,voice_attempts:2});
  assert.equal(value,'Voice failed · Voice generation: 9000 ms · Speech checking: 1200 ms · Voice attempts: 2');
  assert.doesNotMatch(value,/undefined|NaN|Final pause|Director|Reply text|Server to/);
  assert.equal(formatVoiceMetrics({voice_failed:true}),'Voice failed');assert.equal(formatVoiceMetrics({}), '');
});

test('older metrics remain readable and zero counters remain visible',()=>{
  const value=formatVoiceMetrics({speech_end_to_first_audio_ms:1000,endpoint_ms:400,stt_ms:0,recognition_prepared:true,
    context_check_ms:0,llm_first_token_ms:30,tts_first_chunk_ms:570});
  assert.equal(value,'Server to audio delivery: 1000 ms · Final pause: 400 ms · Recognition wait: 0 ms (started during pause) · Director: 0 ms · Reply text: 30 ms · Voice preparation: 570 ms');
  assert.equal(formatVoiceMetrics({voice_retry:true,voice_synthesis_ms:0,voice_verification_ms:0,voice_attempts:1}),
    'Voice retry · Voice generation: 0 ms · Speech checking: 0 ms · Voice attempts: 1');
});

test('only bounded integer counters and known endpoint labels can enter the timing line',()=>{
  const fields=['speech_end_to_first_audio_ms','endpoint_ms','stt_ms','director_ms','context_check_ms','llm_total_ms','llm_first_token_ms',
    'tts_first_chunk_ms','voice_synthesis_ms','voice_verification_ms','voice_model_first_audio_ms','voice_attempts'];
  for(const invalid of [-1,Infinity,NaN,Number.MAX_SAFE_INTEGER,1.25,'PRIVATE_COUNTER',true,{},null]){
    const data=Object.fromEntries(fields.map(key=>[key,invalid]));data.endpoint_reason='PRIVATE_ENDPOINT';data.message='PRIVATE_MESSAGE';data.text='PRIVATE_TEXT';
    assert.equal(formatVoiceMetrics(data),'');
  }
  assert.equal(formatVoiceMetrics({voice_attempts:3,endpoint_ms:15001,voice_verification_ms:120001,voice_model_first_audio_ms:120001}), '');
  assert.equal(formatVoiceMetrics({voice_synthesis_ms:0,voice_verification_ms:0,voice_model_first_audio_ms:0}),
    'Voice generation: 0 ms · Speech checking: 0 ms · Model first bytes: 0 ms');
});
