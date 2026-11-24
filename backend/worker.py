"""Persistent import queue and snapshot publication, reusing normalize and db.load."""
import csv
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
from filelock import FileLock, Timeout
from backend.control import APIError, Store, RULES_VERSION
from backend.cleaning_adapter import clean_for_import
from backend.ingestion import canonicalize
from db.load import load, shard_for

RUNNING=('validating','cleaning','loading','verifying','publishing')

def node_request(base,path):
    started=time.perf_counter()
    def fail(status,code,message,error=None):
        # Keep original exception text (which may contain SQL or local paths)
        # out of user responses and log only its class, never filter values.
        try:
            node=urlsplit(base).netloc.rsplit('@',1)[-1]
            route=urlsplit(path).path
        except ValueError:
            node,route='invalid-url','unknown'
        logging.getLogger(__name__).warning(
            'Node request failed node=%s route=%s elapsed_ms=%.3f category=%s cause=%s',
            node,route,
            (time.perf_counter()-started)*1000,code,type(error).__name__ if error else 'invalid_response')
        raise APIError(status,code,message) from error
    try:
        with urlopen(base+path,timeout=8) as response:
            result=json.load(response)
        if not isinstance(result,dict):
            fail(502,'INVALID_NODE_RESPONSE','数据节点返回的数据格式无效，请稍后重试或联系管理员。')
        return result
    except HTTPError as error:
        try:
            with closing(error):
                detail=json.loads(error.read(4096))
        except (OSError,ValueError,TypeError):
            detail={}
        code=detail.get('code') if isinstance(detail,dict) else None
        if error.code==409 and code=='BATCH_UNAVAILABLE':
            fail(409,'BATCH_UNAVAILABLE','目标批次尚未在数据节点就绪，请刷新批次或联系管理员。',error)
        if error.code==504 and code=='QUERY_TIMEOUT':
            fail(504,'QUERY_TIMEOUT','查询超过节点执行时限，请缩小时间或地图范围后重试。',error)
        if error.code in (408,504):
            fail(504,'NODE_TIMEOUT','数据节点响应超时，请缩小查询条件或稍后重试。',error)
        if error.code==429:
            fail(503,'NODE_BUSY','数据节点当前繁忙，请稍后重试。',error)
        fail(502,'NODE_HTTP_ERROR',f'数据节点返回异常响应（HTTP {error.code}），请稍后重试或联系管理员。',error)
    except TimeoutError as error:
        fail(504,'NODE_TIMEOUT','数据节点响应超时，请缩小查询条件或稍后重试。',error)
    except URLError as error:
        if isinstance(error.reason,TimeoutError):
            fail(504,'NODE_TIMEOUT','数据节点响应超时，请缩小查询条件或稍后重试。',error)
        fail(503,'NODE_UNAVAILABLE','无法连接数据节点，请检查节点服务或稍后重试。',error)
    except OSError as error:
        fail(503,'NODE_UNAVAILABLE','无法连接数据节点，请检查节点服务或稍后重试。',error)
    except (ValueError,TypeError) as error:
        fail(502,'INVALID_NODE_RESPONSE','数据节点返回的数据格式无效，请稍后重试或联系管理员。',error)

def probe(store,batch_id,counts):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda base:node_request(base,'/count?'+urlencode({'batch_id':batch_id})),store.nodes))
    if any(row.get('batch_id')!=batch_id or row.get('count')!=counts[i] for i,row in enumerate(results)):
        raise APIError(503,'SHARD_MISMATCH','节点计数与导入清单不符，未发布。')
    return results

def activate(store,batch_id,expected,actor):
    with store.db() as db:
        batch=db.execute("SELECT * FROM batches WHERE batch_id=? AND status IN ('ready','active','historical')",(batch_id,)).fetchone()
        shards=db.execute('SELECT * FROM batch_shards WHERE batch_id=? ORDER BY node_id',(batch_id,)).fetchall()
    if batch is None or len(shards)!=2 or any(not row['ready'] for row in shards):
        raise APIError(409,'BATCH_UNAVAILABLE','批次不存在或尚未核验通过。')
    probe(store,batch_id,[row['row_count'] for row in shards])
    with store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        previous=store.active(db)
        if previous!=expected:
            raise APIError(409,'ACTIVE_BATCH_CHANGED','当前批次已变化，请刷新后重新确认。')
        db.execute("UPDATE batches SET status='historical' WHERE batch_id=?",(previous,))
        db.execute("UPDATE batches SET status='active',published_at=COALESCE(published_at,?) WHERE batch_id=?",(time.time(),batch_id))
        db.execute("UPDATE dataset_state SET active_batch_id=? WHERE dataset_id='gowalla'",(batch_id,))
        db.execute('INSERT INTO audit_log(action,actor,previous_batch_id,batch_id,created_at) VALUES (?,?,?,?,?)',('activate',actor,previous,batch_id,time.time()))
    return {'batch_id':batch_id,'previous_batch_id':previous}

def enqueue(store,path,filename,source_hash,mode,actor,parent=None,options=None):
    if mode not in ('dry_run','replace'): raise APIError(400,'INVALID_MODE','mode 只能是 dry_run 或 replace。')
    options=options or {'format':'tsv','field_mapping':{},'coordinate_system':'WGS84'}
    encoded=json.dumps(options,sort_keys=True,ensure_ascii=False,separators=(',',':'))
    version=RULES_VERSION+':'+hashlib.sha256(encoded.encode()).hexdigest()[:12]
    with store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        prior=db.execute("SELECT job_id FROM import_jobs WHERE source_hash=? AND mode=? AND rules_version=? AND status!='failed'",(source_hash,mode,version)).fetchone()
        if prior: return prior['job_id'],False
        attempt=db.execute('SELECT COALESCE(MAX(attempt),0)+1 FROM import_jobs WHERE source_hash=? AND mode=? AND rules_version=?',(source_hash,mode,version)).fetchone()[0]
        job_id='J-'+uuid.uuid4().hex[:24]
        db.execute('''INSERT INTO import_jobs(job_id,mode,source_hash,source_path,filename,rules_version,status,attempt,created_at,expected_batch_id,parent_job_id,created_by,import_config)
                    VALUES (?,?,?,?,?,?,'queued',?,?,?,?,?,?)''',(job_id,mode,source_hash,str(path),filename,version,attempt,time.time(),store.active(db),parent,actor,encoded))
    return job_id,True

def retry(store,job_id,actor):
    with store.db() as db:
        row=db.execute('SELECT * FROM import_jobs WHERE job_id=?',(job_id,)).fetchone()
    if row is None: raise APIError(404,'JOB_NOT_FOUND','任务不存在。')
    if row['status']!='failed': raise APIError(409,'JOB_NOT_FAILED','只能重试失败任务。')
    return enqueue(store,Path(row['source_path']),row['filename'],row['source_hash'],row['mode'],actor,job_id,options=json.loads(row['import_config']))

def remove_work(store,path):
    path=path.resolve()
    if path.parent!=(store.root/'work').resolve(): raise RuntimeError('Unsafe work directory')
    if path.exists(): shutil.rmtree(path)

def process_job(store,job_id,stop=None):
    with store.db() as db:
        job=dict(db.execute('SELECT * FROM import_jobs WHERE job_id=?',(job_id,)).fetchone())
    report={'scanned_rows':0,'valid_rows':0,'rejected_rows':0,'duplicates':0,'node_counts':[0,0],'reasons':{},'rules_version':job['rules_version'],'timings':{}}
    work=store.root/'work'/job_id
    remove_work(store,work);work.mkdir()
    batch_id='B-'+uuid.uuid4().hex[:24]
    report['staging_batch_id']=batch_id
    start=time.perf_counter();stage='validating';stage_start=start
    heartbeat_stop=threading.Event()
    def heartbeat():
        while not heartbeat_stop.wait(3):
            with store.db() as db: db.execute('UPDATE import_jobs SET heartbeat_at=? WHERE job_id=?',(time.time(),job_id))
    beat=threading.Thread(target=heartbeat,daemon=True);beat.start()
    def change(next_stage):
        nonlocal stage,stage_start
        now=time.perf_counter();report['timings'][stage]=round(now-stage_start,4)
        stage=next_stage;stage_start=now;report['stage_started_at']=time.time();report.pop('progress',None);store.progress(job_id,stage,report)
    def progress(detail,completed=None,total=None,unit='项'):
        report['progress']={'stage':stage,'detail':detail,'completed':completed,'total':total,'unit':unit}
        store.progress(job_id,stage,report)
    try:
        source=Path(job['source_path'])
        actual_hash=hashlib.sha256()
        with source.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''): actual_hash.update(chunk)
        if actual_hash.hexdigest()!=job['source_hash']: raise ValueError('原始文件校验值已变化，请重新上传。')
        for directory in [work,*store.shards]:
            if shutil.disk_usage(directory).free<source.stat().st_size*8+16*1024*1024:
                raise ValueError('暂存空间不足，需预留至少原始文件 8 倍加 16 MiB 的空闲空间。')
        change('cleaning')
        csv_path=work/'clean.csv'
        options=json.loads(job['import_config']) or {'format':'tsv','field_mapping':{},'coordinate_system':'WGS84'}
        canonical=canonicalize(source,work/'canonical.tsv',options,report,lambda:store.progress(job_id,stage,report))
        clean_for_import(canonical,csv_path,report,lambda:store.progress(job_id,stage,report),stop)
        change('loading')
        def database_progress(rows, detail='loading'):
            report['load_progress']={'processed_rows':rows,'stage':detail}
            store.progress(job_id,stage,report)
        result=load(csv_path,work/'loaded',progress=database_progress)
        report['database_import']={key:value for key,value in result.items() if key!='source'}
        report['load_duplicates']=result['duplicates'];report['duplicates']=report['cleaning_duplicates']+result['duplicates'];report['node_counts']=result['node_counts']
        change('verifying')
        progress('核对清洗与入库条数',0,2,'节点')
        if report['valid_rows']!=report['duplicates']+sum(report['node_counts']): raise ValueError('清洗与入库计数对账失败。')
        for index,root in enumerate(store.shards):
            progress(f'核验节点 {index} 的完整性与分片归属',index,2,'节点')
            path=work/'loaded'/f'node-{index}.sqlite'
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
                db.create_function('shard',1,shard_for,deterministic=True)
                if db.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise ValueError('分片完整性校验失败。')
                if db.execute('SELECT 1 FROM checkins WHERE shard(user_id)!=? LIMIT 1',(index,)).fetchone(): raise ValueError('分片归属错误。')
            temporary=root/(batch_id+'.tmp')
            shutil.copyfile(path,temporary);temporary.replace(root/(batch_id+'.sqlite'))
            progress(f'节点 {index} 快照已核验并复制',index+1,2,'节点')
        probe(store,batch_id,report['node_counts'])
        if job['mode']=='replace':
            change('publishing')
            progress('登记待发布批次',0,6,'分类任务')
            with store.db() as db:
                db.execute('INSERT INTO batches VALUES (?,?,\'preparing\',?,?,?,?,?)',(batch_id,job_id,report['scanned_rows'],report['rejected_rows'],report['duplicates'],None,time.time()))
                for i in range(2): db.execute('INSERT INTO batch_shards VALUES (?,?,?,?,1)',(batch_id,i,batch_id+'.sqlite',report['node_counts'][i]))
            catalog_completed=0
            for index,root in enumerate(store.shards):
                with closing(sqlite3.connect((root/(batch_id+'.sqlite')).as_uri()+'?mode=ro',uri=True)) as shard:
                    for group,column in [('user','user_id'),('month','substr(checkin_time,1,7)'),('location','location_id')]:
                        progress(f'汇总节点 {index} 的 '+{'user':'用户','month':'月份','location':'地点'}[group]+'目录',catalog_completed,6,'分类任务')
                        rows=shard.execute(f'SELECT {column},count(*) FROM checkins GROUP BY {column}')
                        while True:
                            chunk=rows.fetchmany(1000)
                            if not chunk: break
                            with store.db() as db:
                                db.executemany('INSERT INTO catalog_counts VALUES (?,?,?,?) ON CONFLICT(batch_id,group_by,key) DO UPDATE SET count=count+excluded.count',[(batch_id,group,key,count) for key,count in chunk])
                        catalog_completed+=1
                        progress('分类目录汇总中',catalog_completed,6,'分类任务')
            with store.db() as db: db.execute("UPDATE batches SET status='ready' WHERE batch_id=?",(batch_id,))
            progress('分类目录完成，核对节点并切换活动版本',6,6,'分类任务')
            activate(store,batch_id,job['expected_batch_id'],job['created_by'])
            with store.db() as db: db.execute('UPDATE import_jobs SET batch_id=? WHERE job_id=?',(batch_id,job_id))
        else:
            for root in store.shards: (root/(batch_id+'.sqlite')).unlink()
        for entry in report['reasons'].values(): entry['percentage']=round(entry['count']/report['scanned_rows']*100,4)
        report['timings'][stage]=round(time.perf_counter()-stage_start,4);report['timings']['total']=round(time.perf_counter()-start,4)
        store.progress(job_id,'succeeded',report)
    except Exception as error:
        report['failed_stage']=stage;report['error']=str(error);report['timings']['total']=round(time.perf_counter()-start,4)
        with store.db() as db:
            published=db.execute("SELECT batch_id FROM batches WHERE batch_id=? AND status IN ('active','historical')",(batch_id,)).fetchone()
            if published:
                # Publication already committed. A later report failure or user
                # rollback must never turn an immutable published snapshot into
                # disposable staging data.
                report['post_publish_warning']=report.pop('error')
                db.execute("UPDATE import_jobs SET status='succeeded',batch_id=?,report=?,heartbeat_at=? WHERE job_id=?",(batch_id,json.dumps(report,ensure_ascii=False),time.time(),job_id))
                return
            db.execute("UPDATE batches SET status='failed' WHERE batch_id=? AND status IN ('ready','preparing')",(batch_id,))
            db.execute('DELETE FROM catalog_counts WHERE batch_id=? AND NOT EXISTS (SELECT 1 FROM batches WHERE batch_id=? AND status IN (\'active\',\'historical\'))',(batch_id,batch_id))
        store.progress(job_id,'failed',report)
        for root in store.shards:
            for suffix in ('.tmp','.sqlite'):
                path=root/(batch_id+suffix)
                if path.exists(): path.unlink()
    finally:
        heartbeat_stop.set();beat.join(timeout=5);remove_work(store,work)

def run_worker(store=None,stop=None):
    store=store or Store();stop=stop or threading.Event()
    # Cross-process OS lock prevents duplicate workers and releases after a crash.
    lock=FileLock(str(store.root/'worker.lock'))
    try: lock.acquire(timeout=0)
    except Timeout: return
    try:
        with store.db() as db:
            rows=db.execute("SELECT job_id,report,status FROM import_jobs WHERE status NOT IN ('queued','succeeded','failed')").fetchall()
            for row in rows:
                published=db.execute("SELECT batch_id FROM batches WHERE job_id=? AND status IN ('active','historical')",(row['job_id'],)).fetchone()
                if published:
                    db.execute("UPDATE import_jobs SET status='succeeded',batch_id=? WHERE job_id=?",(published[0],row['job_id']))
                    continue
                report=json.loads(row['report']);report.update(error='工作进程意外中断，可从原文件重试。',failed_stage=row['status'])
                db.execute("UPDATE import_jobs SET status='failed',report=? WHERE job_id=?",(json.dumps(report,ensure_ascii=False),row['job_id']))
                db.execute("UPDATE batches SET status='failed' WHERE job_id=? AND status IN ('preparing','ready')",(row['job_id'],))
                staged=report.get('staging_batch_id','')
                if re.fullmatch(r'B-[a-f0-9]{24}',staged):
                    db.execute('DELETE FROM catalog_counts WHERE batch_id=?',(staged,))
                    for root in store.shards:
                        for suffix in ('.tmp','.sqlite'):
                            path=root/(staged+suffix)
                            if path.exists():path.unlink()
                remove_work(store,store.root/'work'/row['job_id'])
        while not stop.is_set():
            with store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                row=db.execute("SELECT job_id FROM import_jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
                if row: db.execute("UPDATE import_jobs SET status='validating',run_id=?,heartbeat_at=? WHERE job_id=?",(uuid.uuid4().hex,time.time(),row['job_id']))
            if row: process_job(store,row['job_id'],stop)
            else: stop.wait(.5)
    finally: lock.release()

if __name__=='__main__':
    run_worker()
