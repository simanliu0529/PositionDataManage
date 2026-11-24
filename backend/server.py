"""Authenticated web application. Run with python -m backend.app."""
import argparse
import atexit
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode
from flask import Flask, request, jsonify, g, send_file
from werkzeug.exceptions import HTTPException
from backend.control import Store, APIError, ROOT
from backend.security import login, session, digest
from backend.spatial import params
from backend.worker import enqueue, retry, activate, node_request
from db.load import shard_for
from backend.ingestion import import_config, preview


def create_app(store=None):
    store=store or Store()
    app=Flask(__name__,static_folder=None)
    maximum=int(os.environ.get('CHECKIN_MAX_UPLOAD_BYTES',1024*1024*1024))
    app.config.update(MAX_CONTENT_LENGTH=maximum+1024*1024,MAX_FORM_MEMORY_SIZE=1024*1024,MAX_FORM_PARTS=8)
    app.store=store
    upload_slots=threading.BoundedSemaphore(int(os.environ.get('CHECKIN_UPLOAD_SLOTS',2)))

    @app.before_request
    def guard():
        g.request_id='r_'+uuid.uuid4().hex[:16]
        if request.path.startswith('/api/'):
            if request.method not in ('GET','HEAD'):
                origin=request.headers.get('Origin')
                if origin and origin.rstrip('/')!=request.host_url.rstrip('/'):
                    raise APIError(403,'CSRF_FAILED','请求来源不匹配。')
            if request.path=='/api/v2/auth/login' and request.method=='POST': return
            g.user=session(store,request.cookies.get('checkin_session'))
            if request.method not in ('GET','HEAD') and not secrets.compare_digest(request.headers.get('X-CSRF-Token',''),g.user['csrf']):
                raise APIError(403,'CSRF_FAILED','请求校验失败，请刷新登录状态后重试。')
            if request.path.startswith(('/api/v2/imports','/api/v2/batches')) and g.user['role']!='admin':
                raise APIError(403,'FORBIDDEN','仅管理员可执行此操作。')

    @app.after_request
    def headers(response):
        response.headers['X-Request-ID']=g.request_id
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='strict-origin-when-cross-origin'
        response.headers['Content-Security-Policy']="default-src 'self'; img-src 'self' https: data:; style-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.path.startswith('/api/'): response.headers['Cache-Control']='no-store'
        return response

    @app.errorhandler(APIError)
    def api_error(error):
        if error.status >= 500:
            app.logger.warning('Request %s failed code=%s path=%s query=%s',
                               g.request_id, error.code, request.path,
                               request.query_string.decode('utf-8', errors='replace')[:2048])
        return jsonify(code=error.code,message=error.message,request_id=g.request_id),error.status

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(code='UPLOAD_TOO_LARGE' if error.code==413 else 'HTTP_ERROR',message='文件超过上传限制。' if error.code==413 else '请求格式或路径无效。',request_id=g.request_id),error.code

    @app.errorhandler(Exception)
    def unexpected(error):
        app.logger.exception('Request %s failed',g.request_id)
        return jsonify(code='INTERNAL_ERROR',message='服务处理失败，请提供请求编号排查。',request_id=g.request_id),500

    @app.get('/')
    def index(): return send_file(ROOT/'frontend'/'index.html',max_age=0)

    @app.get('/healthz')
    def health(): return jsonify(status='ready',service='checkin-web')

    @app.get('/workspace.js')
    def workspace_javascript(): return send_file(ROOT/'frontend'/'workspace.js',max_age=0)

    @app.get('/app.js')
    def javascript(): return send_file(ROOT/'frontend'/'app.js',max_age=0)

    @app.get('/system.js')
    def system_javascript(): return send_file(ROOT/'frontend'/'system.js',max_age=0)

    @app.get('/dashboard.js')
    def dashboard_javascript(): return send_file(ROOT/'frontend'/'dashboard.js',max_age=0)

    @app.get('/style.css')
    def stylesheet(): return send_file(ROOT/'frontend'/'style.css',max_age=0)

    @app.get('/api/v2/config')
    def config():
        return jsonify(map={
            'name':os.environ.get('CHECKIN_MAP_NAME','OpenStreetMap'),
            'tile_url':os.environ.get('CHECKIN_TILE_URL','https://tile.openstreetmap.org/{z}/{x}/{y}.png'),
            'attribution':os.environ.get('CHECKIN_MAP_ATTRIBUTION','OpenStreetMap contributors'),
            'attribution_url':os.environ.get('CHECKIN_MAP_ATTRIBUTION_URL','https://www.openstreetmap.org/copyright')})

    @app.get('/tiles/<int:z>/<int:x>/<int:y>.png')
    def offline_tile(z,x,y):
        session(store,request.cookies.get('checkin_session'))
        if not 0<=z<=18 or not 0<=x<2**z or not 0<=y<2**z:
            raise APIError(404,'TILE_NOT_FOUND','瓦片不存在。')
        tile_root=Path(os.environ.get('CHECKIN_TILE_ROOT',store.root/'tiles')).resolve()
        tile=tile_root/str(z)/str(x)/(str(y)+'.png')
        if not tile.is_file(): raise APIError(404,'TILE_NOT_FOUND','瓦片不存在。')
        return send_file(tile,mimetype='image/png',max_age=7*86400)

    @app.post('/api/v2/auth/login')
    def auth_login():
        if request.content_length is None or request.content_length>4096 or not request.is_json:
            raise APIError(400,'INVALID_LOGIN','请使用 JSON 提交账号密码。')
        body=request.get_json()
        if not isinstance(body,dict) or not isinstance(body.get('username'),str) or not isinstance(body.get('password'),str) or len(body['username'])>80 or len(body['password'])>1024:
            raise APIError(400,'INVALID_LOGIN','账号密码格式无效。')
        token,user=login(store,body['username'],body['password'],request.remote_addr or 'unknown')
        response=jsonify(user)
        response.set_cookie('checkin_session',token,httponly=True,secure=os.environ.get('CHECKIN_SECURE_COOKIE')=='1',samesite='Strict',max_age=8*3600,path='/')
        return response

    @app.get('/api/v2/auth/me')
    def me(): return jsonify(username=g.user['username'],role=g.user['role'],csrf_token=g.user['csrf'])

    @app.post('/api/v2/auth/logout')
    def logout():
        with store.db() as db: db.execute('DELETE FROM sessions WHERE session_key_hash=?',(digest(request.cookies.get('checkin_session','')),))
        response=app.response_class(status=204);response.delete_cookie('checkin_session',path='/');return response

    def parse(extra=()):
        try: return params(request.query_string.decode('utf-8'),extra)
        except UnicodeError: raise APIError(400,'INVALID_PARAMETERS','参数编码无效。') from None

    @app.get('/api/v2/dataset/active')
    @app.get('/api/nodes')
    def active_data():
        batch_id=store.batch()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda base:node_request(base,'/count?'+urlencode({'batch_id':batch_id})),store.nodes))
        if any(row.get('batch_id')!=batch_id for row in results): raise APIError(503,'BATCH_MISMATCH','节点返回了错误批次。')
        nodes=[{'node':i,'count':row['count']} for i,row in enumerate(results)]
        with store.db() as db: batch=dict(db.execute('SELECT * FROM batches WHERE batch_id=?',(batch_id,)).fetchone())
        return jsonify(batch_id=batch_id,published_at=batch['published_at'],total_count=sum(row['count'] for row in nodes),nodes=nodes)

    @app.get('/api/v2/checkins')
    @app.get('/api/checkins')
    def checkins():
        p,limit,offset=parse();batch_id=store.batch(p.get('batch_id'));p['batch_id']=batch_id
        indexes=[shard_for(p['user_id'])] if 'user_id' in p else [0,1]
        query=urlencode({**p,'limit':limit,'offset':offset})
        def fetch(index):
            started=time.perf_counter();data=node_request(store.nodes[index],'/checkins?'+query)
            if data.get('batch_id')!=batch_id: raise APIError(503,'BATCH_MISMATCH','节点返回了错误批次。')
            return data,{'node':index,'elapsed_ms':round((time.perf_counter()-started)*1000,3),'candidates':len(data['items'])}
        with ThreadPoolExecutor(max_workers=len(indexes)) as pool: results=list(pool.map(fetch,indexes))
        rows=[{**row,'node':i} for i,(result,_) in zip(indexes,results) for row in result['items']]
        rows.sort(key=lambda row:(row['checkin_time'],row['id'],row['node']),reverse=True)
        return jsonify(batch_id=batch_id,items=rows[offset:offset+limit],queried_nodes=indexes,has_more=len(rows)>offset+limit and offset+limit<=10000,node_metrics=[metric for _,metric in results])

    @app.get('/api/v2/catalog')
    def catalog():
        p,limit,offset=parse(('group_by','q'))
        if set(p)-{'batch_id','group_by','q'}: raise APIError(400,'INVALID_PARAMETERS','目录只接受分类、前缀、批次和分页参数。')
        group=p.get('group_by','user')
        if group not in ('user','month','location') or (group=='month' and p.get('q')): raise APIError(400,'INVALID_CATALOG','分类维度或前缀无效。')
        batch_id=store.batch(p.get('batch_id'));prefix=p.get('q','')
        order='key DESC' if group=='month' else 'count DESC,key ASC'
        with store.db() as db:
            rows=db.execute(f'''SELECT key,count FROM catalog_counts WHERE batch_id=? AND group_by=? AND substr(key,1,?)=? ORDER BY {order} LIMIT ? OFFSET ?''',(batch_id,group,len(prefix),prefix,limit+1,offset)).fetchall()
        return jsonify(batch_id=batch_id,group_by=group,items=[dict(row) for row in rows[:limit]],has_more=len(rows)>limit and offset+limit<=10000)

    @app.post('/api/v2/imports')
    def imports():
        if not upload_slots.acquire(blocking=False): raise APIError(429,'UPLOAD_BUSY','同时上传数已达上限，请稍后重试。')
        path=store.root/'uploads'/(uuid.uuid4().hex+'.tsv')
        retained=False
        try:
            if not request.content_type or not request.content_type.startswith('multipart/form-data'): raise APIError(415,'UNSUPPORTED_FILE','需要 multipart/form-data 文件上传。')
            if (len(request.files.getlist('file'))!=1 or set(request.files)!={'file'}
                or set(request.form)-{'mode','field_mapping','coordinate_system'}
                or any(len(request.form.getlist(key))>1 for key in request.form)):
                raise APIError(400,'INVALID_UPLOAD','请提交一个 file 文件和一个 mode。')
            uploaded=request.files['file'];mode=request.form.get('mode','dry_run')
            if mode not in ('dry_run','replace'): raise APIError(400,'INVALID_MODE','mode 只能是 dry_run 或 replace。')
            filename=Path((uploaded.filename or '').replace('\\','/')).name[:200]
            options=import_config(filename,request.form.get('field_mapping'),request.form.get('coordinate_system','WGS84'))
            if shutil.disk_usage(store.root).free<(request.content_length or 0)*8+16*1024*1024: raise APIError(507,'INSUFFICIENT_SPACE','可用磁盘空间不足。')
            size=0;source_hash=hashlib.sha256()
            with path.open('xb') as output:
                while True:
                    chunk=uploaded.stream.read(1024*1024)
                    if not chunk: break
                    size+=len(chunk)
                    if size>maximum: raise APIError(413,'UPLOAD_TOO_LARGE','文件超过上传限制。')
                    source_hash.update(chunk);output.write(chunk)
            if size==0: raise APIError(400,'EMPTY_FILE','不能上传空文件。')
            job_id,created=enqueue(store,path,filename,source_hash.hexdigest(),mode,g.user['username'],options=options)
            retained=created
            return jsonify({**store.job(job_id),'reused':not created}),202 if created else 200
        finally:
            if not retained and path.exists(): path.unlink()
            upload_slots.release()

    @app.post('/api/v2/imports/preview')
    def import_preview():
        if (not request.content_length or request.content_length>132*1024
                or set(request.files)!={'file'} or len(request.files.getlist('file'))!=1):
            raise APIError(400,'INVALID_PREVIEW','预览仅接收一个不超过 128 KiB 的文件片段。')
        uploaded=request.files['file']
        options=import_config(uploaded.filename or '')
        content=uploaded.stream.read(128*1024+1)
        if len(content)>128*1024:
            raise APIError(413,'PREVIEW_TOO_LARGE','预览片段超过 128 KiB。')
        try:
            return jsonify(preview(content,options))
        except (ValueError,UnicodeError) as error:
            raise APIError(400,'INVALID_PREVIEW',str(error)) from None

    @app.get('/api/v2/imports')
    def recent_jobs():
        p,limit,offset=parse()
        if p: raise APIError(400,'INVALID_PARAMETERS','任务列表仅支持分页。')
        with store.db() as db: rows=db.execute('SELECT job_id FROM import_jobs ORDER BY created_at DESC LIMIT ? OFFSET ?',(limit+1,offset)).fetchall()
        return jsonify(items=[store.job(row['job_id']) for row in rows[:limit]],has_more=len(rows)>limit)

    @app.get('/api/v2/imports/<job_id>')
    def job(job_id): return jsonify(store.job(job_id))

    @app.post('/api/v2/imports/<job_id>/retry')
    def retry_job(job_id):
        job_id,created=retry(store,job_id,g.user['username']);return jsonify(store.job(job_id)),202 if created else 200

    @app.get('/api/v2/batches')
    def batches():
        p,limit,offset=parse()
        if p: raise APIError(400,'INVALID_PARAMETERS','批次列表仅支持分页。')
        with store.db() as db:
            active=store.active(db)
            rows=db.execute('SELECT b.*,j.filename FROM batches b JOIN import_jobs j USING(job_id) ORDER BY b.created_at DESC LIMIT ? OFFSET ?',(limit+1,offset)).fetchall()
            items=[]
            for row in rows[:limit]:
                item=dict(row);item['is_active']=row['batch_id']==active
                item['nodes']=[dict(shard) for shard in db.execute('SELECT node_id,row_count,ready FROM batch_shards WHERE batch_id=? ORDER BY node_id',(row['batch_id'],))]
                items.append(item)
            audit=[dict(row) for row in db.execute('SELECT * FROM audit_log ORDER BY id DESC LIMIT 20')]
        return jsonify(items=items,has_more=len(rows)>limit,active_batch_id=active,audit=audit)

    @app.post('/api/v2/batches/<batch_id>/activate')
    def activate_batch(batch_id):
        body=request.get_json()
        if not isinstance(body,dict) or 'expected_batch_id' not in body: raise APIError(400,'EXPECTED_BATCH_REQUIRED','请传入确认时的 expected_batch_id。')
        return jsonify(activate(store,batch_id,body['expected_batch_id'],g.user['username']))

    from backend.data_services import register_data_services
    register_data_services(app,store)
    from backend.export_jobs import register_export_jobs
    register_export_jobs(app,store)
    return app


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--host',default='127.0.0.1');parser.add_argument('--port',type=int,default=8000)
    parser.add_argument('--no-worker',action='store_true',help='Run backend.worker separately')
    args=parser.parse_args();store=Store();process=None
    if not args.no_worker:
        process=subprocess.Popen([sys.executable,'-m','backend.worker'],cwd=ROOT,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        def cleanup():
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=10)
                except subprocess.TimeoutExpired: process.kill()
        atexit.register(cleanup)
    create_app(store).run(host=args.host,port=args.port,threaded=True,use_reloader=False,debug=False)

if __name__=='__main__': main()
