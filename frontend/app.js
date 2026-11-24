"use strict";
const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const form = $('#query');
const fields = form.elements;
const state = {filters:new URLSearchParams(), limit:50, offset:0, items:[], selected:null, spatial:'none', controller:null, loaded:false, busy:false, batchId:null, exportSnapshot:null,hasPositioned:false};
const view = {west:100,east:125,south:20,north:45};
let drawMode = false;
let gesture = null;
let suppressClick = false;
const keyOf = item => `${item.node}:${item.id}`;
const QUERY_STORAGE = 'position-data:last-query:v1';
const MERCATOR_LIMIT = 85.05112878;
let basemapEnabled = true;
let basemapFailed = false;
let clusterEnabled = true;
let tileTimer;
const tiles = new Map();
const mapSource = {name:'OpenStreetMap',tile_url:'https://tile.openstreetmap.org/{z}/{x}/{y}.png',attribution:'OpenStreetMap contributors',attribution_url:'https://www.openstreetmap.org/copyright'};

// Centralize authenticated v2 calls independently of map rendering.
const queryAPI = {
  nodes: () => getJSON('/api/v2/dataset/active'),
  checkins: (params, signal) => getJSON('/api/v2/checkins?' + params, signal),
};

async function getJSON(url, signal) {
  const timeout = AbortSignal.timeout(12000);
  try {return await system.request(url,{signal:signal ? AbortSignal.any([signal,timeout]) : timeout});}
  catch(error) {
    if(signal?.aborted)throw error;
    if(timeout.aborted)throw new Error('请求超过 12 秒，请缩小查询范围或稍后重试');
    throw error;
  }
}
function navigate() {
  let page = ['home','map','catalog','imports','batches'].includes(location.hash.slice(1)) ? location.hash.slice(1) : 'home';
  if(!system.user)return;
  if(['imports','batches'].includes(page)&&system.user.role!=='admin')page='home';
  $$('.page').forEach(el => el.hidden = el.id !== page);
  $$('[data-page]').forEach(el => {el.classList.toggle('active',el.dataset.page === page); if(el.dataset.page === page) el.setAttribute('aria-current','page'); else el.removeAttribute('aria-current');});
  document.title = `社交媒体位置签到数据管理服务系统 · ${{home:'工作台',map:'签到地图',catalog:'分类浏览',imports:'数据导入',batches:'批次管理'}[page]}`;
  if (page === 'map' && !state.loaded && !state.busy && !state.invalidShare) search(state.offset);
  if (page === 'catalog') renderCatalog();
  if (page === 'imports') system.importsPage();
  if (page === 'batches') {system.batchesPage();window.workspace?.loadBatchChoices();}
  if (page === 'map') renderMap();
  if (page === 'home') window.dashboard?.load();
  requestAnimationFrame(() => window.scrollTo(0,0));
}
window.addEventListener('hashchange',navigate);
async function refreshNodes() {
  $('#refresh-nodes').disabled = true;
  try {
    const data = await queryAPI.nodes();
    const total = data.nodes.reduce((sum,n) => sum + n.count,0);
    $('#nodes').textContent = data.nodes.map(n=>`节点 ${n.node} · ${n.count.toLocaleString()} 条`).join('　|　') + `　合计 ${total.toLocaleString()} 条`;
    $('#connection').textContent = `当前批次 ${data.batch_id} · ${total.toLocaleString()} 条`;
    $('#nodes').textContent += ` · ${data.batch_id}`;
  } catch(error) {
    $('#nodes').textContent = error.message;
    $('#connection').textContent = error.code==='NO_ACTIVE_BATCH'?'暂无已发布数据':'节点连接异常';
  } finally { $('#refresh-nodes').disabled = false; }
}
$('#refresh-nodes').addEventListener('click',refreshNodes);
function setSpatial(mode) {
  state.spatial = mode;
  $$('[data-spatial]').forEach(el => {el.classList.toggle('active',el.dataset.spatial === mode);el.setAttribute('aria-pressed',String(el.dataset.spatial === mode));});
  $('#bbox-fields').hidden = mode !== 'bbox';
  $('#radius-note').hidden = mode !== 'radius';
  $('#submit-query').disabled = state.busy;
  $('#radius-fields').hidden = mode !== 'radius';
  for(const name of ['center_lon','center_lat','radius_m']) {fields[name].disabled=mode!=='radius';fields[name].required=mode==='radius';}
  for(const name of ['west','east','south','north']) {fields[name].disabled = mode !== 'bbox'; fields[name].required = mode === 'bbox';}
  $('#form-error').textContent = '';
  if(window.workspace)workspace.refreshDraft();else renderMap();
}
$$('[data-spatial]').forEach(el=>el.addEventListener('click',()=>setSpatial(el.dataset.spatial)));
function collectFilters() {

  const params = new URLSearchParams();
  const user = fields.user_id.value.trim();
  if(user) params.set('user_id',user);
  const locationId=fields.location_id.value.trim();if(locationId)params.set('location_id',locationId);
  for(const name of ['start','end']) {
    const value = fields[name].value;
    if(value) params.set(name,value.length === 16 ? value + ':00Z' : value + 'Z');
  }
  if(params.has('start') && params.has('end') && Date.parse(params.get('start')) >= Date.parse(params.get('end'))) throw new Error('开始时间必须早于结束时间（UTC）。');
  if(state.spatial === 'bbox') {
    for(const name of ['west','east','south','north']) {
      if(fields[name].value === '') throw new Error('矩形查询需要填写全部四个边界。');
      const value = Number(fields[name].value), bound = ['west','east'].includes(name) ? 180 : 90;
      if(!Number.isFinite(value) || value < -bound || value > bound) throw new Error('经纬度超出有效范围。');
      params.set(name,String(value));
    }
    if(Number(params.get('west')) >= Number(params.get('east')) || Number(params.get('south')) >= Number(params.get('north'))) throw new Error('矩形必须满足西界小于东界、南界小于北界。');
  }
  if(state.spatial==='radius') {
    for(const name of ['center_lon','center_lat','radius_m']) {const value=Number(fields[name].value),bound=name==='center_lon'?180:name==='center_lat'?90:50000;if(fields[name].value===''||!Number.isFinite(value)||Math.abs(value)>bound||(name==='radius_m'&&value<1))throw new Error('请填写有效的中心坐标和 1–50000 米半径。');params.set(name,String(value));}
  }
  return params;
}
function submitFilters(batchId=null) {
  try {
    if(!form.reportValidity()) return;
    const params = collectFilters();
    state.invalidShare=false;
    $('#form-error').textContent = '';
    state.filters = params; state.batchId=batchId; state.limit = Number(fields.limit.value);
    search(0);
  } catch(error) {$('#form-error').textContent = error.message;}
}
window.openCheckinFilter=(values,batchId=null)=>{form.reset();setSpatial('west' in values?'bbox':'radius_m' in values?'radius':'none');for(const [key,value] of Object.entries(values))if(fields[key])fields[key].value=['start','end'].includes(key)?new Date(value).toISOString().slice(0,19):String(value);location.hash='map';submitFilters(batchId);};
form.addEventListener('submit',event=>{event.preventDefault();submitFilters();});
$('#reset-query').addEventListener('click',()=>{form.reset();setSpatial('none');submitFilters();});
function appliedText() {
  const names = {location_id:'地点',center_lon:'中心经度',center_lat:'中心纬度',radius_m:'半径（米）',user_id:'用户',start:'开始（含）',end:'结束（不含）',west:'西界',east:'东界',south:'南界',north:'北界'};
  return [...state.filters].map(([key,value])=>`${names[key]} ${value}`).join(' · ') || '全部记录';
}
async function search(offset) {
  if(!system.user)return;
  state.controller?.abort();
  const controller = new AbortController(); state.controller = controller; state.busy = true;
  $('#submit-query').disabled=true;
  $('#latest-query').disabled=true;
  $('#query').setAttribute('aria-busy','true');
  system.updateExportState?.();
  $('#previous').disabled = $('#next').disabled = true;
  $('#form-error').textContent = '';
  $('#status').textContent = '查询中…'; $('#applied-filters').textContent = `正在请求：${appliedText()}`;
  state.items = []; state.selected = null; renderResults();
  $('#cluster-members')?.setAttribute('hidden','');
  $('#empty-state').hidden = true;
  const params = new URLSearchParams(state.filters); params.set('limit',state.limit);params.set('offset',offset);if(state.batchId)params.set('batch_id',state.batchId);
  try {
    const data = await queryAPI.checkins(params, controller.signal);
    if(controller.signal.aborted) return;
    state.items = data.items; state.offset = offset; state.batchId=data.batch_id; state.loaded = true;
    state.exportSnapshot={filters:[...state.filters],batchId:data.batch_id,limit:state.limit};
    window.workspace?.querySucceeded();
    rememberQuery();
    $('#status').textContent = `当前页 ${data.items.length} 条 · 查询节点 ${data.queried_nodes.join('、')}`;
    $('#applied-filters').textContent = `已应用：${appliedText()} · 批次 ${data.batch_id}`;
    $('#page-label').textContent = `第 ${Math.floor(offset/state.limit)+1} 页`;
    $('#previous').disabled = offset === 0; $('#next').disabled = !data.has_more || offset+state.limit > 10000;
    renderResults();if(!state.hasPositioned&&state.items.length){fitPoints();state.hasPositioned=true;}
    $('#empty-state').hidden = data.items.length !== 0;
  } catch(error) {
    if(controller.signal.aborted) return;
    $('#status').textContent = ['QUERY_TIMEOUT','NODE_TIMEOUT'].includes(error.code) ? '查询超时' : '查询失败';
    $('#form-error').textContent = error.message;
    window.workspace?.queryFailed(error);
    $('#applied-filters').textContent = `请求条件：${appliedText()} · 未返回结果`;
  } finally {if(state.controller === controller) {state.busy = false;$('#submit-query').disabled=false;$('#latest-query').disabled=false;$('#query').removeAttribute('aria-busy');system.updateExportState?.();window.workspace?.refreshDraft();}}
}
$('#previous').addEventListener('click',()=>search(Math.max(0,state.offset-state.limit)));
$('#latest-query').addEventListener('click',()=>{state.batchId=null;search(0);});
$('#next').addEventListener('click',()=>search(state.offset+state.limit));
function renderResults() {
  $('#rows').replaceChildren(); $('#point-detail').textContent = '点击点位或表格中的“定位”查看签到详情。';
  for(const item of state.items) {
    const tr = document.createElement('tr'); tr.dataset.key = keyOf(item);
    for(const key of ['user_id','checkin_time','longitude','latitude','location_id','node']) {const td=document.createElement('td');td.textContent=item[key];tr.append(td);}
    const td=document.createElement('td'),button=document.createElement('button');button.className='secondary';button.textContent='定位';button.setAttribute('aria-label',`定位用户 ${item.user_id} 的签到`);button.addEventListener('click',()=>selectPoint(item,true));td.append(button);tr.append(td);
    tr.addEventListener('click',event=>{if(!event.target.closest('button')) selectPoint(item,true);});
    $('#rows').append(tr);
  }
  renderMap();
}
function selectPoint(item, fromTable=false) {
  state.selected = keyOf(item);
  if(fromTable && (item.longitude < view.west || item.longitude > view.east || item.latitude < view.south || item.latitude > view.north)) {const w=view.east-view.west,h=view.north-view.south;setView(item.longitude-w/2,item.latitude-h/2,w,h);}
  $$('#rows tr').forEach(tr=>tr.classList.toggle('selected',tr.dataset.key===state.selected));
  $('#point-detail').textContent = `用户 ${item.user_id} · ${item.checkin_time} · 经度 ${item.longitude} / 纬度 ${item.latitude} · 地点 ${item.location_id} · 节点 ${item.node} · 记录 ${item.id}`;
  renderMap();
  if(!fromTable) [...$('#rows').children].find(tr=>tr.dataset.key===state.selected)?.scrollIntoView({block:'nearest',behavior:'smooth'});
}
const svgNS = 'http://www.w3.org/2000/svg';
function svgNode(tag,attrs) {const el=document.createElementNS(svgNS,tag);for(const [key,value] of Object.entries(attrs)) el.setAttribute(key,value);return el;}
const mercatorY = lat => (1-Math.asinh(Math.tan(Math.max(-MERCATOR_LIMIT,Math.min(MERCATOR_LIMIT,lat))*Math.PI/180))/Math.PI)/2;
const inverseMercatorY = y => Math.atan(Math.sinh(Math.PI*(1-2*y)))*180/Math.PI;
const xy = (lon,lat) => [(lon-view.west)/(view.east-view.west)*900,
  basemapEnabled ? (mercatorY(lat)-mercatorY(view.north))/(mercatorY(view.south)-mercatorY(view.north))*420 : (view.north-lat)/(view.north-view.south)*420];
const lonlat = (x,y) => [view.west+x/900*(view.east-view.west),
  basemapEnabled ? inverseMercatorY(mercatorY(view.north)+y/420*(mercatorY(view.south)-mercatorY(view.north))) : view.north-y/420*(view.north-view.south)];
function activateMarker(element,handler) {
  element.addEventListener('click',event=>{event.stopPropagation();if(!suppressClick&&!drawMode)handler();});
  element.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();handler();}});
}
function ensureClusterControls() {
  if(!$('#cluster-toggle')) {
    const button=document.createElement('button');button.id='cluster-toggle';button.className='secondary';button.textContent='聚合当前页';button.setAttribute('aria-pressed','true');$('#basemap-toggle').before(button);
  }
  if(!$('#cluster-members')) {const panel=document.createElement('div');panel.id='cluster-members';panel.className='cluster-members';panel.hidden=true;$('#point-detail').after(panel);}
  $('#cluster-toggle').addEventListener('click',()=>{clusterEnabled=!clusterEnabled;$('#cluster-toggle').setAttribute('aria-pressed',String(clusterEnabled));$('#cluster-toggle').textContent=clusterEnabled?'聚合当前页':'显示单点';$('#cluster-members').hidden=true;renderMap();});
  const note=document.createElement('p');note.id='cluster-note';note.className='hint';$('#cluster-members').after(note);
}
function revealCluster(items) {
  const panel=$('#cluster-members');panel.replaceChildren();panel.hidden=false;
  const heading=document.createElement('strong');heading.textContent=`当前页聚合包含 ${items.length} 条签到 · 选择记录查看详情`;panel.append(heading);
  for(const item of items) {const button=document.createElement('button');button.className='secondary';button.textContent=`用户 ${item.user_id} · ${item.checkin_time} · 地点 ${item.location_id}`;button.onclick=()=>selectPoint(item,true);panel.append(button);}
}
function renderMarkers(markers) {
  const points=state.items.map(item=>({item,position:xy(item.longitude,item.latitude)}));
  const markerScale=900/Math.max(240,$('#points').getBoundingClientRect().width||900);
  const threshold=32*markerScale;
  const candidates=points.filter(point=>keyOf(point.item)!==state.selected);
  const groups=[];
  for(const point of candidates) {
    // Connected groups avoid arbitrary grid boundaries splitting colocated records.
    const neighbours=clusterEnabled?groups.filter(group=>group.some(member=>Math.hypot(member.position[0]-point.position[0],member.position[1]-point.position[1])<=threshold)):[];
    if(!neighbours.length)groups.push([point]);
    else {const group=neighbours[0];group.push(point);for(const merged of neighbours.slice(1)){group.push(...merged);groups.splice(groups.indexOf(merged),1);}}
  }
  const chosen=points.find(point=>keyOf(point.item)===state.selected);if(chosen)groups.push([chosen]);
  for(const group of groups) {
    if(group.length===1) {
      const {item,position:[cx,cy]}=group[0];
      const point=svgNode('circle',{cx,cy,r:(state.selected===keyOf(item)?9:6)*markerScale,fill:item.node===0?'#2858df':'#a96336',tabindex:0,role:'button','aria-label':`用户 ${item.user_id}，地点 ${item.location_id}，节点 ${item.node}`,'data-key':keyOf(item)});
      point.classList.toggle('selected',state.selected===keyOf(item));
      const title=svgNode('title',{});title.textContent=`用户 ${item.user_id} · ${item.checkin_time}`;point.append(title);activateMarker(point,()=>selectPoint(item));markers.append(point);
    } else {
      const cx=group.reduce((sum,point)=>sum+point.position[0],0)/group.length,cy=group.reduce((sum,point)=>sum+point.position[1],0)/group.length;
      const marker=svgNode('g',{class:'map-cluster',tabindex:0,role:'button','aria-label':`当前页 ${group.length} 条签到，点击放大或查看成员`,'data-count':group.length});
      marker.append(svgNode('circle',{cx,cy,r:18*markerScale}));const count=svgNode('text',{x:cx,y:cy,'text-anchor':'middle','dominant-baseline':'central'});count.style.fontSize=(12*markerScale)+'px';count.textContent=group.length;marker.append(count);
      activateMarker(marker,()=>{
        const items=group.map(point=>point.item);revealCluster(items);
        const lons=items.map(item=>item.longitude),lats=items.map(item=>item.latitude),west=Math.min(...lons),east=Math.max(...lons),south=Math.min(...lats),north=Math.max(...lats);
        if(east-west<1e-9&&north-south<1e-9)return;
        if(view.east-view.west<=.0021)return;
        const width=Math.max(.002,(east-west)*1.8,(mercatorY(south)-mercatorY(north))*360*900/420*1.8);setView((west+east-width)/2,south,width,Math.max(.002,north-south));
      });markers.append(marker);
    }
  }
  const note=$('#cluster-note');if(note)note.textContent=`仅显示当前页 ${state.items.length} 条签到，${clusterEnabled?'近邻点按屏幕距离聚合；数字为当前页成员数':'单点模式'}。整批数量请查看工作台或分类目录。`;
}
function renderMap() {
  // Reposition cached tiles in the same paint as points and selection overlays.
  // Only discovery/loading of new tiles is debounced below.
  if(basemapEnabled)for(const [key,tile] of tiles)positionTile(tile,key);
  const grid=$('#grid');grid.replaceChildren();
  for(let i=0;i<=6;i++) {
    const x=i*150,y=i*70;
    grid.append(svgNode('line',{x1:x,y1:0,x2:x,y2:420}),svgNode('line',{x1:0,y1:y,x2:900,y2:y}));
    if(i<6) {const lon=svgNode('text',{x:x+5,y:415}),lat=svgNode('text',{x:5,y:y+14});lon.textContent=lonlat(x,0)[0].toFixed(2)+'°';lat.textContent=lonlat(0,y)[1].toFixed(2)+'°';grid.append(lon,lat);}
  }
  const markers=$('#markers'); markers.replaceChildren();
  renderMarkers(markers);
  $('#grid').setAttribute('opacity',basemapEnabled?'0':'1');
  const applied=new URLSearchParams(state.exportSnapshot?.filters||[]);
  const box=$('#applied-box');box.toggleAttribute('hidden',!applied.has('west'));
  if(!box.hasAttribute('hidden')) drawBox(box,Number(applied.get('west')),Number(applied.get('south')),Number(applied.get('east')),Number(applied.get('north')));
  $('#view-bounds').textContent=`西 ${view.west.toFixed(2)} / 东 ${view.east.toFixed(2)} · 南 ${view.south.toFixed(2)} / 北 ${view.north.toFixed(2)}`;
  renderRadius();
  window.workspace?.renderOverlays();
  clearTimeout(tileTimer);
  if(basemapEnabled && !$('#map').hidden) tileTimer = setTimeout(renderTiles,180);
}
function setMercatorView(west,width,top) {
  width=Math.min(360,Math.max(.002,width));
  view.west=Math.max(-180,Math.min(180-width,west));view.east=view.west+width;
  const span=width/360*420/900;
  top=Math.max(0,Math.min(1-span,top));
  view.north=inverseMercatorY(top);view.south=inverseMercatorY(top+span);
  renderMap();
}
function setView(west,south,width,height) {
  width=Math.min(360,Math.max(.002,width));height=Math.min(180,Math.max(.002,height));
  view.west=Math.max(-180,Math.min(180-width,west));view.east=view.west+width;
  if(basemapEnabled) {
    const span=width/360*420/900,center=mercatorY(south+height/2);
    setMercatorView(west,width,center-span/2);return;
  } else {
    view.south=Math.max(-90,Math.min(90-height,south));view.north=view.south+height;
  }
  renderMap();
}
function fitPoints() {
  if(!state.items.length) {renderMap();return;}
  const lons=state.items.map(i=>i.longitude),lats=state.items.map(i=>i.latitude);
  const minLon=Math.min(...lons),maxLon=Math.max(...lons),minLat=Math.min(...lats),maxLat=Math.max(...lats);
  if(!basemapEnabled&&lats.every(lat=>Math.abs(lat)<=MERCATOR_LIMIT)) {basemapEnabled=true;window.refreshMapSource?.();}
  if(basemapEnabled && lats.some(lat=>Math.abs(lat)>MERCATOR_LIMIT)) {
    disableBasemap('结果包含极地坐标，已切换离线视图以完整显示。');
  }
  const h=Math.max(.08,(maxLat-minLat)*1.5);
  const w=Math.max(.08,(maxLon-minLon)*1.5,basemapEnabled ? (mercatorY(minLat)-mercatorY(maxLat))*360*900/420*1.5 : 0);
  setView((minLon+maxLon-w)/2,(minLat+maxLat-h)/2,w,h);
}
function zoom(factor,anchor=[450,210]) {
  const [lon,lat]=lonlat(...anchor),width=Math.min(360,Math.max(.002,(view.east-view.west)*factor));
  const west=lon-anchor[0]/900*width;
  if(basemapEnabled) {
    const span=width/360*420/900;
    setMercatorView(west,width,mercatorY(lat)-anchor[1]/420*span);
  } else {
    const height=Math.min(180,Math.max(.002,(view.north-view.south)*factor));
    setView(west,lat-(1-anchor[1]/420)*height,width,height);
  }
}
$('#zoom-in').addEventListener('click',()=>zoom(.6));$('#zoom-out').addEventListener('click',()=>zoom(1.6));$('#fit-points').addEventListener('click',fitPoints);
const svg=$('#points');
new ResizeObserver(()=>{if(system.user&&!$('#map').hidden)renderMap();}).observe(svg);
svg.addEventListener('wheel',event=>{event.preventDefault();zoom(event.deltaY>0?1.15:.85,cursor(event));},{passive:false});
function cursor(event) {const point=svg.createSVGPoint();point.x=event.clientX;point.y=event.clientY;const local=point.matrixTransform(svg.getScreenCTM().inverse());return [Math.max(0,Math.min(900,local.x)),Math.max(0,Math.min(420,local.y))];}
function drawBox(el,west,south,east,north) {const [x,y]=xy(west,north),[right,bottom]=xy(east,south);for(const [key,value] of Object.entries({x,y,width:right-x,height:bottom-y}))el.setAttribute(key,value);}
function setDrawMode(enabled) {drawMode=enabled;svg.classList.toggle('drawing',enabled);$('#draw-bbox').setAttribute('aria-pressed',String(enabled));$('#draw-bbox').textContent=enabled?'取消框选':'框选范围';}
$('#draw-bbox').addEventListener('click',()=>setDrawMode(!drawMode));
svg.addEventListener('pointerdown',event=>{if(event.button!==0)return;suppressClick=false;const [x,y]=cursor(event);gesture={x,y,view:{...view},drawing:drawMode,moved:false};if(!event.target.closest('#markers [role="button"]')||drawMode)svg.setPointerCapture(event.pointerId);});
svg.addEventListener('pointermove',event=>{
  if(!gesture)return;const [x,y]=cursor(event),dx=x-gesture.x,dy=y-gesture.y;
  if(Math.abs(dx)+Math.abs(dy)<4&&!gesture.moved)return;gesture.moved=true;suppressClick=true;
  if(gesture.drawing) {const a=lonlat(gesture.x,gesture.y),b=lonlat(x,y);$('#draft-box').removeAttribute('hidden');drawBox($('#draft-box'),Math.min(a[0],b[0]),Math.min(a[1],b[1]),Math.max(a[0],b[0]),Math.max(a[1],b[1]));}
  else {
    const v=gesture.view,w=v.east-v.west,h=v.north-v.south;
    if(basemapEnabled) {
      const shift=-dy/420*(mercatorY(v.south)-mercatorY(v.north));
      setMercatorView(v.west-dx/900*w,w,mercatorY(v.north)+shift);
    } else setView(v.west-dx/900*w,v.south+dy/420*h,w,h);
  }
});
svg.addEventListener('pointerup',event=>{
  if(!gesture)return;
  if(gesture.drawing&&gesture.moved) {const [x,y]=cursor(event),a=lonlat(gesture.x,gesture.y),b=lonlat(x,y);if(Math.abs(x-gesture.x)>3&&Math.abs(y-gesture.y)>3) {fillBounds({west:Math.min(a[0],b[0]),east:Math.max(a[0],b[0]),south:Math.min(a[1],b[1]),north:Math.max(a[1],b[1])});$('#form-error').textContent='已填入框选范围，点击“查询”提交。';}setDrawMode(false);}
  if(!gesture.moved&&state.spatial==='radius'&&!event.target.closest('#markers [role="button"]')) {const [lon,lat]=lonlat(...cursor(event));fields.center_lon.value=lon.toFixed(7);fields.center_lat.value=lat.toFixed(7);renderMap();}
  gesture=null;$('#draft-box').setAttribute('hidden','');
});
svg.addEventListener('pointercancel',()=>{gesture=null;$('#draft-box').setAttribute('hidden','');});
function fillBounds(bounds) {setSpatial('bbox');for(const name of ['west','east','south','north'])fields[name].value=String(bounds[name]);if(window.workspace)workspace.refreshDraft();else renderMap();}
$('#viewport-query').addEventListener('click',()=>{fillBounds(view);submitFilters();});

let catalogGroup='user',catalogOffset=0,catalogPrefix='',catalogBatch=null,catalogController=null;
const catalogLimit=20;
async function renderCatalog() {
  if(!system.user)return;
  catalogController?.abort();const controller=new AbortController();catalogController=controller;
  const label={user:'用户 ID',month:'月份（UTC）',location:'地点 ID'}[catalogGroup];
  $('#catalog-key').textContent=label;$('#prefix-label').textContent=label+' 前缀';$('#catalog-prefix').disabled=catalogGroup==='month';
  $('#catalog-rows').replaceChildren();$('#catalog-previous').disabled=$('#catalog-next').disabled=true;$('#catalog-status').textContent='正在加载目录…';
  const params=new URLSearchParams({group_by:catalogGroup,limit:catalogLimit,offset:catalogOffset});if(catalogPrefix)params.set('q',catalogPrefix);if(catalogBatch)params.set('batch_id',catalogBatch);
  try {
    const data=await getJSON('/api/v2/catalog?'+params,controller.signal);if(controller.signal.aborted)return;catalogBatch=data.batch_id;
    for(const entry of data.items) {
      const tr=document.createElement('tr');for(const value of [entry.key,entry.count]){const td=document.createElement('td');td.textContent=value;tr.append(td);}
      const td=document.createElement('td'),button=document.createElement('button');button.className='secondary';button.textContent='查看签到';
      const group=catalogGroup,batch=data.batch_id,key=entry.key;
      button.addEventListener('click',()=>{
        form.reset();setSpatial('none');
        if(group==='user')fields.user_id.value=key;
        else if(group==='location')fields.location_id.value=key;
        else {const nextMonth=new Date(`${key}-01T00:00:00Z`);nextMonth.setUTCMonth(nextMonth.getUTCMonth()+1);fields.start.value=`${key}-01T00:00:00`;fields.end.value=nextMonth.toISOString().slice(0,19);}
        location.hash='map';submitFilters(batch);
      });td.append(button);tr.append(td);$('#catalog-rows').append(tr);
    }
    $('#catalog-empty').hidden=data.items.length!==0;$('#catalog-previous').disabled=catalogOffset===0;$('#catalog-next').disabled=!data.has_more||catalogOffset+catalogLimit>10000;
    $('#catalog-page').textContent=`第 ${Math.floor(catalogOffset/catalogLimit)+1} 页`;
    $('#catalog-status').textContent='批次 '+data.batch_id+' · 计数范围：整个批次';
  } catch(error) {if(!controller.signal.aborted){$('#catalog-status').textContent=error.message;$('#catalog-empty').hidden=true;}}
}
$$('[data-group]').forEach(button=>button.addEventListener('click',()=>{catalogGroup=button.dataset.group;catalogOffset=0;catalogPrefix='';$('#catalog-prefix').value='';$$('[data-group]').forEach(el=>{el.classList.toggle('active',el===button);el.setAttribute('aria-pressed',String(el===button));});renderCatalog();}));
$('#catalog-search').addEventListener('submit',event=>{event.preventDefault();catalogPrefix=$('#catalog-prefix').value.trim();catalogOffset=0;renderCatalog();});
$('#catalog-latest').addEventListener('click',()=>{catalogBatch=null;catalogOffset=0;renderCatalog();});
$('#catalog-previous').addEventListener('click',()=>{catalogOffset=Math.max(0,catalogOffset-catalogLimit);renderCatalog();});$('#catalog-next').addEventListener('click',()=>{catalogOffset+=catalogLimit;renderCatalog();});
function rememberQuery() {
  try {
    sessionStorage.setItem(QUERY_STORAGE,JSON.stringify({filters:[...state.filters],limit:state.limit,offset:state.offset,batchId:state.batchId}));
    $('#query-memory').textContent='已保存最近一次有效查询，刷新可恢复。';
  } catch { $('#query-memory').textContent='浏览器未允许保存查询；当前查询仍可正常使用。'; }
}
function restoreQuery() {
  const shared=new URLSearchParams(location.search),isShared=shared.has('share');
  try {
    let saved;
    if(isShared) {
      const allowed=new Set(['share','batch_id','limit','user_id','location_id','start','end','west','east','south','north','center_lon','center_lat','radius_m']);
      if(location.search.length>8192||shared.get('share')!=='1'||!/^B-[a-f0-9]{24}$/.test(shared.get('batch_id')||''))throw new Error('Invalid shared batch');
      for(const [key,value] of shared){if(!allowed.has(key)||shared.getAll(key).length!==1)throw new Error('Invalid shared parameter');if(['start','end'].includes(key)&&(!/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/.test(value)||new Date(value).toISOString().replace('.000Z','Z')!==value))throw new Error('Invalid shared time');}
      saved={filters:[...shared].filter(([key])=>!['share','batch_id','limit'].includes(key)),limit:Number(shared.get('limit')||50),offset:0,batchId:shared.get('batch_id')};
    } else {const raw=sessionStorage.getItem(QUERY_STORAGE);if(!raw)return;saved=JSON.parse(raw);}
    if(!Array.isArray(saved.filters)||![2,50,100,200].includes(saved.limit)||!Number.isInteger(saved.offset)||saved.offset<0||saved.offset>10000)throw new Error('Invalid saved query');
    const params=new URLSearchParams(saved.filters);
    if(isShared){const box=['west','east','south','north'],radius=['center_lon','center_lat','radius_m'];if((box.some(k=>params.has(k))&&!box.every(k=>params.has(k)))||(radius.some(k=>params.has(k))&&!radius.every(k=>params.has(k)))||(box.some(k=>params.has(k))&&radius.some(k=>params.has(k))))throw new Error('Incomplete shared geometry');}
    const allowed=new Set(['user_id','location_id','start','end','west','east','south','north','center_lon','center_lat','radius_m']);
    for(const [key,value] of params) {
      if(!allowed.has(key)||params.getAll(key).length!==1)throw new Error('Invalid saved filter');
      if(key==='start'||key==='end') {
        if(!Number.isFinite(Date.parse(value)))throw new Error('Invalid saved time');
        fields[key].value=new Date(value).toISOString().slice(0,19);
      } else fields[key].value=value;
    }
    setSpatial(params.has('west')?'bbox':params.has('radius_m')?'radius':'none');fields.limit.value=String(saved.limit);
    const validated=collectFilters();
    if(!form.checkValidity())throw new Error('Invalid saved bounds');
    state.filters=validated;state.limit=saved.limit;state.offset=saved.offset;state.batchId=typeof saved.batchId==='string'?saved.batchId:null;
    $('#query-memory').textContent=isShared?'已读取分享条件及固定批次；登录后查询，不会自动改查其他版本。':'已恢复最近一次有效查询；结果将重新从服务获取。';
  } catch {
    form.reset();setSpatial('none');state.filters=new URLSearchParams();state.limit=50;state.offset=0;state.batchId=null;state.invalidShare=isShared;
    $('#query-memory').textContent=isShared?'分享链接无效，未执行查询。请重新设置条件，或向发送者获取有效链接。':'未能恢复上次查询，已重置为默认条件。';
    try {sessionStorage.removeItem(QUERY_STORAGE);} catch { /* Storage may be unavailable. */ }
  }
}

function clearTiles() {
  clearTimeout(tileTimer);
  for(const tile of tiles.values())clearTimeout(tile.expiry);
  tiles.clear();$('#basemap-tiles').replaceChildren();
}
function disableBasemap(message='当前为极地坐标视图；Web Mercator 不覆盖极地。') {
  basemapEnabled=false;clearTiles();
  $('#basemap-toggle').textContent='重试城市底图';$('#basemap-toggle').setAttribute('aria-pressed','false');
  $('#map-attribution').hidden=true;$('#basemap-status').textContent=message;renderMap();
}
$('#basemap-toggle').addEventListener('click',()=>{
  if(state.items.some(item=>Math.abs(item.latitude)>MERCATOR_LIMIT)) {disableBasemap('当前结果包含极地坐标，请使用离线视图。');return;}
  basemapEnabled=true;basemapFailed=false;clearTiles();$('#basemap-status').classList.remove('map-error');$('#basemap-toggle').textContent='刷新城市底图';$('#basemap-toggle').setAttribute('aria-pressed','true');
  $('#map-attribution').hidden=false;$('#basemap-status').textContent='正在按当前视野加载 '+mapSource.name+' 底图…';
  if(state.items.length)fitPoints();else setView(view.west,view.south,view.east-view.west,view.north-view.south);
});
window.refreshMapSource=()=>{basemapFailed=false;clearTiles();$('#basemap-status').classList.remove('map-error');$('#map-attribution').hidden=!basemapEnabled;$('#basemap-toggle').textContent='刷新城市底图';$('#basemap-toggle').setAttribute('aria-pressed',String(basemapEnabled));$('#basemap-status').textContent='正在按当前视野加载 '+mapSource.name+' 城市底图…';renderMap();};
function positionTile(tile,key) {
  const [zoom,x,y]=key.split('/').map(Number),size=2**zoom;
  const west=x/size*360-180,east=(x+1)/size*360-180;
  const north=inverseMercatorY(y/size),south=inverseMercatorY((y+1)/size);
  const [px,py]=xy(west,north),[rx,by]=xy(east,south);
  for(const [name,value] of Object.entries({x:px,y:py,width:rx-px,height:by-py}))tile.el.setAttribute(name,value);
}
function renderTiles() {
  if(!basemapEnabled)return;
  const width=view.east-view.west;
  const zoom=Math.max(0,Math.min(18,Math.floor(Math.log2(900*360/(256*width)))));
  const size=2**zoom,needed=new Set();
  const left=Math.max(0,Math.floor((view.west+180)/360*size));
  const right=Math.min(size-1,Math.floor((view.east+180)/360*size));
  const top=Math.max(0,Math.floor(mercatorY(view.north)*size));
  const bottom=Math.min(size-1,Math.floor(mercatorY(view.south)*size));
  for(let x=left;x<=right;x++)for(let y=top;y<=bottom;y++) {
    const key=`${zoom}/${x}/${y}`;needed.add(key);
    let tile=tiles.get(key);
    if(!tile) {
      const el=svgNode('image',{preserveAspectRatio:'none'});
      tile={el,loaded:false,failed:false};tiles.set(key,tile);
      const fail=()=>{if(tiles.get(key)===tile&&basemapEnabled){clearTimeout(tile.expiry);tile.failed=true;basemapFailed=true;$('#basemap-status').textContent='城市底图未完整加载：请检查网络或配置授权离线瓦片。签到坐标与查询结果已保留，当前底图不可作为完整城市地图。点击“重试城市底图”重新加载。';$('#basemap-status').classList.add('map-error');$('#basemap-toggle').textContent='重试城市底图';}};
      el.addEventListener('load',()=>{
        clearTimeout(tile.expiry);tile.loaded=true;
        if(basemapEnabled && [...tiles.values()].every(t=>t.loaded)){basemapFailed=false;$('#basemap-status').classList.remove('map-error');$('#basemap-toggle').textContent='刷新城市底图';$('#basemap-status').textContent=mapSource.name+' 城市底图 · WGS 84 查询 / Web Mercator 显示；拖动或缩放仅改变视野。';}
      });
      el.addEventListener('error',fail,{once:true});
      tile.expiry=setTimeout(fail,10000);
      el.setAttribute('href',mapSource.tile_url.replace('{z}',zoom).replace('{x}',x).replace('{y}',y));
      $('#basemap-tiles').append(el);
    }
    positionTile(tile,key);
  }
  for(const [key,tile] of tiles)if(!needed.has(key)){clearTimeout(tile.expiry);tile.el.remove();tiles.delete(key);}
  if([...tiles.values()].some(tile=>!tile.loaded&&!tile.failed)&&!basemapFailed)$('#basemap-status').textContent='正在加载 '+mapSource.name+' 城市底图…';
}

function renderRadius() {
  const applied=Object.fromEntries(state.exportSnapshot?.filters||[]),keys=['center_lon','center_lat','radius_m'];
  drawRadius($('#radius-circle'),applied);
  const pending=state.spatial==='radius'?Object.fromEntries(keys.map(key=>[key,fields[key].value])):{};
  drawRadius($('#pending-radius'),keys.some(key=>String(pending[key])!==String(applied[key]))?pending:{});
}
function drawRadius(path,p) {
  if(!p.radius_m||p.center_lon===''||p.center_lat===''){path.setAttribute('hidden','');return;}
  const lon=Number(p.center_lon)*Math.PI/180,lat=Number(p.center_lat)*Math.PI/180,angular=Number(p.radius_m)/6371008.8;
  if(![lon,lat,angular].every(Number.isFinite)||Math.abs(Number(p.center_lon))>180||Math.abs(Number(p.center_lat))>90||Number(p.radius_m)<1||Number(p.radius_m)>50000){path.setAttribute('hidden','');return;}
  const points=[];
  for(let i=0;i<=72;i++){const bearing=i/72*Math.PI*2;const nextLat=Math.asin(Math.sin(lat)*Math.cos(angular)+Math.cos(lat)*Math.sin(angular)*Math.cos(bearing));const nextLon=lon+Math.atan2(Math.sin(bearing)*Math.sin(angular)*Math.cos(lat),Math.cos(angular)-Math.sin(lat)*Math.sin(nextLat));points.push(xy(nextLon*180/Math.PI,nextLat*180/Math.PI));}
  path.removeAttribute('hidden');path.setAttribute('d',points.map(([x,y],index)=>(index?'L':'M')+x+','+y).join(' ')+' Z');
}
for(const name of ['center_lon','center_lat','radius_m'])fields[name].addEventListener('input',renderMap);
ensureClusterControls();$('#basemap-toggle').textContent='刷新城市底图';$('#basemap-toggle').setAttribute('aria-pressed','true');$('#map-attribution').hidden=false;$('#basemap-status').textContent='城市底图将按当前视野自动加载。';
setSpatial('none');restoreQuery();renderMap();system.boot();
