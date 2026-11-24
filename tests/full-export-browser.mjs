// Read-only source data; creates downloadable export artifacts on the local stack.
import {readFile,writeFile,mkdir} from 'node:fs/promises';
import assert from 'node:assert/strict';
const out='data/full-export-verification',base='http://127.0.0.1:8002';await mkdir(out,{recursive:true});
const credentials=JSON.parse(await readFile('data/local-accounts.json','utf8'));
const target=(await(await fetch('http://127.0.0.1:9234/json')).json()).find(p=>p.type==='page');assert.ok(target);
const socket=new WebSocket(target.webSocketDebuggerUrl);await new Promise((yes,no)=>{socket.addEventListener('open',yes,{once:true});socket.addEventListener('error',no,{once:true});});
let seq=0;const pending=new Map(),exceptions=[],checks=[];
socket.addEventListener('message',event=>{const m=JSON.parse(event.data);if(m.id){const p=pending.get(m.id);if(p){pending.delete(m.id);m.error?p.reject(new Error(JSON.stringify(m.error))):p.resolve(m.result);}return;}if(m.method==='Runtime.exceptionThrown')exceptions.push(m.params.exceptionDetails);});
function cdp(method,params={}){return new Promise((resolve,reject)=>{const id=++seq;pending.set(id,{resolve,reject});socket.send(JSON.stringify({id,method,params}));});}
async function evaluate(expression){const r=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true,userGesture:true});if(r.exceptionDetails)throw new Error(JSON.stringify(r.exceptionDetails));return r.result.value;}
const pause=ms=>new Promise(yes=>setTimeout(yes,ms));
async function wait(expression){const end=Date.now()+30000;while(Date.now()<end){if(await evaluate(`(()=>{try{return Boolean(${expression})}catch{return false}})()`))return;await pause(100);}throw new Error('Timed out '+expression);}
function passed(name,detail={}){checks.push({case:name,passed:true,...detail});console.log(name);}
try{
  await cdp('Runtime.enable');await cdp('Page.enable');await cdp('Network.enable');await cdp('Network.clearBrowserCookies');
  await cdp('Page.navigate',{url:base+'/?export-test='+Date.now()+'#map'});await wait("document.readyState==='complete'&&system.booted");
  await evaluate(`nodeById('login-username').value='viewer';nodeById('login-password').value=${JSON.stringify(credentials.viewer)};nodeById('login-form').requestSubmit()`);
  await wait("system.user?.username==='viewer'&&state.loaded&&!state.busy");
  await evaluate("openCheckinFilter({user_id:'0',start:'2010-10-17T00:00:00Z',end:'2010-10-20T00:00:00Z'})");await wait('!state.busy&&state.items.length===4');
  const batch=await evaluate('state.batchId'),view=await evaluate('({...view})');
  await evaluate("nodeById('export-csv').click()");await wait("!nodeById('export-download').hidden");
  const job=await evaluate('system.exportJobId');assert.match(job,/^E-[a-f0-9]{32}$/);
  const data=await evaluate("fetch(nodeById('export-download').href).then(async r=>({status:r.status,text:await r.text()}))");assert.equal(data.status,200);assert.equal(data.text.trim().split('\n').length,5);assert.ok(data.text.includes(batch));
  assert.deepEqual(await evaluate('({...view})'),view);passed('csv_ui_creates_complete_task_and_downloads_four_rows_without_moving_map');
  await evaluate('window.__oldExportDocument=true');await cdp('Page.reload',{ignoreCache:true});await wait("!window.__oldExportDocument&&system.booted&&system.user&&state.loaded&&!state.busy&&!nodeById('export-download').hidden");assert.equal(await evaluate('system.exportJobId'),job);passed('page_reload_restores_owned_export_download');
  await evaluate("nodeById('export-geojson').click()");await wait(`system.exportJobId!==${JSON.stringify(job)}&&!nodeById('export-download').hidden`);
  const geo=await evaluate("fetch(nodeById('export-download').href).then(r=>r.json())");assert.equal(geo.features.length,4);assert.equal(geo.metadata.batch_id,batch);passed('geojson_ui_downloads_complete_fixed_batch_features');
  await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:false});await pause(200);assert.ok(await evaluate('document.documentElement.scrollWidth<=innerWidth+1'));passed('export_actions_fit_narrow_screen');
  const download=await evaluate("nodeById('export-download').getAttribute('href')");await evaluate('system.logout()');assert.equal(await evaluate("nodeById('export-download').hidden"),true);assert.equal(await evaluate("nodeById('export-download').hasAttribute('href')"),false);assert.equal(await evaluate(`fetch(${JSON.stringify(download)}).then(r=>r.status)`),401);passed('logout_clears_download_and_server_rejects_unauthenticated_access');
  assert.equal(exceptions.length,0);await writeFile(out+'/browser-result.json',JSON.stringify({passed:checks.length,failed:0,checks,exceptions},null,2)+'\n');console.log(JSON.stringify({passed:checks.length,failed:0}));
}catch(error){await writeFile(out+'/browser-failed.json',JSON.stringify({checks,exceptions,error:String(error)},null,2));throw error;}
finally{socket.close();}
