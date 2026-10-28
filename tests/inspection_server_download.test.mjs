import test from 'node:test';
import assert from 'node:assert/strict';
import {downloadViaServer} from '../frontend/live-inspection-export.mjs';

const token='a1'.repeat(24),path=`/api/inspection/downloads/${token}`;
const file={filename:'character-studio-turn-001-actor-request-002.txt',
  content:'SYSTEM\nYou are Anna.\nUSER\nMontréal café — what is my name?\n',mime:'text/plain;charset=utf-8'};

function setup({response,fetchError,fail}={}){
  const fetches=[],events=[],anchors=[];
  const document={
    createElement(name){
      assert.equal(name,'a');if(fail==='create')throw new Error('Create failed.');
      const anchor={href:'',download:'',hidden:false,
        click(){events.push(['click',this.href,this.download,this.hidden]);if(fail==='click')throw new Error('Click failed.');},
        remove(){events.push(['remove']);if(fail==='remove')throw new Error('Remove failed.');}};
      anchors.push(anchor);events.push(['create']);return anchor;
    },
    body:{append(anchor){events.push(['append',anchor]);if(fail==='append')throw new Error('Append failed.');}}
  };
  const fetch=async(url,options)=>{
    fetches.push({url,options});
    if(fetchError)throw fetchError;
    return response??{ok:true,status:200,json:async()=>({download_url:path})};
  };
  return {document,fetch,fetches,events,anchors};
}

test('server export posts exact JSON to the fixed relative endpoint before clicking the HTTP attachment',async()=>{
  const fixture=setup();await downloadViaServer(fixture.document,file,{fetch:fixture.fetch});
  assert.equal(fixture.fetches.length,1);
  const call=fixture.fetches[0];assert.equal(call.url,'/api/inspection/downloads');
  assert.equal(call.options.method,'POST');assert.deepEqual(call.options.headers,{'Content-Type':'application/json'});
  assert.deepEqual(JSON.parse(call.options.body),file);
  assert.equal(call.options.body,JSON.stringify(file));
  assert.deepEqual(fixture.events.map(event=>event[0]),['create','append','click','remove']);
  assert.deepEqual(fixture.events[2],['click',path,file.filename,true]);
  assert.equal(fixture.anchors[0].href,path);assert.equal(fixture.anchors[0].download,file.filename);
  assert.equal(fixture.fetches.some(call=>call.options.method==='GET'),false);
});

test('unexpected redirects are disabled for the prompt-bearing POST',async()=>{
  const fixture=setup();await downloadViaServer(fixture.document,file,{fetch:fixture.fetch});
  assert.equal(fixture.fetches[0].options.redirect,'error');
});

test('JSON exports preserve their filename, Unicode content and MIME',async()=>{
  const fixture=setup(),jsonFile={filename:'character-studio-model-inputs.json',content:'{"name":"ألي"}\n',mime:'application/json;charset=utf-8'};
  await downloadViaServer(fixture.document,jsonFile,{fetch:fixture.fetch});
  assert.deepEqual(JSON.parse(fixture.fetches[0].options.body),jsonFile);
  assert.equal(fixture.anchors[0].download,jsonFile.filename);
});

test('400 or 500 responses use a safe preparation error without reading or reflecting the response body',async()=>{
  for(const status of [400,403,500]){
    let bodyReads=0;
    const fixture=setup({response:{ok:false,status,
      json:async()=>{bodyReads++;return {detail:'PRIVATE SERVER OUTPUT'};},
      text:async()=>{bodyReads++;return '<script>PRIVATE SERVER OUTPUT</script>';}}});
    await assert.rejects(downloadViaServer(fixture.document,file,{fetch:fixture.fetch}),error=>{
      assert.equal(error.message,'The model-input download could not be prepared.');return true;
    });
    assert.equal(bodyReads,0);assert.deepEqual(fixture.anchors,[]);
  }
});

test('404 explains that the older server needs updating and creates no download link',async()=>{
  const fixture=setup({response:{ok:false,status:404,json:async()=>{throw new Error('Must not read body.');}}});
  await assert.rejects(downloadViaServer(fixture.document,file,{fetch:fixture.fetch}),/Update and restart the studio server/);
  assert.deepEqual(fixture.events,[]);
});

test('returned URLs must be the exact relative attachment path with a 48-character lowercase hex token',async()=>{
  const rejected=[
    `https://elsewhere.example${path}`,`//elsewhere.example${path}`,`http://127.0.0.1:18091${path}`,
    `javascript:alert(1)`,`data:text/html,private`,`blob:https://example.test/private`,
    `${path}?redirect=https://elsewhere.example`,`${path}#fragment`,`${path}/extra`,
    `/api/inspection/downloads/${'a'.repeat(47)}`,`/api/inspection/downloads/${'a'.repeat(49)}`,
    `/api/inspection/downloads/${'A'.repeat(48)}`,`/api/inspection/downloads/${'g'.repeat(48)}`,
    `/api/inspection/downloads/../${token}`,`/api/inspection/downloads/%2f${token}`,
    `${path}\n`,undefined,null,42,{href:path}
  ];
  for(const download_url of rejected){
    const fixture=setup({response:{ok:true,status:200,json:async()=>({download_url})}});
    await assert.rejects(downloadViaServer(fixture.document,file,{fetch:fixture.fetch}),/invalid download link/,
      `Unsafe or malformed URL: ${String(download_url)}`);
    assert.deepEqual(fixture.anchors,[]);assert.equal(fixture.fetches.length,1);
  }
});

test('malformed success bodies cannot create links or expose raw parse output',async()=>{
  const cases=[async()=>null,async()=>[],async()=>({}),async()=>{throw new Error('PRIVATE RESPONSE PARSE OUTPUT');}];
  for(const json of cases){
    const fixture=setup({response:{ok:true,status:200,json}});
    await assert.rejects(downloadViaServer(fixture.document,file,{fetch:fixture.fetch}),error=>{
      assert.doesNotMatch(error.message,/PRIVATE RESPONSE PARSE OUTPUT/);return true;
    });
    assert.deepEqual(fixture.anchors,[]);
  }
});

test('network errors become usable safe preparation errors instead of raw transport details',async()=>{
  const fixture=setup({fetchError:new Error('PRIVATE NETWORK DETAILS')});
  await assert.rejects(downloadViaServer(fixture.document,file,{fetch:fixture.fetch}),error=>{
    assert.equal(error.message,'The model-input download could not be prepared.');return true;
  });
  assert.deepEqual(fixture.anchors,[]);
});

for(const fail of ['append','click','remove'])test(`temporary attachment link is removed after ${fail} failure`,async()=>{
  const fixture=setup({fail});
  await assert.rejects(downloadViaServer(fixture.document,file,{fetch:fixture.fetch}));
  assert.equal(fixture.events.at(-1)[0],'remove');assert.equal(fixture.fetches.length,1);
});
