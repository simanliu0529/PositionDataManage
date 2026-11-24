// End-to-end complete system browser test. Uses local generated credentials only.
import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {resolve} from 'node:path';
import assert from 'node:assert/strict';
const base=process.env.CHECKIN_TEST_BASE||'http://127.0.0.1:8012';
const accounts=process.env.CHECKIN_TEST_ACCOUNTS||'data/review-runtime-20261122/local-accounts.json';
const output=process.env.CHECKIN_TEST_OUTPUT||'data/review-verification';
const credentials=JSON.parse(await readFile(accounts,'utf8'));
const target=(await (await fetch('http://127.0.0.1:9224/json')).json()).find(t=>t.type==='page');
const socket=new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve,reject)=>{socket.addEventListener('open',resolve,{once:true});socket.addEventListener('error',reject,{once:true});});
let sequence=0;const pending=new Map(),errors=[],dialogs=[];
function cdp(method,params={}){return new Promise((resolve,reject)=>{const id=++sequence;pending.set(id,{resolve,reject});socket.send(JSON.stringify({id,method,params}));});}
socket.addEventListener('message',event=>{const data=JSON.parse(event.data);if(data.id){const task=pending.get(data.id);if(task){pending.delete(data.id);data.error?task.reject(new Error(JSON.stringify(data.error))):task.resolve(data.result);}}if(data.method==='Runtime.exceptionThrown')errors.push(data.params.exceptionDetails.text);if(data.method==='Page.javascriptDialogOpening'){dialogs.push(data.params.message);cdp('Page.handleJavaScriptDialog',{accept:true}).catch(e=>errors.push(e.message));}});
async function evaluate(expression){const result=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true,userGesture:true});if(result.exceptionDetails)throw new Error(JSON.stringify(result.exceptionDetails));return result.result.value;}
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function wait(expression,count=200){for(let i=0;i<count;i++){if(await evaluate(`(()=>{try{return Boolean(${expression})}catch{return false}})()`))return;await pause(100);}throw new Error('Timed out: '+expression);}
async function click(selector){await evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);}
async function signIn(username){await evaluate(`document.querySelector('#login-username').value=${JSON.stringify(username)};document.querySelector('#login-password').value=${JSON.stringify(credentials[username])};document.querySelector('#login-form').requestSubmit()`);await wait('system.user !== null && !document.body.classList.contains("signed-out")');}
async function query(count){await click('#submit-query');await wait('!state.busy');assert.equal(await evaluate('state.items.length'),count);assert.equal(await evaluate("document.querySelectorAll('#markers circle').length"),count);}
async function reset(){await click('#reset-query');await wait('!state.busy');}
async function go(page){await evaluate(`location.hash=${JSON.stringify(page)}`);await wait(`!document.querySelector('#${page}').hidden`);}
await mkdir(output,{recursive:true});
async function shot(name){await evaluate('window.scrollTo(0,0)');await pause(100);const shot=await cdp('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});await writeFile(`${output}/${name}.png`,Buffer.from(shot.data,'base64'));}
async function uploadSample(mode){await evaluate(`document.querySelector('input[name="mode"][value="${mode}"]').checked=true`);const root=await cdp('DOM.getDocument');const node=await cdp('DOM.querySelector',{nodeId:root.root.nodeId,selector:'#upload-form input[type=file]'});await cdp('DOM.setFileInputFiles',{nodeId:node.nodeId,files:[resolve('sample/checkins.tsv')]});await click('#upload-button');await wait('!system.uploading && system.jobId !== null');await wait("document.querySelector('#job-status').textContent==='已完成'",400);return await evaluate("system.request('/api/v2/imports/'+system.jobId)");}
const passed=[];
try {
 await cdp('Page.enable');await cdp('Runtime.enable');await cdp('Emulation.setDeviceMetricsOverride',{width:1500,height:1000,deviceScaleFactor:1,mobile:false});
 await cdp('Page.navigate',{url:base+'/'});await pause(300);await wait("document.readyState==='complete' && typeof system!=='undefined' && system.booted");
 if(await evaluate('system.user!==null')){await click('#logout');await wait('system.user===null');}
 await shot('login');await signIn('admin');await go('imports');
 const dry=await uploadSample('dry_run');assert.equal(dry.batch_id,null);assert.equal(dry.valid_rows,5);assert.equal(dry.node_counts.reduce((a,b)=>a+b),4);passed.push('admin uploads dry run and receives real reconciliation report');
 const published=await uploadSample('replace');const old=published.batch_id;assert.ok(old);
 const active=await evaluate("system.request('/api/v2/dataset/active')");
 if(active.batch_id!==old)await evaluate(`system.request('/api/v2/batches/${old}/activate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_batch_id:${JSON.stringify(active.batch_id)}})})`);
 await shot('import-report');passed.push('replace confirmation and automatic dual-node publication');
 await cdp('Page.reload',{ignoreCache:true});await wait("system.user!==null && document.querySelector('#job-status').textContent==='已完成'");assert.equal(await evaluate('system.jobId'),published.job_id);passed.push('task report recovered after page refresh');
 await evaluate("fields.user_id.value='unsubmitted-draft'");await click('#job-open-map');await wait('!state.busy');assert.equal(await evaluate('state.batchId'),old);assert.equal(await evaluate('state.items.length'),4);passed.push('report opens its published batch with cleared draft filters');
 await go('home');await wait("document.querySelector('#nodes').textContent.includes('合计 4')");await shot('workbench');
 await go('map');await wait('!state.busy && state.loaded');await reset();
 await evaluate("fields.user_id.value='1'");await query(1);assert.deepEqual(await evaluate('state.items.map(row=>row.user_id)'),['1']);passed.push('authenticated user query and one-node routing');
 await click('#rows button');assert.equal(await evaluate("document.querySelectorAll('#markers .selected').length"),1);passed.push('map/table linked selection');
 await reset();await evaluate("fields.location_id.value='101'");await query(1);passed.push('location ID exact filter');
 await reset();await click('[data-spatial="radius"]');await evaluate("fields.center_lon.value='114.36';fields.center_lat.value='30.54';fields.radius_m.value='1000'");await query(1);assert.equal(await evaluate("document.querySelector('#radius-circle').hasAttribute('hidden')"),false);await shot('radius-map');passed.push('radius query uses real backend and circle preview');
 await evaluate("fields.user_id.value='1';fields.start.value='2010-08-01T12:00:00';fields.end.value='2010-08-02T00:00:00'");await query(1);passed.push('radius, user and UTC time intersection');
 await reset();await evaluate("fields.limit.value='2'");await query(2);await click('#next');await wait('!state.busy');assert.equal(await evaluate('state.offset'),2);const pinned=await evaluate('state.batchId');await cdp('Page.reload',{ignoreCache:true});await wait('system.user!==null && state.loaded && !state.busy');assert.equal(await evaluate('state.offset'),2);assert.equal(await evaluate('state.batchId'),pinned);passed.push('authenticated refresh restores pinned pagination');
 await go('catalog');await wait("document.querySelector('#catalog-status').textContent.includes('批次 B-')");assert.equal(await evaluate("document.querySelectorAll('#catalog-rows tr').length"),4);passed.push('real user catalog with whole-batch counts');
 await click('[data-group="month"]');await wait("document.querySelector('#catalog-status').textContent.includes('批次 B-')");await click('#catalog-rows button');await wait('!state.busy');assert.equal(await evaluate("state.filters.get('end')"),'2010-09-01T00:00:00Z');assert.equal(await evaluate('state.items.length'),4);passed.push('real UTC month directory drilldown');
 await go('catalog');await click('[data-group="location"]');await wait("document.querySelector('#catalog-status').textContent.includes('批次 B-')");await evaluate("document.querySelector('#catalog-prefix').value='101';document.querySelector('#catalog-search').requestSubmit()");await wait("document.querySelector('#catalog-status').textContent.includes('批次 B-')");assert.equal(await evaluate("document.querySelectorAll('#catalog-rows tr').length"),1);await shot('catalog');await click('#catalog-rows button');await wait('!state.busy');assert.equal(await evaluate("state.filters.get('location_id')"),'101');assert.equal(await evaluate('state.items.length'),1);passed.push('location prefix search and same-batch map drilldown');
 await go('imports');await evaluate("(()=>{const row=['9','2014-01-01T00:00:00Z','30','114','browser-'+Date.now()].join('\\t')+'\\n';const file=new File([row],'browser-replacement.tsv',{type:'text/plain'});const dt=new DataTransfer();dt.items.add(file);document.querySelector('#upload-form input[type=file]').files=dt.files;document.querySelector('input[name=mode][value=replace]').checked=true})()");
 await click('#upload-button');await wait('!system.uploading');await wait("document.querySelector('#job-status').textContent==='已完成'",400);const newer=await evaluate("system.request('/api/v2/imports/'+system.jobId)");assert.notEqual(newer.batch_id,old);
 await go('batches');await wait(`system.batchActive===${JSON.stringify(newer.batch_id)}`);await shot('batches');
 await evaluate(`(()=>{const row=[...document.querySelectorAll('#batch-rows tr')].find(row=>row.textContent.includes(${JSON.stringify(old)}));row.querySelectorAll('button')[1].click()})()`);await wait(`system.batchActive===${JSON.stringify(old)}`);passed.push('batch list, activation confirmation and rollback');
 await click('#logout');await wait('system.user===null');assert.equal(await evaluate("fetch('/api/checkins').then(r=>r.status)"),401);passed.push('logout revokes legacy query access');
 await signIn('viewer');await go('home');assert.equal(await evaluate("getComputedStyle(document.querySelector('[data-page=imports]')).display"),'none');assert.equal(await evaluate("fetch('/api/v2/batches').then(r=>r.status)"),403);passed.push('viewer role hides admin actions and server returns 403');
 await go('map');await wait('!state.busy');await reset();await query(4);await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:false});await shot('mobile');assert.ok(await evaluate('document.documentElement.scrollWidth<=390'));passed.push('authenticated mobile layout does not overflow');
 await click('#logout');await wait('system.user===null');assert.deepEqual(errors,[]);passed.push('no uncaught browser exceptions');
 await writeFile(`${output}/browser-result.json`,JSON.stringify({base,passed,confirmations:dialogs.length},null,2));console.log(JSON.stringify({passed,confirmations:dialogs.length},null,2));
}finally{socket.close();}
