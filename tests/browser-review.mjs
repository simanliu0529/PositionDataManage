// Real-browser acceptance for the review iteration. Only use an isolated stack.
// This publishes synthetic fixtures and changes the active batch in that stack.
import {readFile,writeFile,mkdir,copyFile} from 'node:fs/promises';
import {resolve,join} from 'node:path';
import assert from 'node:assert/strict';

const base=process.env.CHECKIN_TEST_BASE||'http://127.0.0.1:8012';
const accounts=process.env.CHECKIN_TEST_ACCOUNTS||'data/review-runtime-20261122/local-accounts.json';
const output=process.env.CHECKIN_TEST_OUTPUT||'data/review-verification';
const debugging=process.env.CHECKIN_BROWSER_DEBUG||'http://127.0.0.1:9224';
if(!/^http:\/\/127\.0\.0\.1:8012\/?$/.test(base)&&process.env.CHECKIN_ALLOW_TEST_MUTATIONS!=='1')throw new Error('Use the isolated :8012 stack, or explicitly set CHECKIN_ALLOW_TEST_MUTATIONS=1.');
await mkdir(output,{recursive:true});
const downloadPath=resolve(output,'browser-downloads');await mkdir(downloadPath,{recursive:true});
const credentials=JSON.parse(await readFile(accounts,'utf8'));
const oracle=JSON.parse(await readFile('sample/review-oracle.json','utf8'));
const totalRecords=oracle.tsv_expected.inserted;
const inRectangle=(row,box)=>row.longitude>=box.west&&row.longitude<=box.east&&row.latitude>=box.south&&row.latitude<=box.north;
const rectangleRows=oracle.cleaned_records.filter(row=>inRectangle(row,oracle.queries.rectangle.params));
assert.equal(rectangleRows.length,oracle.queries.rectangle.expected_count,'Independent spatial filter must match the direct upstream output oracle.');
const csvSource=await readFile('sample/review-checkins.csv','utf8');
const customCSV=join(output,'custom.csv');
await writeFile(customCSV,csvSource.replace(/^[^\r\n]*/,'person,at,y,x,venue'));
const aliasJSONL=join(output,'aliases.jsonl');
const jsonRows=(await readFile('sample/review-checkins.jsonl','utf8')).trim().split(/\r?\n/).map(line=>JSON.parse(line));
await writeFile(aliasJSONL,jsonRows.map(row=>JSON.stringify({uid:row.user_id,time:row.checkin_time,lat:row.latitude,lng:row.longitude,venue_id:row.location_id})).join('\n')+'\n');

const target=(await (await fetch(debugging+'/json')).json()).find(entry=>entry.type==='page');
assert.ok(target,'Chrome must provide a page target on the configured debugging port.');
const socket=new WebSocket(target.webSocketDebuggerUrl);
await new Promise((done,fail)=>{socket.addEventListener('open',done,{once:true});socket.addEventListener('error',fail,{once:true});});
let sequence=0,failTiles=false;
const pending=new Map(),exceptions=[],extensionExceptions=[],dialogs=[],downloads=[],osmResponses=[],networkFailures=[],consoleErrors=[];
function cdp(method,params={}){return new Promise((done,fail)=>{const id=++sequence;pending.set(id,{done,fail});socket.send(JSON.stringify({id,method,params}));});}
socket.addEventListener('message',event=>{
  const message=JSON.parse(event.data);
  if(message.id){const task=pending.get(message.id);if(task){pending.delete(message.id);message.error?task.fail(new Error(JSON.stringify(message.error))):task.done(message.result);}return;}
  const data=message.params||{};
  if(message.method==='Runtime.exceptionThrown'){
    const details=data.exceptionDetails,frames=details.stackTrace?.callFrames||[];
    // Browser-managed extensions run in this target too. Keep their full evidence
    // separately; application or mixed-source stacks still fail acceptance.
    if(details.url?.startsWith('chrome-extension://')&&frames.length&&frames.every(frame=>frame.url.startsWith('chrome-extension://')))extensionExceptions.push(details);
    else exceptions.push(details);
  }
  if(message.method==='Page.javascriptDialogOpening'){dialogs.push(data.message);cdp('Page.handleJavaScriptDialog',{accept:true}).catch(error=>exceptions.push({text:error.message}));}
  if(message.method==='Browser.downloadWillBegin')downloads.push({...data,state:'inProgress'});
  if(message.method==='Browser.downloadProgress'){const entry=downloads.find(row=>row.guid===data.guid);if(entry)Object.assign(entry,data);}
  if(message.method==='Network.responseReceived'&&/^https:\/\/tile\.openstreetmap\.org\/\d+\/\d+\/\d+\.png(?:\?|$)/.test(data.response.url))osmResponses.push({url:data.response.url,status:data.response.status,mimeType:data.response.mimeType,fromDiskCache:data.response.fromDiskCache||false,type:data.type});
  if(message.method==='Network.loadingFailed')networkFailures.push({errorText:data.errorText,type:data.type,blockedReason:data.blockedReason||null});
  if(message.method==='Log.entryAdded'&&data.entry.level==='error')consoleErrors.push({text:data.entry.text,url:data.entry.url||null});
  if(message.method==='Fetch.requestPaused'){
    const shouldFail=failTiles&&data.request.url.startsWith('https://tile.openstreetmap.org/');
    cdp(shouldFail?'Fetch.failRequest':'Fetch.continueRequest',shouldFail?{requestId:data.requestId,errorReason:'Failed'}:{requestId:data.requestId}).catch(error=>exceptions.push({text:error.message}));
  }
});
async function evaluate(expression){const result=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true,userGesture:true});if(result.exceptionDetails)throw new Error(JSON.stringify(result.exceptionDetails));return result.result.value;}
const pause=milliseconds=>new Promise(done=>setTimeout(done,milliseconds));
async function until(predicate,description,timeout=25000){const end=Date.now()+timeout;let last;while(Date.now()<end){try{if(await predicate())return;}catch(error){last=error;}await pause(100);}throw new Error('Timed out: '+description+(last?' ('+last.message+')':''));}
async function wait(expression,timeout=25000){await until(()=>evaluate(`(()=>{try{return Boolean(${expression})}catch{return false}})()`),expression,timeout);}
async function click(selector){await evaluate(`(()=>{const element=document.querySelector(${JSON.stringify(selector)});if(typeof element.click==='function')element.click();else element.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window}));})()`);}
async function go(page){await evaluate(`location.hash=${JSON.stringify(page)}`);await wait(`!document.querySelector('#${page}').hidden`);}
async function shot(name){await evaluate('window.scrollTo(0,0)');await pause(200);const result=await cdp('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});const file=join(output,name+'.png');await writeFile(file,Buffer.from(result.data,'base64'));return file;}
async function signIn(username){await evaluate(`nodeById('login-username').value=${JSON.stringify(username)};nodeById('login-password').value=${JSON.stringify(credentials[username])};nodeById('login-form').requestSubmit()`);await wait(`system.user?.username===${JSON.stringify(username)}&&!document.body.classList.contains('signed-out')`);}
async function signOut(){await click('#logout');await wait('system.user===null');}
async function chooseFile(path,format){const document=await cdp('DOM.getDocument');const input=await cdp('DOM.querySelector',{nodeId:document.root.nodeId,selector:'#upload-form input[type=file]'});await cdp('DOM.setFileInputFiles',{nodeId:input.nodeId,files:[resolve(path)]});await evaluate("nodeById('upload-form').elements.file.dispatchEvent(new Event('change',{bubbles:true}))");await wait(`system.previewData?.format===${JSON.stringify(format)}`);}
async function upload(mode,filename){
  await evaluate(`nodeById('upload-form').elements.mode.value=${JSON.stringify(mode)};nodeById('upload-status').textContent='';nodeById('import-error').textContent=''`);await click('#upload-button');
  await wait("!system.uploading&&(nodeById('upload-status').textContent.includes('上传完成')||nodeById('upload-status').textContent.includes('复用')||nodeById('import-error').textContent.length>0)",45000);
  assert.equal(await evaluate("nodeById('import-error').textContent"),'');const id=await evaluate('system.jobId');assert.ok(id);
  let job;await until(async()=>{job=await evaluate(`system.request('/api/v2/imports/'+${JSON.stringify(id)})`);return ['succeeded','failed'].includes(job.status);},'terminal import '+id,60000);
  assert.equal(job.status,'succeeded',job.error);assert.equal(job.filename,filename);
  await wait(`nodeById('job-title').textContent.includes(${JSON.stringify(id)})&&nodeById('job-status').textContent==='已完成'`);
  return job;
}
async function query(expected){await click('#submit-query');await wait('!state.busy');assert.equal(await evaluate('state.items.length'),expected);assert.equal(await evaluate("nodeById('form-error').textContent"),'');return await evaluate('state.items');}
async function reset(){await click('#reset-query');await wait('!state.busy');}
function canonical(row){return {user_id:row.user_id,checkin_time:row.checkin_time,latitude:Number(row.latitude),longitude:Number(row.longitude),location_id:row.location_id};}
function normalized(rows){return rows.map(canonical).sort((a,b)=>JSON.stringify(a).localeCompare(JSON.stringify(b)));}
function parseCSV(source){const records=[];let record=[],cell='',quoted=false;for(let index=0;index<source.length;index++){const char=source[index];if(quoted){if(char==='"'&&source[index+1]==='"'){cell+='"';index++;}else if(char==='"')quoted=false;else cell+=char;}else if(char==='"')quoted=true;else if(char===','){record.push(cell);cell='';}else if(char==='\n'){record.push(cell.replace(/\r$/,''));records.push(record);record=[];cell='';}else cell+=char;}if(cell||record.length){record.push(cell.replace(/\r$/,''));records.push(record);}const header=records.shift();header[0]=header[0].replace(/^\uFEFF/,'');return records.filter(row=>row.length>1).map(row=>Object.fromEntries(header.map((key,index)=>[key,row[index]])));}
async function download(format){const before=downloads.length;await click('#export-'+format);await until(()=>downloads.length>before,'browser download start');const entry=downloads[before];await until(()=>entry.state==='completed','browser download completion',45000);const file=entry.filePath||join(downloadPath,entry.guid);const content=await readFile(file,'utf8');const artifact=join(output,'review-export-'+format+(format==='csv'?'.csv':'.geojson'));await copyFile(file,artifact);return {content,artifact,suggestedFilename:entry.suggestedFilename};}

const report={base,started_at:new Date().toISOString(),checks:[],screenshots:[],real_osm_responses:osmResponses,exceptions,extension_exceptions:extensionExceptions,exception_scope:'Application and mixed-source exceptions fail acceptance; stacks entirely under chrome-extension:// are preserved separately.',console_errors:consoleErrors,network_failures:networkFailures,
  validation_scope:'Fullstack, query, map and export consistency against the unmodified upstream cleaner output; passing checks do not certify the known faulty coordinate-majority algorithm.',
  oracle_provenance:oracle.cleaning_provenance,known_upstream_issue:oracle.known_upstream_issue,
  historical_withdrawn_local_fix:oracle.historical_withdrawn_local_fix,
  upstream_accuracy_regressions:{status:'OPEN; expectedFailure tests retain the mathematically correct expectations',tests:['test_first_coordinate_remains_majority_and_minorities_are_repaired','test_later_occurrences_of_initial_coordinate_contribute_to_mode','test_new_majority_counts_only_records_that_change'],owner:'team cleaning module; unchanged by fullstack'},
};
async function save(){await writeFile(join(output,'browser-review-result.json'),JSON.stringify(report,null,2));}
async function check(name,action,{continueOnFailure=false}={}){const start=Date.now();try{const evidence=await action();report.checks.push({name,status:'passed',milliseconds:Date.now()-start,...(evidence?{evidence}: {})});console.log('PASS '+name);}catch(error){report.checks.push({name,status:'failed',milliseconds:Date.now()-start,error:error.message});console.log('FAIL '+name+': '+error.message);if(!continueOnFailure)throw error;}finally{await save();}}
let baselineBatch,publishedBatch,fullJob;
try{
  await cdp('Page.enable');await cdp('Runtime.enable');await cdp('Network.enable');await cdp('Log.enable');
  await cdp('Browser.setDownloadBehavior',{behavior:'allowAndName',downloadPath,eventsEnabled:true});
  await cdp('Emulation.setDeviceMetricsOverride',{width:1500,height:1000,deviceScaleFactor:1,mobile:false});
  await cdp('Page.navigate',{url:base+'/'});await wait("document.readyState==='complete'&&typeof system!=='undefined'&&system.booted");if(await evaluate('system.user!==null'))await signOut();
  await check('administrator sign in and isolated baseline',async()=>{
    await signIn('admin');const batches=await evaluate("system.request('/api/v2/batches?limit=50')");baselineBatch=batches.items.find(batch=>['active','historical'].includes(batch.status)&&batch.nodes.reduce((sum,node)=>sum+node.row_count,0)===4)?.batch_id;
    let created=false,emptyOnboardingVerified=false;
    if(!baselineBatch){
      const overview=await evaluate("system.request('/api/v2/overview')");
      if(!overview.has_data){await wait("Boolean(document.querySelector('#overview-content .onboarding'))");assert.ok((await evaluate("nodeById('overview-content').textContent")).includes('第一份原始签到文件'));emptyOnboardingVerified=true;}
      await go('imports');await chooseFile('sample/checkins.tsv','tsv');const job=await upload('replace','checkins.tsv');assert.equal(job.node_counts.reduce((sum,count)=>sum+count,0),4);baselineBatch=job.batch_id;created=true;
    }
    assert.ok(baselineBatch);return {baseline_batch:baselineBatch,created_in_isolated_stack:created,empty_onboarding_verified:emptyOnboardingVerified};
  });
  await go('imports');
  await check('custom CSV preview and explicit five-field mapping',async()=>{
    await chooseFile(customCSV,'csv');assert.deepEqual(await evaluate('system.previewData.columns'),['person','at','y','x','venue']);assert.ok(await evaluate('Boolean(system.previewData.mapping_error)'));
    assert.equal(await evaluate("nodeById('upload-form').checkValidity()"),false);
    const mapping={user_id:'person',checkin_time:'at',latitude:'y',longitude:'x',location_id:'venue'};
    await evaluate(`(()=>{for(const [key,value] of Object.entries(${JSON.stringify(mapping)})){const select=document.querySelector('[data-canonical="'+key+'"]');select.value=value;select.dispatchEvent(new Event('change',{bubbles:true}));}})()`);
    assert.equal(await evaluate("nodeById('upload-form').checkValidity()"),true);assert.deepEqual(await evaluate('system.mapping()'),mapping);
    const job=await upload('dry_run','custom.csv');for(const key of ['scanned_rows','rejected_rows','duplicates'])assert.equal(job[key],oracle.csv_json_jsonl_expected[key]);assert.deepEqual(job.node_counts,oracle.csv_json_jsonl_expected.node_counts);assert.equal(job.batch_id,null);assert.deepEqual(job.field_mapping,mapping);
    report.screenshots.push(await shot('review-csv-mapping'));return {job_id:job.job_id,input_format:job.input_format,node_counts:job.node_counts};
  });
  await check('JSONL aliases preview and actual normalized import',async()=>{
    await chooseFile(aliasJSONL,'jsonl');assert.deepEqual(await evaluate('system.previewData.field_mapping'),{user_id:'uid',checkin_time:'time',latitude:'lat',longitude:'lng',location_id:'venue_id'});
    const job=await upload('dry_run','aliases.jsonl');for(const key of ['scanned_rows','rejected_rows','duplicates'])assert.equal(job[key],oracle.csv_json_jsonl_expected[key]);assert.deepEqual(job.node_counts,oracle.csv_json_jsonl_expected.node_counts);assert.equal(job.batch_id,null);return {job_id:job.job_id,input_format:job.input_format};
  });
  await check('19 physical TSV rows become 10 records with genuine quality evidence',async()=>{
    await chooseFile('sample/review-anomalies.tsv','tsv');fullJob=await upload('replace','review-anomalies.tsv');publishedBatch=fullJob.batch_id;assert.ok(publishedBatch);
    for(const key of ['scanned_rows','rejected_rows','valid_rows','duplicates'])assert.equal(fullJob[key],oracle.tsv_expected[key],key);assert.deepEqual(fullJob.node_counts,oracle.tsv_expected.node_counts);assert.equal(fullJob.cleaning_stats.coord_repair,oracle.coordinate_repairs);
    assert.equal(fullJob.quality_samples.filter(row=>row.outcome==='coordinate_repaired'&&row.before&&row.after).length,oracle.coordinate_repairs_observed_in_output);assert.ok(fullJob.quality_samples.some(row=>row.outcome==='rejected'));assert.ok(fullJob.quality_samples.some(row=>row.outcome==='not_retained'));assert.ok((await evaluate("nodeById('job-outcome').textContent")).includes('坐标处理（清洗模块报告）'+oracle.coordinate_repairs));
    assert.equal(await evaluate("nodeById('quality-samples').children.length"),fullJob.quality_samples.length);assert.ok((await evaluate("nodeById('quality-samples').textContent")).includes('未保留（详见规则汇总）'));assert.ok((await evaluate("nodeById('record-basis').textContent")).includes('物理行'));
    report.screenshots.push(await shot('review-cleaning-evidence'));return {job_id:fullJob.job_id,batch_id:publishedBatch,sample_count:fullJob.quality_samples.length,reasons:fullJob.reasons,reported_coordinate_processing:fullJob.cleaning_stats.coord_repair,observed_changed_output_rows:oracle.coordinate_repairs_observed_in_output,upstream_majority_issue_remains_open:true};
  });
  await check('overview reconciles users locations months quality and node manifests',async()=>{
    await go('home');await wait("nodeById('overview-content').textContent.includes('统计批次 '+"+JSON.stringify(publishedBatch)+')');const overview=await evaluate("system.request('/api/v2/overview')");
    assert.equal(overview.batch_id,publishedBatch);assert.equal(overview.total_count,totalRecords);assert.deepEqual(overview.entities,{users:oracle.unique_users,locations:oracle.unique_locations,months:Object.keys(oracle.month_counts).length});assert.deepEqual(overview.monthly_trend,Object.entries(oracle.month_counts).map(([month,count])=>({month,count})));assert.equal(overview.quality.raw,oracle.tsv_expected.scanned_rows);assert.equal(overview.quality.inserted,totalRecords);assert.equal(overview.quality.coordinate_repairs,oracle.coordinate_repairs);assert.ok(Math.abs(overview.quality.retention_rate-totalRecords/oracle.tsv_expected.scanned_rows*100)<.0001);assert.equal(overview.nodes_consistent,true);assert.equal(overview.catalog_consistent,true);assert.ok(overview.time_range.start);assert.ok(overview.time_range.end);
    assert.deepEqual(await evaluate("[...document.querySelectorAll('.stat-value')].map(element=>element.textContent)"),[totalRecords,oracle.unique_users,oracle.unique_locations,Object.keys(oracle.month_counts).length].map(String));assert.equal(await evaluate("document.querySelector('.quality-rate').textContent"),(totalRecords/oracle.tsv_expected.scanned_rows*100).toFixed(2)+'%');assert.ok((await evaluate("nodeById('overview-content').textContent")).includes('坐标处理（清洗模块报告）'));report.screenshots.push(await shot('review-workbench'));return overview;
  });
  await check('month directory drilldown preserves batch and UTC interval',async()=>{
    await go('catalog');await click('[data-group="month"]');await wait("nodeById('catalog-status').textContent.includes("+JSON.stringify(publishedBatch)+')');await click('#catalog-rows button');await wait('!state.busy&&state.loaded');assert.equal(await evaluate('state.items.length'),totalRecords);assert.equal(await evaluate('state.batchId'),publishedBatch);assert.equal(await evaluate("state.filters.get('start')"),'2010-10-01T00:00:00Z');assert.equal(await evaluate("state.filters.get('end')"),'2010-11-01T00:00:00Z');
  });
  await check('spatial and timezone queries remain consistent with actual upstream output',async()=>{
    await click('[data-spatial="bbox"]');await evaluate(`(()=>{for(const [key,value] of Object.entries(${JSON.stringify(oracle.queries.rectangle.params)}))fields[key].value=String(value)})()`);let items=await query(rectangleRows.length);assert.deepEqual(normalized(items),normalized(rectangleRows));
    await click('[data-spatial="radius"]');await evaluate(`(()=>{for(const [key,value] of Object.entries(${JSON.stringify(oracle.queries.radius.params)}))fields[key].value=String(value)})()`);items=await query(oracle.queries.radius.expected_users.length);assert.deepEqual(items.map(row=>row.user_id).sort(),oracle.queries.radius.expected_users.slice().sort());assert.equal(await evaluate("nodeById('radius-circle').hasAttribute('hidden')"),false);
    await evaluate("fields.start.value='2010-10-19T02:00:00';fields.end.value='2010-10-19T03:00:00'");items=await query(1);assert.equal(items[0].user_id,'synthetic-u2');return {rectangle_count:rectangleRows.length,radius_users:oracle.queries.radius.expected_users,timezone_user:items[0].user_id,cleaner_accuracy_certified:false,upstream_issue:oracle.known_upstream_issue};
  });
  await check('colocated point aggregation exposes members and selected rows',async()=>{
    await reset();await evaluate("fields.location_id.value='synthetic-plaza'");const plazaCount=oracle.location_counts['synthetic-plaza'],items=await query(plazaCount);const actualPOI=oracle.known_upstream_issue.plaza_selected_by_upstream;assert.ok(items.every(row=>row.latitude===actualPOI.latitude&&row.longitude===actualPOI.longitude),'Map must show the actual upstream output, including its documented incorrect majority choice.');assert.equal(await evaluate("document.querySelectorAll('#markers .map-cluster').length"),1);assert.equal(await evaluate("document.querySelector('#markers .map-cluster').dataset.count"),String(plazaCount));const width=await evaluate('view.east-view.west');await click('#markers .map-cluster');assert.equal(await evaluate('view.east-view.west'),width);assert.equal(await evaluate("nodeById('cluster-members').hidden"),false);assert.equal(await evaluate("nodeById('cluster-members').querySelectorAll('button').length"),plazaCount);
    await click('#cluster-members button');assert.equal(await evaluate("document.querySelectorAll('#markers circle.selected').length"),1);assert.equal(await evaluate("document.querySelectorAll('#rows tr.selected').length"),1);await click('#rows tr:last-child button');assert.equal(await evaluate("document.querySelector('#markers circle.selected').dataset.key"),await evaluate("document.querySelector('#rows tr:last-child').dataset.key"));assert.ok((await evaluate("nodeById('cluster-note').textContent")).includes('仅显示当前页 5 条'));report.screenshots.push(await shot('review-colocated-members'));
  });
  await check('real OpenStreetMap PNG response and loaded SVG city basemap',async()=>{
    await reset();await click('[data-spatial="bbox"]');await evaluate(`(()=>{for(const [key,value] of Object.entries(${JSON.stringify(oracle.queries.rectangle.params)}))fields[key].value=String(value)})()`);await query(rectangleRows.length);await click('#basemap-toggle');await until(async()=>osmResponses.some(row=>row.status===200&&row.mimeType==='image/png'&&row.type==='Image')&&await evaluate("basemapEnabled&&[...tiles.values()].some(tile=>tile.loaded)&&nodeById('basemap-tiles').querySelectorAll('image').length>0"),'real OSM PNG network response and loaded SVG image',30000);assert.equal(await evaluate("nodeById('map-attribution').hidden"),false);report.screenshots.push(await shot('review-real-city-map'));return {successful_png:osmResponses.filter(row=>row.status===200&&row.mimeType==='image/png')};
  },{continueOnFailure:true});
  await check('tile failure is explicit preserves results and offers retry',async()=>{
    const items=await evaluate('state.items');assert.equal(items.length,rectangleRows.length);failTiles=true;await cdp('Network.setCacheDisabled',{cacheDisabled:true});await cdp('Fetch.enable',{patterns:[{urlPattern:'https://tile.openstreetmap.org/*',requestStage:'Request'}]});
    try{await click('#basemap-toggle');await wait("nodeById('basemap-status').classList.contains('map-error')&&nodeById('basemap-toggle').textContent.includes('重试')");assert.deepEqual(await evaluate('state.items'),items);assert.equal(await evaluate('basemapEnabled'),true);assert.equal(await evaluate("nodeById('grid').getAttribute('opacity')"),'0');assert.ok((await evaluate("nodeById('basemap-status').textContent")).includes('不可作为完整城市地图'));report.screenshots.push(await shot('review-basemap-failure'));}
    finally{failTiles=false;await cdp('Fetch.disable');await cdp('Network.setCacheDisabled',{cacheDisabled:false});}
    await click('#basemap-toggle');assert.equal(await evaluate("nodeById('basemap-status').classList.contains('map-error')"),false);assert.deepEqual(await evaluate('state.items'),items);
  });
  await check('CSV GeoJSON downloads include all matching records and fixed successful snapshot',async()=>{
    await reset();await click('[data-spatial="bbox"]');await evaluate(`(()=>{for(const [key,value] of Object.entries(${JSON.stringify(oracle.queries.rectangle.params)}))fields[key].value=String(value);fields.limit.value='2'})()`);await query(2);assert.equal(await evaluate('state.exportSnapshot.batchId'),publishedBatch);
    await evaluate(`system.request('/api/v2/batches/${baselineBatch}/activate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_batch_id:${JSON.stringify(publishedBatch)}})})`);assert.equal((await evaluate("system.request('/api/v2/dataset/active')")).batch_id,baselineBatch);
    await evaluate("fields.user_id.value='not-submitted-and-must-not-affect-export'");const csv=await download('csv'),csvRows=parseCSV(csv.content);assert.equal(csvRows.length,rectangleRows.length);assert.deepEqual(normalized(csvRows),normalized(rectangleRows));assert.ok(csvRows.every(row=>row.batch_id===publishedBatch));
    const geo=await download('geojson'),collection=JSON.parse(geo.content);assert.equal(collection.type,'FeatureCollection');assert.equal(collection.features.length,rectangleRows.length);assert.equal(collection.metadata.batch_id,publishedBatch);assert.equal(collection.metadata.scope,'all_matching_records');assert.equal(collection.metadata.filters.user_id,undefined);assert.equal(collection.metadata.filters.limit,undefined);assert.equal(collection.metadata.filters.offset,undefined);assert.deepEqual(normalized(collection.features.map(feature=>({...feature.properties,longitude:feature.geometry.coordinates[0],latitude:feature.geometry.coordinates[1]}))),normalized(rectangleRows));
    await evaluate(`system.request('/api/v2/batches/${publishedBatch}/activate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_batch_id:${JSON.stringify(baselineBatch)}})})`);return {csv:csv.artifact,geojson:geo.artifact,batch_id:publishedBatch,interactive_page_size:2,exported_records:rectangleRows.length};
  });
  await check('viewer cannot manage but can query and download with clean page size',async()=>{
    await signOut();await signIn('viewer');assert.equal(await evaluate('state.limit'),50);assert.equal(await evaluate('fields.limit.value'),'50');assert.equal(await evaluate('getComputedStyle(document.querySelector(\'[data-page="imports"]\')).display'),'none');
    assert.equal(await evaluate("fetch('/api/v2/batches').then(response=>response.status)"),403);assert.equal(await evaluate("system.request('/api/v2/imports',{method:'POST',body:new FormData()}).then(()=>0,error=>error.status)"),403);
    await go('map');await wait('!state.busy&&state.loaded');await reset();assert.equal(await evaluate('state.items.length'),totalRecords);const downloaded=await download('geojson'),collection=JSON.parse(downloaded.content);assert.equal(collection.features.length,totalRecords);assert.deepEqual(normalized(collection.features.map(feature=>({...feature.properties,longitude:feature.geometry.coordinates[0],latitude:feature.geometry.coordinates[1]}))),normalized(oracle.cleaned_records));assert.equal(collection.metadata.batch_id,publishedBatch);return {role:'viewer',exported_records:collection.features.length,management_status:403};
  });
  await check('desktop and narrow-screen map and dashboard remain usable',async()=>{
    await cdp('Emulation.setDeviceMetricsOverride',{width:1500,height:1000,deviceScaleFactor:1,mobile:false});await go('home');await wait("nodeById('overview-content').textContent.includes('统计批次 '+"+JSON.stringify(publishedBatch)+')');report.screenshots.push(await shot('review-desktop-home'));await go('map');await wait('!state.busy');report.screenshots.push(await shot('review-desktop-map'));assert.ok(await evaluate('document.documentElement.scrollWidth<=1500'));
    await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:false});await evaluate('renderMap()');await pause(200);assert.ok(await evaluate('document.documentElement.scrollWidth<=390'));report.screenshots.push(await shot('review-mobile-map'));assert.ok(await evaluate("nodeById('export-geojson').getBoundingClientRect().width>0"));await go('home');await wait("nodeById('overview-content').textContent.includes('统计批次 '+"+JSON.stringify(publishedBatch)+')');assert.ok(await evaluate('document.documentElement.scrollWidth<=390'));report.screenshots.push(await shot('review-mobile-home'));return {desktop:[1500,1000],mobile:[390,844],no_horizontal_overflow:true};
  });
  await check('no uncaught application exceptions and session exits cleanly',async()=>{await signOut();assert.deepEqual(exceptions,[]);assert.equal(await evaluate("fetch('/api/v2/overview').then(response=>response.status)"),401);return {application_exceptions:exceptions.length,external_extension_exceptions:extensionExceptions.length};});
}catch(error){report.fatal_error=error.stack||error.message;process.exitCode=1;}
finally{
  try{if(failTiles){failTiles=false;await cdp('Fetch.disable');}await cdp('Network.setCacheDisabled',{cacheDisabled:false});}catch{}
  report.completed_at=new Date().toISOString();report.confirmations=dialogs.length;report.downloads=downloads.map(({guid,suggestedFilename,state})=>({guid,suggestedFilename,state}));report.passed=report.checks.filter(check=>check.status==='passed').length;report.failed=report.checks.filter(check=>check.status==='failed').length;if(report.failed||report.fatal_error)process.exitCode=1;await save();socket.close();console.log(JSON.stringify({passed:report.passed,failed:report.failed,fatal:report.fatal_error||null,report:join(output,'browser-review-result.json')},null,2));
}
