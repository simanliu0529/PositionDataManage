"use strict";
const jobLabels={queued:'排队',validating:'校验',cleaning:'清洗',loading:'入库',verifying:'核验',publishing:'发布',succeeded:'已完成',failed:'失败'};
const canonicalFields={user_id:'用户 ID',checkin_time:'签到时间',latitude:'纬度',longitude:'经度',location_id:'地点 ID'};
const rejectionLabels={invalid_coordinates:'经纬度无效',invalid_time:'时间无效',missing_id:'ID 缺失',invalid_field_count:'字段数错误',blank_line:'空白行',null_island:'零点坐标',conflict_same_time:'同用户同一时刻冲突',speed_anomaly:'速度异常'};
const nodeById=id=>document.getElementById(id);
const system={
  user:null,csrf:'',booted:false,sessionGeneration:0,jobTimer:null,jobId:null,jobGeneration:0,
  jobsOffset:0,jobsGeneration:0,batchOffset:0,batchActive:null,batchesGeneration:0,
  uploading:false,previewGeneration:0,previewData:null,previewController:null,exporting:false,exportController:null,
  async request(url,options={}){
    const generation=this.sessionGeneration,headers=new Headers(options.headers);
    if(options.method&&options.method!=='GET'&&this.csrf)headers.set('X-CSRF-Token',this.csrf);
    const response=await fetch(url,{...options,headers,credentials:'same-origin'});
    if(response.status===204)return null;
    let data;try{data=await response.json();}catch{throw new Error(`服务返回了无法读取的响应（HTTP ${response.status}）`);}
    if(!response.ok){const error=new Error((data.message||data.error||'请求失败')+(data.request_id?`（请求编号 ${data.request_id}）`:''));error.status=response.status;error.code=data.code;error.requestId=data.request_id;if(response.status===401&&!url.endsWith('/auth/login')&&generation===this.sessionGeneration)this.showLogin();throw error;}
    return data;
  },
  async boot(){
    try{this.user=await this.request('/api/v2/auth/me');this.csrf=this.user.csrf_token;this.enter();}
    catch(error){this.showLogin();if(error.status!==401)nodeById('login-error').textContent=error.message;}
    finally{this.booted=true;nodeById('login-button').disabled=false;}
  },
  showLogin(){
    this.returnTo=['home','map','catalog','imports','batches'].includes(location.hash.slice(1))?location.hash:'#home';
    this.sessionGeneration++;this.jobsGeneration++;this.batchesGeneration++;this.previewGeneration++;this.previewController?.abort();this.exportController?.abort();
    this.user=null;this.csrf='';this.jobGeneration++;this.jobId=null;this.jobBatch=null;clearTimeout(this.jobTimer);clearTimeout(this.exportTimer);this.exportJobId=null;nodeById('export-download').hidden=true;nodeById('export-download').removeAttribute('href');nodeById('export-cancel').hidden=true;this.uploading=false;this.exporting=false;
    window.workspace?.clear();
    document.body.classList.add('signed-out');window.dashboard?.clear();nodeById('job-report').hidden=true;
    for(const id of ['job-list','batch-rows','reason-rows','catalog-rows','quality-samples','preview-head','preview-rows','mapping-inputs'])nodeById(id).replaceChildren();
    for(const id of ['audit-log','job-meta','job-outcome','job-timings','sample-scope','record-basis','job-list-status','preview-status','export-status'])nodeById(id).textContent='';
    this.previewData=null;nodeById('import-preview').hidden=true;nodeById('upload-form').reset();nodeById('upload-button').disabled=false;
    if(typeof catalogController!=='undefined')catalogController?.abort();
    if(typeof state!=='undefined'){state.controller?.abort();state.controller=null;state.busy=false;state.items=[];state.loaded=false;state.exportSnapshot=null;state.hasPositioned=false;renderResults();nodeById('submit-query').disabled=false;nodeById('latest-query').disabled=false;nodeById('query').removeAttribute('aria-busy');}
    nodeById('cluster-members')?.setAttribute('hidden','');nodeById('login-password').value='';this.updateExportState();
  },
  enter(){
    this.sessionGeneration++;document.body.classList.remove('signed-out');document.body.classList.toggle('viewer',this.user.role!=='admin');
    nodeById('account-name').textContent=`${this.user.username} · ${this.user.role==='admin'?'管理员':'普通用户'}`;this.csrf=this.user.csrf_token;
    this.configureMap();location.hash=this.returnTo||location.hash||'#home';refreshNodes();navigate();this.restoreExport();
  },
  async configureMap(){
    const generation=this.sessionGeneration;
    try{
      const {map}=await this.request('/api/v2/config');if(generation!==this.sessionGeneration||!this.user)return;
      const tileURL=new URL(map.tile_url,location.origin),creditURL=new URL(map.attribution_url,location.origin);
      if(!['http:','https:'].includes(creditURL.protocol)||!(tileURL.protocol==='https:'||tileURL.origin===location.origin)||!['{z}','{x}','{y}'].every(key=>map.tile_url.includes(key)))throw new Error('底图配置无效');
      Object.assign(mapSource,map);const credit=nodeById('map-attribution');credit.replaceChildren();const link=document.createElement('a');link.href=map.attribution_url;link.textContent='© '+map.attribution;link.target='_blank';link.rel='noopener';credit.append(link);window.refreshMapSource?.();
    }catch(error){if(generation===this.sessionGeneration&&this.user)nodeById('basemap-status').textContent='底图配置读取失败，正在尝试默认 OpenStreetMap；若未加载请检查网络并重试。';}
  },
  async logout(){
    try{await this.request('/api/v2/auth/logout',{method:'POST'});}catch(error){nodeById('global-message').textContent=error.message;return;}
    try{sessionStorage.removeItem(QUERY_STORAGE);sessionStorage.removeItem('checkin-job');}catch{}
    state.filters=new URLSearchParams();state.offset=0;state.limit=50;state.batchId=null;state.exportSnapshot=null;form.reset();setSpatial('none');
    catalogBatch=null;catalogOffset=0;catalogPrefix='';this.jobsOffset=0;this.batchOffset=0;this.showLogin();this.returnTo='#home';location.hash='home';
  },
  async importsPage(){
    if(this.user?.role!=='admin')return;const generation=++this.jobsGeneration,session=this.sessionGeneration;
    nodeById('job-list-status').textContent='正在读取任务…';nodeById('jobs-previous').disabled=nodeById('jobs-next').disabled=true;
    try{
      const data=await this.request('/api/v2/imports?limit=20&offset='+this.jobsOffset);if(generation!==this.jobsGeneration||session!==this.sessionGeneration||this.user?.role!=='admin')return;
      const rows=nodeById('job-list');rows.replaceChildren();
      for(const job of data.items){const button=document.createElement('button');button.className='secondary';button.textContent=`${job.filename} · ${jobLabels[job.status]||job.status} · ${job.job_id}`;button.onclick=()=>this.watchJob(job.job_id);rows.append(button);}
      nodeById('job-list-status').textContent=data.items.length?'点击任务查看报告或后台进度。':'暂无任务，请先上传原始签到文件。';nodeById('jobs-previous').disabled=this.jobsOffset===0;nodeById('jobs-next').disabled=!data.has_more;nodeById('jobs-page').textContent='第 '+(this.jobsOffset/20+1)+' 页';
      let saved;try{saved=sessionStorage.getItem('checkin-job');}catch{}if(!this.jobId&&saved)this.watchJob(saved);
    }catch(error){if(generation===this.jobsGeneration&&session===this.sessionGeneration)nodeById('job-list-status').textContent='任务列表读取失败：'+error.message;}
  },
  async watchJob(id){
    clearTimeout(this.jobTimer);const generation=++this.jobGeneration,session=this.sessionGeneration;this.jobId=id;let failures=0;
    try{sessionStorage.setItem('checkin-job',id);}catch{}
    const poll=async()=>{
      try{
        const job=await this.request('/api/v2/imports/'+encodeURIComponent(id));if(generation!==this.jobGeneration||session!==this.sessionGeneration||this.user?.role!=='admin')return;
        failures=0;nodeById('import-error').textContent='';this.renderJob(job,id);
        if(!['succeeded','failed'].includes(job.status))this.jobTimer=setTimeout(poll,2000);
        else{refreshNodes();this.importsPage();if(location.hash==='#home')window.dashboard?.load();}
      }catch(error){
        if(generation!==this.jobGeneration||session!==this.sessionGeneration||!this.user)return;
        if([401,403,404].includes(error.status)){nodeById('import-error').textContent=error.message;if(error.status===404){this.jobId=null;try{sessionStorage.removeItem('checkin-job');}catch{}}return;}
        const delay=Math.min(30000,2000*2**Math.min(failures++,4));nodeById('import-error').textContent=`报告暂时不可读：${error.message}。${delay/1000} 秒后自动重试，后台任务状态不受此提示影响。`;this.jobTimer=setTimeout(poll,delay);
      }
    };await poll();
  },
  renderJob(job,id){
    this.jobBatch=job.batch_id;const stages={queued:'排队',validating:'文件校验',cleaning:'数据清洗',loading:'双节点写入',verifying:'批次核验',publishing:'发布',succeeded:'已完成',failed:'失败'};
    nodeById('job-report').hidden=false;nodeById('job-title').textContent=`任务 ${id} · ${job.mode==='replace'?'正式替换':'试导入'}`;nodeById('job-status').textContent=stages[job.status]||job.status;
    const stageNodes=nodeById('job-stages');stageNodes.replaceChildren();for(const name of ['validating','cleaning','loading','verifying',...(job.mode==='replace'?['publishing']:[])]){const span=document.createElement('span');span.className='badge'+(name===job.status?' current':'');span.textContent=stages[name];stageNodes.append(span);}
    for(const [selector,value] of [['scanned',job.scanned_rows||0],['valid',job.valid_rows||0],['rejected',job.rejected_rows||0],['duplicates',job.duplicates||0]])nodeById(selector).textContent=value.toLocaleString();
    nodeById('job-meta').textContent=`文件 ${job.filename} · 格式 ${job.input_format||'TSV'} · 尝试 ${job.attempt} · 规则 ${job.rules_version} · SHA-256 ${job.source_hash||'校验中'}`;
    nodeById('job-outcome').textContent=job.error?`失败阶段：${stages[job.failed_stage]||job.failed_stage}。${job.error}`:job.status==='succeeded'?`${job.batch_id?'已发布批次 '+job.batch_id:'试导入报告已生成，当前批次保持不变'} · 节点 ${JSON.stringify(job.node_counts)} · 清洗去重 ${job.cleaning_duplicates||0} / 入库去重 ${job.load_duplicates??(job.cleaning_duplicates==null?job.duplicates||0:0)} · 坐标处理（清洗模块报告）${job.cleaning_stats?.coord_repair||0} 条 · 总耗时 ${this.duration(job.timings?.total)}`:job.load_progress?`入库阶段已处理 ${Number(job.load_progress.processed_rows||0).toLocaleString()} 条；任务在后台继续，可离开页面。`:job.cleaning_phase==='upstream_pipeline'?'正在执行分桶、去重、坐标校正与时空检查，可以离开页面。':'任务在服务器继续运行，可以离开页面。';
    nodeById('job-timings').textContent=Object.entries(job.timings||{}).filter(([key])=>key!=='total').map(([key,value])=>`${stages[key]||key} ${this.duration(value)}`).join(' · ');
    const basis=job.record_basis||'原始物理行（含空行）';nodeById('record-basis').textContent='计数口径：'+basis;nodeById('reason-record-label').textContent=(job.input_format&&job.input_format!=='tsv'?'源数据记录序号':'原始物理行号')+'（最多 5 个）';
    const reasons=nodeById('reason-rows');reasons.replaceChildren();for(const [key,value] of Object.entries(job.reasons||{})){const tr=document.createElement('tr');for(const text of [rejectionLabels[key]||key,value.count,((value.count/(job.scanned_rows||1))*100).toFixed(2)+'%',value.sample_lines?.length?value.sample_lines.join('、'):'此规则未提供源记录编号']){const td=document.createElement('td');td.textContent=text;tr.append(td);}reasons.append(tr);}
    if(!reasons.children.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=4;td.textContent=['succeeded','failed'].includes(job.status)?'未报告规则剔除记录。':'等待清洗规则结果…';tr.append(td);reasons.append(tr);}
    nodeById('sample-scope').textContent=job.sample_scope||'仅展示有真实清洗证据的有限样本，不代表完整异常集。';const samples=nodeById('quality-samples');samples.replaceChildren();
    for(const sample of job.quality_samples||[]){const tr=document.createElement('tr'),number=document.createElement('td');number.textContent=sample.record_number;tr.append(number);for(const value of [sample.before,sample.after]){const td=document.createElement('td'),pre=document.createElement('pre');pre.textContent=value==null?'—':typeof value==='string'?value:JSON.stringify(value,null,2);td.append(pre);tr.append(td);}const outcome=document.createElement('td');outcome.textContent=sample.outcome==='not_retained'?'未保留（详见规则汇总）':({rejected:'规则剔除',retained:'已保留',coordinate_repaired:'已保留并校正坐标'}[sample.outcome]||sample.outcome)+(sample.reason?'：'+(rejectionLabels[sample.reason]||sample.reason):'');tr.append(outcome);samples.append(tr);}
    nodeById('samples-empty').hidden=samples.children.length!==0;nodeById('retry-job').hidden=job.status!=='failed';nodeById('job-open-map').hidden=!job.batch_id;
    window.workspace?.renderProgress(job);
    nodeById('download-report').onclick=()=>this.download(new Blob([JSON.stringify(job,null,2)],{type:'application/json'}),id+'.json');
  },
  duration(value){const seconds=Number(value);if(!Number.isFinite(seconds))return'暂无';if(seconds<60)return seconds.toFixed(2)+' 秒';return Math.floor(seconds/60)+' 分 '+(seconds%60).toFixed(1)+' 秒';},
  resetPreview(){this.previewGeneration++;this.previewController?.abort();this.previewData=null;nodeById('import-preview').hidden=true;for(const id of ['preview-head','preview-rows','mapping-inputs'])nodeById(id).replaceChildren();nodeById('mapping-error').textContent='';nodeById('preview-status').textContent='';},
  async previewFile(){
    this.resetPreview();const file=nodeById('upload-form').elements.file.files[0];if(!file)return;const generation=this.previewGeneration,session=this.sessionGeneration;
    nodeById('preview-status').textContent=`${file.name} · ${(file.size/1024/1024).toFixed(2)} MiB · 正在预览前 64 KiB…`;const body=new FormData();body.set('file',file.slice(0,64*1024),file.name);this.previewController=new AbortController();
    try{const preview=await this.request('/api/v2/imports/preview',{method:'POST',body,signal:AbortSignal.any([this.previewController.signal,AbortSignal.timeout(15000)])});if(generation!==this.previewGeneration||session!==this.sessionGeneration||!this.user)return;this.previewData=preview;this.renderPreview(preview);nodeById('preview-status').textContent=`预览格式 ${preview.format.toUpperCase()} · 仅为前部样本，完整文件将重新校验。`;}
    catch(error){if(generation===this.previewGeneration&&session===this.sessionGeneration&&this.user)nodeById('preview-status').textContent='样本预览失败：'+error.message+'。仍可提交文件让服务端自动识别标准字段；自定义字段请先修正预览并完成映射。';}
  },
  renderPreview(data){
    nodeById('import-preview').hidden=false;const head=document.createElement('tr');for(const column of data.columns){const th=document.createElement('th');th.textContent=column;head.append(th);}nodeById('preview-head').replaceChildren(head);const rows=nodeById('preview-rows');rows.replaceChildren();for(const record of data.rows.slice(0,5)){const tr=document.createElement('tr');for(const column of data.columns){const td=document.createElement('td');const value=record?.[column];td.textContent=value==null?'—':typeof value==='object'?JSON.stringify(value):String(value);tr.append(td);}rows.append(tr);}
    const mapping=nodeById('mapping-inputs');mapping.replaceChildren();nodeById('mapping-fields').hidden=data.format==='tsv';if(data.format==='tsv')return;
    for(const [key,label] of Object.entries(canonicalFields)){const wrapper=document.createElement('label');wrapper.textContent=label;const select=document.createElement('select');select.dataset.canonical=key;select.required=true;const empty=document.createElement('option');empty.value='';empty.textContent='选择源字段';select.append(empty);for(const column of data.columns){const option=document.createElement('option');option.value=column;option.textContent=column;select.append(option);}select.value=data.field_mapping?.[key]||'';select.onchange=()=>{try{this.mapping();nodeById('mapping-error').textContent='';}catch(error){nodeById('mapping-error').textContent=error.message;}};wrapper.append(select);mapping.append(wrapper);}
    nodeById('mapping-error').textContent=data.mapping_error||'';
  },
  mapping(){
    if(!this.previewData||this.previewData.format==='tsv')return null;const mapping={};for(const select of nodeById('mapping-inputs').querySelectorAll('select'))mapping[select.dataset.canonical]=select.value;
    if(Object.keys(mapping).length!==5||Object.values(mapping).some(value=>!value)||new Set(Object.values(mapping)).size!==5)throw new Error('请为五个标准字段各选择一个不同的源字段。');return mapping;
  },
  async upload(event){
    event.preventDefault();if(this.uploading||this.user?.role!=='admin')return;const uploadForm=nodeById('upload-form');if(!uploadForm.reportValidity())return;const body=new FormData(uploadForm);
    try{const mapping=this.mapping();if(mapping)body.set('field_mapping',JSON.stringify(mapping));body.set('coordinate_system','WGS84');}catch(error){nodeById('mapping-error').textContent=error.message;return;}
    const generation=this.sessionGeneration;this.uploading=true;nodeById('upload-button').disabled=true;
    try{
      if(body.get('mode')==='replace'){let current;try{current=await this.request('/api/v2/dataset/active');}catch(error){if(error.code!=='NO_ACTIVE_BATCH')throw error;}if(generation!==this.sessionGeneration)return;if(!confirm(`将用完整文件建立新版本，替换当前批次 ${current?.batch_id||'无'}（${current?.total_count||0} 条）。核验通过后发布，历史批次保留供回退。\n相同文件、映射和规则的旧任务会复用；已完成的历史批次须在批次管理中激活。确认继续？`))return;}
      nodeById('import-error').textContent='';nodeById('upload-status').textContent='正在上传文件，请保持页面打开；上传完成后可离开页面。';const job=await this.request('/api/v2/imports',{method:'POST',body});if(generation!==this.sessionGeneration||!this.user)return;
      nodeById('upload-status').textContent=job.reused?(job.status==='succeeded'?'检测到相同文件、模式、映射和规则版本，已复用完成任务，未重新处理或发布。若需切换历史批次，请前往批次管理激活。':'检测到相同任务，已复用进度，请勿重复上传。'):'上传完成，后台自动处理。';this.jobsOffset=0;await this.watchJob(job.job_id);await this.importsPage();
    }catch(error){if(generation===this.sessionGeneration&&this.user){nodeById('import-error').textContent=error.message;nodeById('upload-status').textContent='上传未完成，可检查文件、字段映射和网络后重试。';}}
    finally{if(generation===this.sessionGeneration){this.uploading=false;nodeById('upload-button').disabled=false;}}
  },
  async batchesPage(){
    if(this.user?.role!=='admin')return;const generation=++this.batchesGeneration,session=this.sessionGeneration;nodeById('batch-error').textContent='';nodeById('batch-previous').disabled=nodeById('batch-next').disabled=true;
    try{
      const data=await this.request('/api/v2/batches?limit=10&offset='+this.batchOffset);if(generation!==this.batchesGeneration||session!==this.sessionGeneration||this.user?.role!=='admin')return;this.batchActive=data.active_batch_id;const rows=nodeById('batch-rows');rows.replaceChildren();
      for(const batch of data.items){const tr=document.createElement('tr');for(const value of [batch.batch_id,batch.filename,batch.is_active?'当前批次':({historical:'历史批次',ready:'已就绪',preparing:'准备中',failed:'失败'}[batch.status]||batch.status),batch.raw_count,batch.rejected_count,batch.duplicate_count,batch.nodes.map(node=>`${node.node_id}: ${node.row_count}`).join(' / ')]){const td=document.createElement('td');td.textContent=value;tr.append(td);}const td=document.createElement('td'),report=document.createElement('button');report.textContent='报告';report.className='secondary';report.onclick=()=>{location.hash='imports';this.watchJob(batch.job_id);};td.append(report);
        const activate=document.createElement('button');activate.textContent='激活 / 回退';activate.disabled=batch.is_active||!['ready','historical'].includes(batch.status);activate.className='secondary';activate.onclick=async()=>{if(!confirm(`当前批次：${data.active_batch_id||'无'}\n目标批次：${batch.batch_id}\n源文件：${batch.filename}\n节点条数：${batch.nodes.map(node=>node.row_count).join(' / ')}\n确认切换？`))return;activate.disabled=true;try{await this.request('/api/v2/batches/'+encodeURIComponent(batch.batch_id)+'/activate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_batch_id:data.active_batch_id})});if(session!==this.sessionGeneration)return;await this.batchesPage();window.workspace?.loadBatchChoices();refreshNodes();}catch(error){if(session===this.sessionGeneration){nodeById('batch-error').textContent=error.message;activate.disabled=false;}}};td.append(activate);tr.append(td);rows.append(tr);
      }
      nodeById('batch-empty').hidden=data.items.length!==0;nodeById('batch-previous').disabled=this.batchOffset===0;nodeById('batch-next').disabled=!data.has_more;nodeById('batch-page').textContent='第 '+(this.batchOffset/10+1)+' 页';nodeById('audit-log').textContent=data.audit.map(row=>`${new Date(row.created_at*1000).toISOString()} · ${row.actor} · ${row.previous_batch_id||'无'} → ${row.batch_id}`).join('\n')||'暂无批次切换记录';
    }catch(error){if(generation===this.batchesGeneration&&session===this.sessionGeneration)nodeById('batch-error').textContent=error.message;}
  },
  updateExportState(){if(typeof state==='undefined')return;for(const id of ['export-csv','export-geojson'])nodeById(id).disabled=this.exporting||state.busy||!this.user||!state.loaded||!state.items.length||!state.exportSnapshot;},
  download(blob,name){const url=URL.createObjectURL(blob),anchor=document.createElement('a');anchor.href=url;anchor.download=name;anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000);},
  async exportData(format){
    if(this.exporting||!state.exportSnapshot||state.busy||!state.items.length)return;const snapshot=state.exportSnapshot,params=new URLSearchParams(snapshot.filters);params.set('batch_id',snapshot.batchId);const generation=this.sessionGeneration;this.exporting=true;this.updateExportState();nodeById('export-status').textContent='正在创建后台完整导出任务…';nodeById('export-download').hidden=true;
    try{
      const job=await this.request('/api/v2/exports',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:params.toString(),format}),signal:AbortSignal.timeout(15000)});
      if(generation!==this.sessionGeneration||!this.user)return;
      this.exportJobId=job.job_id;try{sessionStorage.setItem('checkin-export:'+this.user.username,job.job_id);}catch{}
      await this.watchExport(job.job_id);
    }catch(error){if(generation===this.sessionGeneration&&this.user){this.exporting=false;this.updateExportState();nodeById('export-status').textContent='导出任务未创建：'+(error.name==='TimeoutError'?'请求超时，请先刷新导出状态，避免重复创建。':error.message);}}
  },
  restoreExport(){
    if(!this.user)return;let id=this.exportJobId;try{id=id||sessionStorage.getItem('checkin-export:'+this.user.username);}catch{}
    if(id&&/^E-[a-f0-9]{32}$/.test(id)){this.exportJobId=id;this.watchExport(id);}else nodeById('export-status').textContent='后台完整导出全部匹配记录，不受 10000 条限制；当前账号在本标签页暂无导出任务。';
  },
  async watchExport(id){
    clearTimeout(this.exportTimer);const generation=this.sessionGeneration,poll=this.exportPoll=(this.exportPoll||0)+1;
    try{
      const job=await this.request('/api/v2/exports/'+id,{signal:AbortSignal.timeout(15000)});
      if(generation!==this.sessionGeneration||!this.user||this.exportJobId!==id||poll!==this.exportPoll)return;
      this.exporting=job.status==='running';this.updateExportState();nodeById('export-cancel').hidden=!this.exporting;
      nodeById('export-status').textContent=`${job.message} 已扫描 ${job.scanned_count.toLocaleString()} 条，已写出 ${job.exported_count.toLocaleString()} 条 · 固定批次 ${job.batch_id}。`;
      const link=nodeById('export-download');link.hidden=job.status!=='succeeded';link.removeAttribute('href');
      if(job.status==='succeeded'){link.href='/api/v2/exports/'+id+'/download';link.setAttribute('download','');link.textContent=`下载完整 ${job.format.toUpperCase()}（${job.exported_count.toLocaleString()} 条，${(job.size_bytes/1048576).toFixed(1)} MiB）`;}
      if(this.exporting)this.exportTimer=setTimeout(()=>this.watchExport(id),1000);
    }catch(error){if(generation===this.sessionGeneration&&this.user&&this.exportJobId===id&&poll===this.exportPoll){this.exporting=false;this.updateExportState();nodeById('export-download').hidden=true;nodeById('export-cancel').hidden=true;nodeById('export-status').textContent='读取导出状态失败：'+error.message+' 请点“刷新导出状态”；后台任务可能仍在继续。';}}
  },
  async cancelExport(){
    const id=this.exportJobId,generation=this.sessionGeneration;if(!id||!this.user)return;
    try{await this.request('/api/v2/exports/'+id+'/cancel',{method:'POST'});if(generation===this.sessionGeneration){nodeById('export-status').textContent='已请求取消，正在结束当前分块…';this.watchExport(id);}}
    catch(error){if(generation===this.sessionGeneration&&this.user)nodeById('export-status').textContent=error.message;}
  }
};
nodeById('login-form').addEventListener('submit',async event=>{event.preventDefault();const button=nodeById('login-button');button.disabled=true;nodeById('login-error').textContent='';try{system.user=await system.request('/api/v2/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:nodeById('login-username').value.trim(),password:nodeById('login-password').value})});nodeById('login-password').value='';system.enter();}catch(error){nodeById('login-error').textContent=error.message;}finally{button.disabled=false;}});
nodeById('logout').onclick=()=>system.logout();nodeById('upload-form').onsubmit=event=>system.upload(event);nodeById('upload-form').elements.file.onchange=()=>system.previewFile();
nodeById('retry-job').onclick=async()=>{const generation=system.sessionGeneration;nodeById('retry-job').disabled=true;try{const job=await system.request('/api/v2/imports/'+encodeURIComponent(system.jobId)+'/retry',{method:'POST'});if(generation===system.sessionGeneration&&system.user)system.watchJob(job.job_id);}catch(error){if(generation===system.sessionGeneration)nodeById('import-error').textContent=error.message;}finally{nodeById('retry-job').disabled=false;}};
nodeById('job-open-map').onclick=()=>window.openCheckinFilter({},system.jobBatch);nodeById('refresh-jobs').onclick=()=>system.importsPage();nodeById('refresh-batches').onclick=()=>system.batchesPage();
nodeById('jobs-previous').onclick=()=>{system.jobsOffset=Math.max(0,system.jobsOffset-20);system.importsPage();};nodeById('jobs-next').onclick=()=>{system.jobsOffset+=20;system.importsPage();};
nodeById('batch-previous').onclick=()=>{system.batchOffset=Math.max(0,system.batchOffset-10);system.batchesPage();};nodeById('batch-next').onclick=()=>{system.batchOffset+=10;system.batchesPage();};
nodeById('export-csv').onclick=()=>system.exportData('csv');nodeById('export-geojson').onclick=()=>system.exportData('geojson');
nodeById('export-cancel').onclick=()=>system.cancelExport();nodeById('export-resume').onclick=()=>system.restoreExport();
