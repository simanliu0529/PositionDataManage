// Read-only real-browser regression. Requires a dedicated Chrome on :9224.
// Never uploads fixtures or changes the active dataset.
import {readFile,writeFile,mkdir} from 'node:fs/promises';
import assert from 'node:assert/strict';

const base='http://127.0.0.1:8002',output=process.env.CHECKIN_VIEWPORT_OUTPUT||'data/map-viewport-verification';
await mkdir(output,{recursive:true});
const credentials=JSON.parse(await readFile('data/local-accounts.json','utf8'));
const target=(await (await fetch((process.env.CHECKIN_VIEWPORT_DEBUG||'http://127.0.0.1:9224')+'/json')).json()).find(x=>x.type==='page');
assert.ok(target,'Start a dedicated Chrome with remote debugging on :9224.');
const socket=new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve,reject)=>{socket.addEventListener('open',resolve,{once:true});socket.addEventListener('error',reject,{once:true});});
let sequence=0;const pending=new Map(),exceptions=[],requests=[],tileResponses=[];
function cdp(method,params={}){return new Promise((resolve,reject)=>{const id=++sequence,timer=setTimeout(()=>{pending.delete(id);reject(new Error('CDP timeout: '+method));},15000);pending.set(id,{resolve:result=>{clearTimeout(timer);resolve(result)},reject:error=>{clearTimeout(timer);reject(error)}});socket.send(JSON.stringify({id,method,params}));});}
socket.addEventListener('message',event=>{
  const m=JSON.parse(event.data);
  if(m.id){const p=pending.get(m.id);if(p){pending.delete(m.id);m.error?p.reject(new Error(JSON.stringify(m.error))):p.resolve(m.result);}return;}
  const p=m.params||{};
  if(m.method==='Runtime.exceptionThrown'&&!p.exceptionDetails.url?.startsWith('chrome-extension://'))exceptions.push(p.exceptionDetails);
  if(m.method==='Network.requestWillBeSent'&&p.request.url.includes('/api/'))requests.push({method:p.request.method,url:p.request.url});
  if(m.method==='Network.responseReceived'&&p.response.url.startsWith('https://tile.openstreetmap.org/'))tileResponses.push({status:p.response.status,mimeType:p.response.mimeType});
});
async function evaluate(expression){const r=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true,userGesture:true});if(r.exceptionDetails)throw new Error(JSON.stringify(r.exceptionDetails));return r.result.value;}
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function wait(expression){const end=Date.now()+25000;while(Date.now()<end){if(await evaluate(`(()=>{try{return Boolean(${expression})}catch{return false}})()`))return;await pause(100);}throw new Error('Timed out: '+expression);}
async function click(selector){await evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);}
async function screen(x,y){return evaluate(`(()=>{const p=svg.createSVGPoint();p.x=${x};p.y=${y};const q=p.matrixTransform(svg.getScreenCTM());return{x:q.x,y:q.y}})()`);}
const checks=[];let signedIn=false;
try {
  await cdp('Runtime.enable');await cdp('Network.enable');await cdp('Page.enable');await cdp('Network.clearBrowserCookies');
  const stamp=Date.now();await cdp('Page.navigate',{url:base+'/?viewport-test='+stamp+'#map'});
  await wait(`location.search==='?viewport-test=${stamp}'&&document.readyState==='complete'&&system.booted&&!system.user`);
  await evaluate(`nodeById('login-username').value='viewer';nodeById('login-password').value=${JSON.stringify(credentials.viewer)};nodeById('login-form').requestSubmit()`);
  await wait("system.user?.username==='viewer'&&state.loaded&&!state.busy");signedIn=true;
  const active=await evaluate("system.request('/api/v2/dataset/active')");
  await evaluate("fillBounds({west:-30,east:75,south:12,north:60});submitFilters()");await wait('!state.busy&&state.items.length===50');
  assert.equal(await evaluate("nodeById('form-error').textContent"),'');
  await evaluate("setView(-40,10,140,60);svg.scrollIntoView({block:'center'})");
  await wait('tiles.size>0&&[...tiles.values()].some(t=>t.loaded)');
  const filters=await evaluate('[...state.filters]');
  // Independent slippy-map projection, compared against actual DOM attributes.
  // Check immediately within the same JS task: no 180ms tile timer can run.
  await evaluate(`window.checkViewportAlignment=()=>{
    const world=(lon,lat)=>[(lon+180)/360,(1-Math.log(Math.tan(Math.PI/4+lat*Math.PI/360))/Math.PI)/2];
    const b=nodeById('applied-box'),n=k=>Number(b.getAttribute(k));
    const corners=[[Number(state.filters.get('west')),Number(state.filters.get('north')),n('x'),n('y')],
                   [Number(state.filters.get('east')),Number(state.filters.get('south')),n('x')+n('width'),n('y')+n('height')]];
    let error=0;
    for(const [key,tile] of tiles){const [z,x,y]=key.split('/').map(Number),s=2**z,e=tile.el,a=k=>Number(e.getAttribute(k));
      for(const [lon,lat,px,py] of corners){const [wx,wy]=world(lon,lat);error=Math.max(error,Math.abs(a('x')+(wx*s-x)*a('width')-px),Math.abs(a('y')+(wy*s-y)*a('height')-py));}}
    return{error,tiles:tiles.size,boxHidden:b.hasAttribute('hidden')};
  }`);
  async function aligned(name,expression){const result=await evaluate(`(()=>{${expression};return window.checkViewportAlignment()})()`);assert.ok(result.tiles>0&&!result.boxHidden);assert.ok(result.error<1e-7,name+': '+JSON.stringify(result));checks.push({case:name,passed:true,max_svg_error:result.error,tiles:result.tiles});}
  await aligned('button_zoom_same_paint',"nodeById('zoom-in').click()");
  const center=await evaluate('lonlat(450,210)');
  await aligned('rapid_zoom_no_intermediate_drift',"for(let i=0;i<12;i++){zoom(.85);const a=window.checkViewportAlignment();if(a.error>1e-7)throw new Error('Intermediate zoom drift');zoom(1/.85)}");
  const centerAfter=await evaluate('lonlat(450,210)');assert.ok(center.every((v,i)=>Math.abs(v-centerAfter[i])<1e-9));
  checks.push({case:'projected_center_stays_fixed',passed:true});
  const anchor=[310,175],geo=await evaluate('lonlat(310,175)');
  await aligned('off_center_zoom_same_paint',`zoom(.85,${JSON.stringify(anchor)})`);
  const geoAfter=await evaluate('lonlat(310,175)');assert.ok(geo.every((v,i)=>Math.abs(v-geoAfter[i])<1e-9));
  checks.push({case:'mouse_anchor_stays_fixed',passed:true});
  const wheelBefore=await evaluate('view.east-view.west'),wheelPoint=await screen(...anchor);
  await evaluate("document.addEventListener('wheel',event=>{const anchor=cursor(event);window.viewportWheelBefore={anchor,geo:lonlat(...anchor)}},{once:true,capture:true})");
  await cdp('Input.dispatchMouseEvent',{type:'mouseWheel',...wheelPoint,deltaX:0,deltaY:-100});
  await wait(`Math.abs(view.east-view.west-${wheelBefore})>.001`);
  const wheel=await evaluate('({before:window.viewportWheelBefore,after:lonlat(...window.viewportWheelBefore.anchor)})');
  assert.ok(wheel.before.geo.every((v,i)=>Math.abs(v-wheel.after[i])<1e-9),'Native wheel preserves its actual event anchor: '+JSON.stringify(wheel));
  await aligned('native_wheel_alignment','');
  const dragGeo=await evaluate('lonlat(350,230)'),start=await screen(350,230),end=await screen(405,200);
  await cdp('Input.dispatchMouseEvent',{type:'mouseMoved',...start});
  await cdp('Input.dispatchMouseEvent',{type:'mousePressed',...start,button:'left',buttons:1,clickCount:1});
  await cdp('Input.dispatchMouseEvent',{type:'mouseMoved',...end,buttons:1});
  await cdp('Input.dispatchMouseEvent',{type:'mouseReleased',...end,button:'left',buttons:0,clickCount:1});
  const dragGeoAfter=await evaluate('lonlat(405,200)');assert.ok(dragGeo.every((v,i)=>Math.abs(v-dragGeoAfter[i])<1e-3),'Map follows the drag in projected coordinates');
  await aligned('native_pan_alignment','');
  await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:false});await pause(200);
  await aligned('narrow_viewport_alignment','');
  await cdp('Emulation.setDeviceMetricsOverride',{width:1500,height:1000,deviceScaleFactor:1,mobile:false});await pause(200);
  await aligned('new_tiles_after_debounce_alignment','');
  assert.deepEqual(await evaluate('[...state.filters]'),filters);
  assert.deepEqual(await evaluate("system.request('/api/v2/dataset/active')"),active);
  assert.ok(requests.every(r=>r.method==='GET'||r.url.endsWith('/auth/login')));
  assert.equal(exceptions.length,0);
  await evaluate('window.scrollTo(0,0)');const image=await cdp('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
  await writeFile(output+'/map-aligned.png',Buffer.from(image.data,'base64'));
  const report={passed:checks.length,failed:0,checks,active,query_filters_unchanged:true,exceptions,tileResponses,requests,completed_at:new Date().toISOString()};
  await writeFile(output+'/browser-result.json',JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report));
} finally {
  if(signedIn)await evaluate("system.request('/api/v2/auth/logout',{method:'POST'})").catch(()=>{});
  socket.close();
}
