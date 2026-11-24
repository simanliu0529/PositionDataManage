"""Web-report adapter around the unmodified upstream complete cleaner."""
import os
import csv
import time
from cleaning.clean import clean, normalize, NULL_ISLAND_EPS, FIELDS

REJECTIONS=('blank_line','invalid_field_count','missing_id','invalid_time','invalid_coordinates','null_island','conflict_same_time','speed_anomaly')

def clean_for_import(source,target,report,progress,stop=None):
    samples={}
    evidence=[]
    valid_samples=0
    maximum=int(os.environ.get('CHECKIN_MAX_LINE_BYTES',65536))
    report['cleaning_phase']='preflight'
    last=time.monotonic()
    # Safety and line-number observation only. Filtering is done by upstream clean().
    with source.open('rb') as stream:
        while True:
            raw=stream.readline(maximum+1)
            if not raw: break
            report['scanned_rows']+=1
            if len(raw)>maximum: raise ValueError('单行超过配置上限，整批导入已停止。')
            text=raw.decode('utf-8-sig' if report['scanned_rows']==1 else 'utf-8').rstrip('\r\n')
            reason=None
            values=[]
            normalized=None
            try:
                if not text.strip(): reason='blank_line'
                else:
                    values=text.split('\t')
                    if len(values)!=5: raise ValueError('invalid_field_count')
                    normalized=normalize(values)
                    _,_,lat,lon,_=normalized
                    if abs(lat)<NULL_ISLAND_EPS and abs(lon)<NULL_ISLAND_EPS: reason='null_island'
            except ValueError as error: reason=str(error)
            if reason:
                lines=samples.setdefault(reason,[])
                if len(lines)<5:
                    lines.append(report['scanned_rows'])
                    evidence.append({'record_number':report['scanned_rows'],'before':dict(zip(FIELDS,[str(v)[:256] for v in values])) if len(values)==5 else text[:256],'after':None,'outcome':'rejected','reason':reason})
            elif valid_samples<20:
                valid_samples+=1
                evidence.append({'record_number':report['scanned_rows'],'before':dict(zip(FIELDS,[str(v)[:256] for v in values])),'after':None,'outcome':'not_retained','_key':tuple(normalized[i] for i in (0,1,4))})
            if time.monotonic()-last>.5: progress();last=time.monotonic()
            if stop and stop.is_set(): raise ValueError('工作进程已停止，可重试任务。')
    report['cleaning_phase']='upstream_pipeline';progress()
    stats=clean(source,target)
    # Match a bounded set of actual source records to the actual cleaner output.
    # Unmatched valid samples are not assigned an invented rejection reason.
    waiting={}
    for item in evidence:
        key=item.pop('_key',None)
        if key is not None: waiting.setdefault(key,[]).append(item)
    with target.open(encoding='utf-8',newline='') as stream:
        for row in csv.DictReader(stream):
            candidates=waiting.get((row['user_id'],row['checkin_time'],row['location_id']))
            if candidates:
                item=candidates.pop(0)
                item['after']=row
                before=item['before']
                coordinate_change=any(abs(float(before[k])-float(row[k]))>1e-6 for k in ('latitude','longitude'))
                item['outcome']='coordinate_repaired' if coordinate_change else 'retained'
    report['quality_samples']=evidence
    parse_samples={sample['record_number']:sample for sample in report.get('parse_rejections',[])}
    for item in evidence:
        if item['record_number'] in parse_samples:
            sample=parse_samples[item['record_number']]
            item['before']=sample['source'];item['parse_detail']=sample['reason']
    report['sample_scope']='前 20 条字段有效记录及每种字段异常最多 5 条；未保留样本的精确时空剔除原因由汇总提供。'
    report['cleaning_stats']=stats
    report['cleaning_duplicates']=stats.get('duplicate',0)
    output=stats.get('filtered_output',stats.get('output',0))
    rejected=sum(stats.get(key,0) for key in REJECTIONS)
    if 'filtered_output' in stats: rejected+=stats['output']-stats['filtered_output']
    if report['scanned_rows']!=output+report['cleaning_duplicates']+rejected:
        raise ValueError('上游清洗计数无法对账，已阻止发布。')
    report['rejected_rows']=rejected
    report['valid_rows']=report['scanned_rows']-rejected
    report['duplicates']=report['cleaning_duplicates']
    report['cleaning_output_rows']=output
    report['reasons']={key:{'count':stats[key],'sample_lines':samples.get(key,[]),'line_samples_available':key in samples} for key in REJECTIONS if stats.get(key,0)}
    report['cleaning_phase']='completed';progress()
    if output==0: raise ValueError('最新版清洗后没有可入库记录，已阻止发布。')
    return stats
