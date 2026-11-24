"""Disk-backed full exports. Read-only bounded node windows; no schema changes."""
import csv
import json
import logging
import os
import re
import threading
import time
import uuid
from urllib.parse import urlencode

from filelock import FileLock, Timeout
from flask import g, jsonify, request, send_file

from backend.control import APIError
from backend.data_services import CSV_FIELDS, _spreadsheet_text
from backend.spatial import params
from backend.worker import node_request
from db.load import shard_for

RETENTION = 86400


def replace_file(source, target):
    # Windows readers/antivirus may briefly deny delete-sharing on the target.
    # Retain atomic publication; never fall back to overwriting the live JSON.
    for attempt in range(10):
        try:
            os.replace(source,target)
            return
        except PermissionError:
            if attempt==9: raise
            time.sleep(.02*(attempt+1))


class ExportJobs:
    def __init__(self, store):
        self.store = store
        self.root = store.root / 'exports'
        self.lock = FileLock(str(store.root / '.full-export.lock'), thread_local=False)
        self.creation_guard = threading.Lock()
        self.metadata_lock = threading.RLock()

    def save(self, job):
        path = self.root / (job['job_id'] + '.json')
        temporary = path.with_suffix('.tmp')
        with self.metadata_lock:
            temporary.write_text(json.dumps(job, ensure_ascii=True), encoding='utf-8')
            replace_file(temporary, path)

    def read(self, job_id, owner):
        if not re.fullmatch(r'E-[a-f0-9]{32}', job_id):
            raise APIError(404, 'EXPORT_NOT_FOUND', '导出任务不存在。')
        try:
            with self.metadata_lock:
                job = json.loads((self.root / (job_id + '.json')).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise APIError(404, 'EXPORT_NOT_FOUND', '导出任务不存在或已清理。') from None
        if job['owner'] != owner:
            raise APIError(404, 'EXPORT_NOT_FOUND', '导出任务不存在。')
        if job['status']=='running' and not self.lock.is_locked:
            # The lock can be owned by another web process. Probe without stealing it.
            probe=FileLock(str(self.store.root / '.full-export.lock'))
            try:
                with probe.acquire(timeout=0):
                    job.update(status='failed',message='服务重启中断了导出，请重新创建任务。')
            except Timeout: pass
        return job

    @staticmethod
    def public(job):
        return {key:value for key,value in job.items() if key!='owner'}

    def create(self, filters, output_format, owner):
        with self.creation_guard:
            if self.lock.is_locked:
                raise APIError(409, 'EXPORT_BUSY', '已有全量导出正在运行，请稍后再试。')
            return self._create(filters,output_format,owner)

    def _create(self, filters, output_format, owner):
        try: self.lock.acquire(timeout=0)
        except Timeout:
            raise APIError(409, 'EXPORT_BUSY', '已有全量导出正在运行，请稍后再试。') from None
        try:
            self.root.mkdir(exist_ok=True)
            for path in self.root.glob('E-*.json'):
                try:
                    old=json.loads(path.read_text(encoding='utf-8'))
                    if old['status']=='running':
                        old.update(status='failed',message='服务重启中断了导出，请重新创建任务。')
                        self.save(old)
                        path.with_suffix('.part').unlink(missing_ok=True)
                    if time.time()-old['created_at']>RETENTION:
                        for suffix in ('.csv','.geojson','.part','.cancel','.json'):
                            path.with_suffix(suffix).unlink(missing_ok=True)
                except (OSError, ValueError, KeyError):
                    logging.getLogger(__name__).warning('Could not clean an old export')
            job={'job_id':'E-'+uuid.uuid4().hex,'owner':owner,'status':'running',
                 'batch_id':filters['batch_id'],'filters':filters,'format':output_format,
                 'created_at':time.time(),'exported_count':0,'scanned_count':0,
                 'message':'后台导出已开始；按节点与记录 ID 顺序写出。'}
            self.save(job)
            result=self.public(job)
            threading.Thread(target=self.run,args=(job,),daemon=True).start()
            return result
        except Exception:
            self.lock.release()
            raise

    def run(self, job):
        part=self.root/(job['job_id']+'.part')
        cancel=self.root/(job['job_id']+'.cancel')
        try:
            indexes=[shard_for(job['filters']['user_id'])] if 'user_id' in job['filters'] else [0,1]
            first=True
            with part.open('w',encoding='utf-8-sig' if job['format']=='csv' else 'utf-8',newline='') as out:
                writer=csv.writer(out)
                if job['format']=='csv': writer.writerow(CSV_FIELDS)
                else: out.write('{"type":"FeatureCollection","features":[')
                for index in indexes:
                    after=-(2**63)
                    while True:
                        if cancel.exists(): raise APIError(409,'EXPORT_CANCELLED','已取消导出，未生成不完整文件。')
                        data=node_request(self.store.nodes[index],'/export-chunk?'+urlencode({**job['filters'],'after':after}))
                        rows=data.get('items');end=data.get('next_after');scanned=data.get('scanned');done=data.get('done')
                        if (data.get('batch_id')!=job['batch_id'] or not isinstance(rows,list) or len(rows)>20000
                                or type(end) is not int or type(scanned) is not int or not 0<=scanned<=20000
                                or type(done) is not bool or len(rows)>scanned or (scanned and end<=after)
                                or (not scanned and (not done or end!=after))):
                            raise APIError(502,'INVALID_NODE_RESPONSE','节点导出响应无效，未生成完整文件。')
                        previous=after
                        for row in rows:
                            if (not isinstance(row,dict) or not set(CSV_FIELDS[2:])<=set(row)
                                    or type(row['id']) is not int or not previous<row['id']<=end):
                                raise APIError(502,'INVALID_NODE_RESPONSE','节点记录无效，未生成完整文件。')
                            previous=row['id']
                            if job['format']=='csv':
                                writer.writerow([job['batch_id'],index,row['id'],_spreadsheet_text(row['user_id']),row['checkin_time'],row['latitude'],row['longitude'],_spreadsheet_text(row['location_id'])])
                            else:
                                feature={'type':'Feature','id':f"{index}:{row['id']}",'geometry':{'type':'Point','coordinates':[row['longitude'],row['latitude']]},'properties':{'batch_id':job['batch_id'],'node':index,**{k:row[k] for k in CSV_FIELDS[2:] if k not in ('latitude','longitude')}}}
                                if not first: out.write(',')
                                out.write(json.dumps(feature,ensure_ascii=False,allow_nan=False));first=False
                        job['exported_count']+=len(rows);job['scanned_count']+=scanned
                        self.save(job)
                        after=end
                        if done: break
                if job['format']=='geojson':
                    out.write('],"metadata":'+json.dumps({'batch_id':job['batch_id'],'filters':job['filters'],'exported_count':job['exported_count'],'scope':'all_matching_records','coordinate_system':'WGS84 (EPSG:4326)','order':'node,id'},ensure_ascii=False)+'}')
            if cancel.exists(): raise APIError(409,'EXPORT_CANCELLED','已取消导出，未生成不完整文件。')
            final=part.with_suffix('.'+job['format']);replace_file(part,final)
            job.update(status='succeeded',size_bytes=final.stat().st_size,message='全部匹配记录已导出，可下载。')
        except Exception as error:
            part.unlink(missing_ok=True)
            job.update(status='cancelled' if isinstance(error,APIError) and error.code=='EXPORT_CANCELLED' else 'failed',
                       message=error.message if isinstance(error,APIError) else '导出失败，请检查磁盘空间和服务日志后重新创建任务。')
            if isinstance(error,APIError):
                logging.getLogger(__name__).warning('Export %s stopped: %s',job['job_id'],error.code)
            else:
                logging.getLogger(__name__).exception('Export %s did not complete',job['job_id'])
        finally:
            try:
                job['finished_at']=time.time();self.save(job)
                cancel.unlink(missing_ok=True)
            finally: self.lock.release()


def register_export_jobs(app,store):
    jobs=ExportJobs(store)
    app.export_jobs=jobs

    @app.post('/api/v2/exports')
    def create_export():
        if request.content_length is None or request.content_length>16384 or not request.is_json:
            raise APIError(400,'INVALID_EXPORT','请提交有效导出参数。')
        body=request.get_json()
        if not isinstance(body,dict) or set(body)!={'query','format'} or not isinstance(body['query'],str) or body['format'] not in ('csv','geojson'):
            raise APIError(400,'INVALID_EXPORT','导出格式或条件无效。')
        filters,_,_=params(body['query'])
        filters['batch_id']=store.batch(filters.get('batch_id'))
        return jsonify(jobs.create(filters,body['format'],g.user['user_id'])),202

    @app.get('/api/v2/exports/<job_id>')
    def export_status(job_id):
        return jsonify(jobs.public(jobs.read(job_id,g.user['user_id'])))

    @app.post('/api/v2/exports/<job_id>/cancel')
    def cancel_export(job_id):
        job=jobs.read(job_id,g.user['user_id'])
        if job['status']=='running': (jobs.root/(job_id+'.cancel')).touch()
        return jsonify(status='cancelling' if job['status']=='running' else job['status'])

    @app.get('/api/v2/exports/<job_id>/download')
    def download_export(job_id):
        job=jobs.read(job_id,g.user['user_id'])
        if job['status']!='succeeded': raise APIError(409,'EXPORT_NOT_READY','导出尚未成功，不能下载。')
        path=jobs.root/(job_id+'.'+job['format'])
        if not path.is_file(): raise APIError(410,'EXPORT_EXPIRED','导出文件已清理，请重新导出。')
        return send_file(path,as_attachment=True,download_name=f"checkins-{job['batch_id']}.{job['format']}",mimetype='text/csv' if job['format']=='csv' else 'application/geo+json',conditional=True)
