"use strict";
// The overview reads published-batch statistics. No display-only demo figures.
window.dashboard={
  generation:0,controller:null,
  clear(){this.generation++;this.controller?.abort();document.querySelector('#overview-content')?.replaceChildren();},
  async load(){
    const root=document.querySelector('#overview-content');if(!root||typeof system==='undefined'||!system.user)return;
    const generation=++this.generation;this.controller?.abort();this.controller=new AbortController();
    root.replaceChildren(this.message('正在读取已发布数据概况…','overview-loading'));root.setAttribute('aria-busy','true');
    try{
      const data=await system.request('/api/v2/overview',{signal:AbortSignal.any([this.controller.signal,AbortSignal.timeout(15000)])});
      if(generation!==this.generation||!system.user)return;
      this.render(root,data);
    }catch(error){
      if(generation!==this.generation||!system.user)return;
      const message=this.message('数据概况加载失败：'+(error.name==='TimeoutError'?'请求超时，请检查节点后重试。':error.message),'overview-error');
      const retry=document.createElement('button');retry.className='secondary';retry.textContent='重试概况';retry.onclick=()=>this.load();message.append(retry);root.replaceChildren(message);
    }finally{if(generation===this.generation)root.removeAttribute('aria-busy');}
  },
  message(text,className){const element=document.createElement('div');element.className=className;element.textContent=text;return element;},
  text(tag,text,className=''){const element=document.createElement(tag);element.textContent=text;if(className)element.className=className;return element;},
  count(value){return typeof value==='number'?value.toLocaleString():'暂无统计';},
  render(root,data){
    root.replaceChildren();
    if(!data.has_data){
      const card=this.text('article','','card onboarding');card.append(this.text('span','开始建立可查询的数据集','eyebrow'),this.text('h2','从第一份原始签到文件开始'),this.text('p',system.user.role==='admin'?'先试导入并检查质量报告，再正式发布。核验通过后，即可在地图、分类目录和导出服务中使用同一批数据。':'当前尚未发布数据，请联系管理员导入并发布原始签到文件。'));
      if(system.user.role==='admin'){const link=this.text('a','上传原始数据 →','button');link.href='#imports';card.append(link);}root.append(card);return;
    }
    const stats=this.text('div','','overview-stats');
    for(const [label,value,detail] of [['签到记录',data.total_count,'已发布、已去重'],['签到用户',data.entities.users,'按用户 ID 组织'],['签到地点',data.entities.locations,'按地点 ID 组织'],['时间跨度',data.entities.months,'个 UTC 月份']]){
      const card=this.text('article','','stat-card');card.append(this.text('span',label,'stat-label'),this.text('strong',this.count(value),'stat-value'),this.text('small',detail));stats.append(card);
    }root.append(stats);
    const row=this.text('div','','overview-grid'),trend=this.text('article','','card overview-trend');trend.append(this.text('h2','签到时间分布'),this.text('p','整个已发布批次 · UTC 月份，最近 12 个有数据的月份','hint'));this.renderTrend(trend,data.monthly_trend||[]);row.append(trend);
    const quality=this.text('article','','card overview-quality');quality.append(this.text('h2','清洗与存储质量'));
    const rate=typeof data.quality.retention_rate==='number'?data.quality.retention_rate:null;
    quality.append(this.text('strong',rate===null?'暂无留存统计':rate.toFixed(2)+'%','quality-rate'),this.text('p',data.quality.retention_label||'清洗留存率（不代表数据准确率）','hint'));
    const list=this.text('dl','','quality-list');
    for(const [name,value] of [['原始记录',data.quality.raw],['规则剔除',data.quality.rejected],['重复记录',data.quality.duplicates],['最终入库',data.quality.inserted],['坐标处理（清洗模块报告）',data.quality.coordinate_repairs]])list.append(this.text('dt',name),this.text('dd',this.count(value)));
    quality.append(list,this.text('p',data.nodes_consistent===true?'双节点条数与发布清单一致':data.nodes_consistent===false?'警告：节点条数与发布清单不一致，请联系管理员核查。':'当前批次无可用的节点核验统计',data.nodes_consistent===false?'error':'storage-status'));
    if(data.catalog_consistent===false)quality.append(this.text('p','警告：分类目录与节点记录数未对账，请联系管理员核查。','error'));
    if(data.time_range?.start&&data.time_range?.end)quality.append(this.text('p',`${data.time_range.start} 至 ${data.time_range.end}`,'hint'));
    row.append(quality);root.append(row);
    const ranks=this.text('div','','overview-grid');this.renderTop(ranks,'活跃签到用户',data.top_users||[],'user_id',data.batch_id);this.renderTop(ranks,'热门签到地点',data.top_locations||[],'location_id',data.batch_id);root.append(ranks);
    if(data.tasks){const active=Object.entries(data.tasks.status_counts||{}).filter(([status])=>!['succeeded','failed'].includes(status)).reduce((sum,[,value])=>sum+value,0),failed=data.tasks.status_counts?.failed||0;const line=this.text('p',`导入任务：共 ${this.count(data.tasks.total)} 个 · 处理中 ${this.count(active)} 个 · 失败 ${this.count(failed)} 个`,'overview-task-summary');const link=this.text('a','查看任务与报告 →');link.href='#imports';line.append(document.createTextNode('　'),link);root.append(line);}
    root.append(this.text('p','统计批次 '+data.batch_id+' · 目录、趋势和质量均来自该批次；地图默认显示当前页签到。','hint overview-batch'));
  },
  renderTrend(card,source){
    const months=source.slice().sort((a,b)=>a.month.localeCompare(b.month)).slice(-12);
    if(!months.length){card.append(this.text('p','当前批次没有月份统计。','hint'));return;}
    const max=Math.max(...months.map(entry=>entry.count),1),chart=this.text('div','','month-chart');chart.setAttribute('role','list');chart.setAttribute('aria-label','按 UTC 月份汇总的签到数量');
    for(const entry of months){const bar=this.text('div','','month-bar');bar.setAttribute('role','listitem');bar.title=`${entry.month}：${this.count(entry.count)} 条`;const track=this.text('div','','month-track'),fill=this.text('span','','month-fill');fill.style.height=Math.max(2,entry.count/max*100)+'%';track.append(fill);bar.append(this.text('span',this.count(entry.count),'month-value'),track,this.text('span',entry.month,'month-label'));chart.append(bar);}card.append(chart);
  },
  renderTop(parent,title,entries,key,batchId){
    const card=this.text('article','','card overview-ranking');card.append(this.text('h2',title));
    if(!entries.length)card.append(this.text('p','暂无目录统计。','hint'));
    for(const [index,entry] of entries.slice(0,5).entries()){const button=this.text('button','','ranking-row');button.type='button';button.append(this.text('span',String(index+1),'ranking-index'),this.text('span',entry[key]),this.text('strong',this.count(entry.count)+' 条'));button.onclick=()=>window.openCheckinFilter?.({[key]:entry[key]},batchId);card.append(button);}parent.append(card);
  }
};
