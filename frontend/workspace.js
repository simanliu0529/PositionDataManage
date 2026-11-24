"use strict";
// Enhancements use existing authenticated queries and immutable batch reports.
window.workspace={
  track:null,trackController:null,trackGeneration:0,trackMessage:'',lastError:null,batches:new Map(),comparisonGeneration:0,choicesGeneration:0,
  signature(snapshot=state.exportSnapshot){return snapshot?JSON.stringify([snapshot.batchId,[...snapshot.filters].sort()]):'';},
  draft(){
    try{const filters=collectFilters(),snapshot=state.exportSnapshot;return{filters,dirty:!snapshot||JSON.stringify([...filters].sort())!==JSON.stringify([...snapshot.filters].sort())||Number(fields.limit.value)!==(snapshot.limit||state.limit),valid:true};}
    catch{return{filters:null,dirty:true,valid:false};}
  },
  refreshDraft(){
    const draft=this.draft(),snapshot=state.exportSnapshot;
    nodeById('query-draft-status').textContent=state.busy?'正在查询，实线仍表示最近成功查询的范围。':draft.dirty?'条件已修改，尚未提交。橙色虚线为待提交范围，蓝色实线为最近成功查询范围。':'条件已应用。蓝色实线为已查询范围；拖动、缩放和翻页保持当前视野。';
    nodeById('query-draft-status').classList.toggle('pending-notice',draft.dirty&&!state.busy);
    nodeById('share-query').disabled=!snapshot||state.busy||!system.user;
    const p=new URLSearchParams(snapshot?.filters||[]);
    nodeById('load-track').disabled=!system.user||state.busy||draft.dirty||!p.get('user_id')||!p.get('start')||!p.get('end')||Boolean(this.trackController);
    if(!this.track&&!this.trackController)nodeById('track-status').textContent=this.trackMessage||'最近成功查询须限定单用户、开始和结束时间，且当前表单与查询一致。仅连接同一 UTC 日期内相隔不超过 6 小时的签到，非实际移动路线。';
    renderMap();
  },
  renderOverlays(){
    const inside=state.items.filter(row=>row.longitude>=view.west&&row.longitude<=view.east&&row.latitude>=view.south&&row.latitude<=view.north).length;
    nodeById('view-result-hint').textContent=state.busy?'正在查询，地图视野保持不变。':this.lastError?'查询未返回结果，请查看筛选区的错误提示。':state.items.length?inside?`本页 ${state.items.length} 条签到中，${inside} 条在当前视野内。可点击“定位结果”查看本页全部点位。`:'本页结果均在当前视野外；点击地图右侧“定位结果”查看，查询条件不会改变。':state.loaded?'本次没有匹配记录，请调整条件后重新提交。':'提交条件后展示结果，拖动地图只改变视野。';
    const draft=this.draft(),pending=nodeById('pending-box'),applied=new URLSearchParams(state.exportSnapshot?.filters||[]);
    const keys=['west','east','south','north'];
    const show=draft.valid&&draft.filters.has('west')&&keys.some(k=>draft.filters.get(k)!==applied.get(k));
    pending.toggleAttribute('hidden',!show);
    if(show)drawBox(pending,...['west','south','east','north'].map(k=>Number(draft.filters.get(k))));
    const path=nodeById('checkin-track');path.toggleAttribute('hidden',!this.track||!nodeById('show-track').checked);
    if(this.track){let d='';for(const segment of this.track.segments){d+=segment.map((point,i)=>(i?'L':'M')+xy(...point).join(',')).join(' ');}path.setAttribute('d',d);}
  },
  querySucceeded(){
    this.lastError=null;
    if(this.track?.signature!==this.signature()){this.trackGeneration++;this.trackController?.abort();this.trackController=null;this.track=null;this.trackMessage='';nodeById('show-track').disabled=true;}
  },
  queryFailed(error){this.lastError={code:error.code||'NETWORK_ERROR',request_id:error.requestId||null,http_status:error.status||null};},
  clear(){
    this.trackGeneration++;this.trackController?.abort();this.trackController=null;this.track=null;this.trackMessage='';this.lastError=null;this.batches.clear();this.comparisonGeneration++;this.choicesGeneration++;
    for(const id of ['comparison-rows','compare-left','compare-right'])nodeById(id)?.replaceChildren();
    for(const id of ['comparison-status','query-tool-status','track-status'])if(nodeById(id))nodeById(id).textContent='';
    nodeById('copy-content').value='';if(nodeById('copy-dialog').open)nodeById('copy-dialog').close();nodeById('show-track').disabled=true;
  },
  async copy(text,success){
    const generation=system.sessionGeneration;
    try{await navigator.clipboard.writeText(text);if(generation===system.sessionGeneration)nodeById('query-tool-status').textContent=success;}
    catch{if(generation!==system.sessionGeneration)return;nodeById('copy-content').value=text;nodeById('copy-dialog').showModal();nodeById('copy-content').select();nodeById('query-tool-status').textContent='自动复制受限，请在弹窗中手动复制。';}
  },
  share(){
    const s=state.exportSnapshot;if(!s||state.busy)return;
    const url=new URL(location.origin+'/');url.searchParams.set('share','1');url.searchParams.set('batch_id',s.batchId);url.searchParams.set('limit',String(s.limit||50));
    for(const [key,value] of s.filters)url.searchParams.set(key,value);url.hash='map';
    return this.copy(url.href,'已复制最近成功查询链接：固定条件与批次，打开后从第一页查询；接收者须登录。');
  },
  diagnostics(){
    const s=state.exportSnapshot;
    return this.copy(JSON.stringify({created_at:new Date().toISOString(),page:'checkin-map',error:this.lastError,requested_filters:Object.fromEntries(state.filters),last_success:s?{batch_id:s.batchId,filters:Object.fromEntries(s.filters),limit:s.limit||state.limit}:null,view:{...view},displayed_rows:state.items.length},null,2),'已复制查询诊断，不含密码或会话；内容含筛选条件，分享前请核对。');
  },
  async loadTrack(){
    const snapshot=state.exportSnapshot,p=new URLSearchParams(snapshot?.filters||[]);
    if(!snapshot||this.draft().dirty||!p.get('user_id')||!p.get('start')||!p.get('end')||this.trackController)return;
    const signature=this.signature(),generation=++this.trackGeneration,session=system.sessionGeneration;
    this.trackController=new AbortController();const controller=this.trackController;
    p.set('batch_id',snapshot.batchId);p.set('format','geojson');nodeById('track-status').textContent='正在读取该用户在已应用条件内的全部签到，最多 10,000 条；地图视野保持不变…';this.refreshDraft();
    try{
      const result=await system.request('/api/v2/checkins/export?'+p,{signal:AbortSignal.any([controller.signal,AbortSignal.timeout(30000)])});
      if(generation!==this.trackGeneration||session!==system.sessionGeneration)return;
      if(result.metadata?.batch_id!==snapshot.batchId||!Array.isArray(result.features)||result.features.length>10000)throw new Error('连线数据的批次或数量无效，已停止显示。');
      const rows=result.features.map(feature=>({point:feature.geometry?.coordinates,time:feature.properties?.checkin_time,user:feature.properties?.user_id,id:feature.id}));
      if(rows.some(r=>r.user!==new URLSearchParams(snapshot.filters).get('user_id')||!Array.isArray(r.point)||r.point.length!==2||!r.point.every(Number.isFinite)||!Number.isFinite(Date.parse(r.time))))throw new Error('连线中包含不符合单用户约束或坐标无效的记录。');
      rows.sort((a,b)=>Date.parse(a.time)-Date.parse(b.time)||String(a.id).localeCompare(String(b.id)));
      const segments=[];let segment=[];let previous=null;
      for(const row of rows){const seconds=previous?(Date.parse(row.time)-Date.parse(previous.time))/1000:0;const join=previous&&row.time.slice(0,10)===previous.time.slice(0,10)&&seconds>0&&seconds<=21600&&Math.abs(row.point[0]-previous.point[0])<=180;
        if(!join){if(segment.length>1)segments.push(segment);segment=[];}segment.push(row.point);previous=row;}
      if(segment.length>1)segments.push(segment);
      this.track={signature,segments,records:rows.length};nodeById('show-track').checked=true;nodeById('show-track').disabled=false;
      nodeById('track-status').textContent=`已读取 ${rows.length} 条签到，形成 ${segments.length} 段顺序连线。固定批次 ${snapshot.batchId}；沿用全部筛选，不连接跨 UTC 日、同刻、超过 6 小时或跨 180° 经线的记录。非实际移动路线，点位仍为当前页。`;
    }catch(error){if(generation===this.trackGeneration&&session===system.sessionGeneration){this.track=null;nodeById('show-track').disabled=true;this.trackMessage='连线未加载：'+(error.name==='TimeoutError'?'请求超时，请缩小时间范围。':error.message);nodeById('track-status').textContent=this.trackMessage;}}
    finally{if(generation===this.trackGeneration){this.trackController=null;this.refreshDraft();}}
  },
  renderProgress(job){
    const element=nodeById('job-progress');element.hidden=['succeeded','failed'].includes(job.status);
    if(element.hidden)return;
    const labels={queued:'等待后台工作进程',validating:'校验原文件与可用空间',cleaning:'解析、标准化与团队清洗',loading:'团队模块写入并建立索引',verifying:'核验分片完整性、归属与节点计数',publishing:'生成分类目录并核验发布'};
    const progress=job.progress?.stage===job.status?job.progress:null;
    if(progress&&Number.isFinite(progress.total)&&progress.total>0&&Number.isFinite(progress.completed)){element.max=progress.total;element.value=progress.completed;}
    else element.removeAttribute('value');
    const detail=progress?.detail||labels[job.status]||'后台处理中';
    const amount=progress?.total>0?` · ${progress.completed} / ${progress.total} ${progress.unit||'项'}`:'';
    const load=job.status==='loading'&&job.load_progress?` · 已处理 ${Number(job.load_progress.processed_rows||0).toLocaleString()} 条（${job.load_progress.stage==='loading'?'写入记录':'建立索引'}）`:'';
    nodeById('job-outcome').textContent=detail+amount+load+'。任务在后台继续，可离开页面；尚未测得的进度与剩余时间不估算。';
  },
  async loadBatchChoices(){
    if(system.user?.role!=='admin')return;const session=system.sessionGeneration,generation=++this.choicesGeneration;
    try{const data=await system.request('/api/v2/batches?limit=200');if(session!==system.sessionGeneration||generation!==this.choicesGeneration)return;
      this.batches=new Map(data.items.filter(b=>['active','historical'].includes(b.status)).map(b=>[b.batch_id,b]));
      const previous=[nodeById('compare-left').value,nodeById('compare-right').value],ids=[...this.batches.keys()];
      for(const [i,id] of ['compare-left','compare-right'].entries()){const select=nodeById(id);select.replaceChildren();for(const batch of this.batches.values()){const option=document.createElement('option');option.value=batch.batch_id;option.textContent=(batch.is_active?'当前 · ':'历史 · ')+batch.filename+' · '+batch.batch_id;select.append(option);}select.value=ids.includes(previous[i])?previous[i]:i===0&&ids.includes(data.active_batch_id)?data.active_batch_id:ids.find(key=>key!==nodeById('compare-left').value)||ids[0]||'';}
      nodeById('compare-batches').disabled=ids.length<2;nodeById('comparison-status').textContent=ids.length<2?'至少需要两个已发布批次才能比较。':'比较最近 200 个批次中的已发布版本，只读取摘要，不切换活动数据。';
    }catch(error){if(session===system.sessionGeneration&&generation===this.choicesGeneration)nodeById('comparison-status').textContent=error.message;}
  },
  async compareBatches(){
    const left=nodeById('compare-left').value,right=nodeById('compare-right').value;
    if(!left||!right||left===right){nodeById('comparison-rows').replaceChildren();nodeById('comparison-status').textContent='请选择两个不同的已发布批次。';return;}
    const generation=++this.comparisonGeneration,session=system.sessionGeneration;nodeById('compare-batches').disabled=true;nodeById('comparison-rows').replaceChildren();nodeById('comparison-status').textContent='正在读取两个版本的质量、来源与时间摘要…';
    try{
      const read=async id=>{const batch=this.batches.get(id);if(!batch)throw new Error('批次列表已变化，请刷新。');const [overview,job]=await Promise.all([system.request('/api/v2/overview?batch_id='+encodeURIComponent(id)),system.request('/api/v2/imports/'+encodeURIComponent(batch.job_id))]);if(overview.batch_id!==id)throw new Error('批次响应不一致。');return{batch,overview,job};};
      const [a,b]=await Promise.all([read(left),read(right)]);if(generation!==this.comparisonGeneration||session!==system.sessionGeneration)return;
      const specs=[['批次',v=>v.batch.batch_id],['源文件',v=>v.job.filename],['源文件 SHA-256',v=>v.job.source_hash],['规则版本',v=>v.job.rules_version],['发布时间',v=>v.overview.published_at?new Date(v.overview.published_at*1000).toISOString():null],['原始记录',v=>v.overview.quality.raw],['规则剔除',v=>v.overview.quality.rejected],['去重',v=>v.overview.quality.duplicates],['入库记录',v=>v.overview.total_count],['用户数',v=>v.overview.entities.users],['地点数',v=>v.overview.entities.locations],['月份数',v=>v.overview.entities.months],['时间起点',v=>v.overview.time_range.start],['时间终点',v=>v.overview.time_range.end],['节点 0',v=>v.overview.nodes.find(n=>n.node===0)?.count],['节点 1',v=>v.overview.nodes.find(n=>n.node===1)?.count]];
      const rules=new Set([...Object.keys(a.job.reasons||{}),...Object.keys(b.job.reasons||{})]);for(const rule of rules)specs.push(['异常：'+(rejectionLabels[rule]||rule),v=>v.job.reasons?.[rule]?.count??null]);
      for(const [label,get] of specs){const x=get(a),y=get(b),tr=document.createElement('tr');const difference=typeof x==='number'&&typeof y==='number'?(y-x>0?'+':'')+(y-x):x==null||y==null?'无法比较':x===y?'相同':'不同';for(const value of [label,x??'暂无统计',y??'暂无统计',difference]){const td=document.createElement('td');td.textContent=String(value);tr.append(td);}nodeById('comparison-rows').append(tr);}
      nodeById('comparison-status').textContent='已完成摘要比较，差值为右侧减左侧。源文件与规则相同、计数相同也不代表逐条坐标完全相同；历史缺失指标显示“暂无统计”，未切换活动批次。';
    }catch(error){if(generation===this.comparisonGeneration&&session===system.sessionGeneration)nodeById('comparison-status').textContent='比较失败：'+error.message;}
    finally{if(generation===this.comparisonGeneration&&session===system.sessionGeneration)nodeById('compare-batches').disabled=false;}
  }
};
const progress=document.createElement('progress');progress.id='job-progress';progress.hidden=true;progress.setAttribute('aria-label','当前任务阶段进度');nodeById('job-timings').before(progress);
const comparison=document.createElement('article');comparison.className='card comparison-card';
comparison.innerHTML='<h2>批次摘要对比</h2><div class="comparison-choices"><label>左侧版本<select id="compare-left"></select></label><label>右侧版本<select id="compare-right"></select></label></div><div class="actions"><button id="compare-batches" class="secondary" disabled>比较摘要</button></div><p id="comparison-status" class="hint" role="status">请选择两个已发布版本。</p><div class="table-wrap"><table><thead><tr><th>指标</th><th>左侧版本</th><th>右侧版本</th><th>右减左 / 状态</th></tr></thead><tbody id="comparison-rows"></tbody></table></div>';
nodeById('batches').append(comparison);nodeById('compare-batches').onclick=()=>workspace.compareBatches();
for(const id of ['compare-left','compare-right'])nodeById(id).onchange=()=>{workspace.comparisonGeneration++;nodeById('comparison-rows').replaceChildren();nodeById('comparison-status').textContent='版本选择已改变，请重新比较。';nodeById('compare-batches').disabled=false;};
form.addEventListener('input',()=>workspace.refreshDraft());form.addEventListener('change',()=>workspace.refreshDraft());
nodeById('share-query').onclick=()=>workspace.share();nodeById('copy-diagnostics').onclick=()=>workspace.diagnostics();nodeById('load-track').onclick=()=>workspace.loadTrack();nodeById('show-track').onchange=()=>renderMap();
nodeById('refresh-batches').addEventListener('click',()=>workspace.loadBatchChoices());
workspace.refreshDraft();
if(location.hash==='#batches'&&system.user?.role==='admin')workspace.loadBatchChoices();
