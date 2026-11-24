"""Run real HTTP imports and the design's 30-repeat benchmarks on the same server.
Credentials are read from a local ignored file; neither passwords nor cookies are logged.
"""
import argparse
import http.client
import json
import math
import platform
import sqlite3
import statistics
import sys
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import urlencode, urlsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

class Client:
    def __init__(self,base,accounts):
        self.url=urlsplit(base);self.cookie='';self.csrf=''
        credentials=json.loads(Path(accounts).read_text(encoding='utf-8'))
        body=json.dumps({'username':'admin','password':credentials['admin']}).encode()
        data,headers=self.request('POST','/api/v2/auth/login',body,{'Content-Type':'application/json'})
        self.cookie=headers['Set-Cookie'].split(';')[0];self.csrf=data['csrf_token']
    def connect(self):
        cls=http.client.HTTPSConnection if self.url.scheme=='https' else http.client.HTTPConnection
        return cls(self.url.hostname,self.url.port or (443 if self.url.scheme=='https' else 80),timeout=120)
    def request(self,method,path,body=None,headers=None):
        conn=self.connect()
        conn.request(method,path,body,{'Cookie':self.cookie,'X-CSRF-Token':self.csrf,**(headers or {})})
        response=conn.getresponse();data=json.load(response) if response.status!=204 else None;response_headers=dict(response.getheaders());status=response.status;conn.close()
        if status>=400: raise RuntimeError(f'{status}: {data}')
        return data,response_headers
    def get(self,path): return self.request('GET',path)[0]
    def upload(self,path,mode='replace'):
        path=Path(path);boundary='checkin-local-benchmark-9b2b8b7a'
        before=(f'--{boundary}\r\nContent-Disposition: form-data; name="mode"\r\n\r\n{mode}\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\nContent-Type: text/plain\r\n\r\n').encode()
        after=f'\r\n--{boundary}--\r\n'.encode();conn=self.connect()
        conn.putrequest('POST','/api/v2/imports');conn.putheader('Cookie',self.cookie);conn.putheader('X-CSRF-Token',self.csrf);conn.putheader('Content-Type','multipart/form-data; boundary='+boundary);conn.putheader('Content-Length',str(len(before)+path.stat().st_size+len(after)));conn.endheaders();conn.send(before)
        with path.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''): conn.send(chunk)
        conn.send(after);response=conn.getresponse();data=json.load(response);status=response.status;conn.close()
        if status>=400: raise RuntimeError(f'{status}: {data}')
        return data


def oracle(root,batch,kind,first,offset):
    results=[]
    def spherical(lon,lat):
        # Independent all-row oracle, without using the production prefilter.
        lon1,lat1=map(math.radians,(float(first[3]),float(first[2])))
        lon2,lat2=map(math.radians,(lon,lat))
        a=math.sin((lat2-lat1)/2)**2+math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
        return 2*6371008.8*math.atan2(math.sqrt(max(0,a)),math.sqrt(max(0,1-a)))
    for i in range(2):
        with closing(sqlite3.connect((root/'shards'/f'node-{i}'/(batch+'.sqlite')).as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row;db.create_function('reference_distance',2,spherical)
            where,values='1=1',[]
            if kind=='single_user': where='user_id=?';values=[first[0]]
            if kind=='city_rectangle': where='longitude BETWEEN ? AND ? AND latitude BETWEEN ? AND ?';values=[float(first[3])-.1,float(first[3])+.1,float(first[2])-.1,float(first[2])+.1]
            if kind=='radius_1km': where='reference_distance(longitude,latitude)<=1000.0000001'
            rows=db.execute(f'SELECT * FROM checkins WHERE {where} ORDER BY checkin_time DESC,id DESC LIMIT ?',(*values,offset+51))
            results.extend({**dict(row),'node':i} for row in rows)
    results.sort(key=lambda row:(row['checkin_time'],row['id'],row['node']),reverse=True)
    return results[offset:offset+50]


def benchmark(client,root,job,first,repeats):
    batch=job['batch_id'];cases={
      'single_user':{'user_id':first[0]},
      'city_rectangle':{'west':float(first[3])-.1,'east':float(first[3])+.1,'south':float(first[2])-.1,'north':float(first[2])+.1},
      'radius_1km':{'center_lon':first[3],'center_lat':first[2],'radius_m':1000},
      'wide_shallow':{},'wide_deep':{'offset':9900}}
    reports={}
    for name,params in cases.items():
        path='/api/v2/checkins?'+urlencode({**params,'batch_id':batch,'limit':50})
        start=time.perf_counter();result=client.get(path);first_ms=(time.perf_counter()-start)*1000
        expected=oracle(root,batch,name,first,int(params.get('offset',0)))
        assert result['items']==expected,(name,'SQL oracle mismatch')
        for _ in range(2): client.get(path)
        durations=[];metrics=[]
        for _ in range(repeats):
            start=time.perf_counter();data=client.get(path);durations.append((time.perf_counter()-start)*1000);metrics.append(data['node_metrics'])
            assert data['batch_id']==batch
        sorted_times=sorted(durations)
        reports[name]={'query':params,'rows':len(result['items']),'oracle':'passed','first_request_ms':round(first_ms,3),'median_ms':round(statistics.median(durations),3),'p95_ms':round(sorted_times[math.ceil(.95*len(sorted_times))-1],3),'repeats':repeats,'node_samples':metrics}
        print(name, 'median',reports[name]['median_ms'],'P95',reports[name]['p95_ms'],flush=True)
    return reports


def main():
    parser=argparse.ArgumentParser();parser.add_argument('source',type=Path);parser.add_argument('--base',default='http://127.0.0.1:8002');parser.add_argument('--root',type=Path,default=ROOT/'data');parser.add_argument('--repeats',type=int,default=30);parser.add_argument('--sizes',default='10000,1000000,full');args=parser.parse_args()
    root=args.root.resolve();output=root/'full-verification';output.mkdir(parents=True,exist_ok=True)
    client=Client(args.base,root/'local-accounts.json')
    with args.source.open(encoding='utf-8-sig') as stream: first=stream.readline().rstrip('\n').split('\t')
    summary={'machine':platform.platform(),'python':platform.python_version(),'runs':[]}
    for size in args.sizes.split(','):
        if size=='full': path=args.source
        else:
            path=output/(size+'.tsv')
            with args.source.open('rb') as stream,path.open('wb') as target:
                for _ in range(int(size)):
                    line=stream.readline()
                    if not line: break
                    target.write(line)
        started=time.perf_counter();job=client.upload(path);last=None
        while job['status'] not in ('succeeded','failed'):
            if (job['status'],job.get('scanned_rows'))!=last:
                print(size,job['status'],job.get('scanned_rows',0),flush=True);last=(job['status'],job.get('scanned_rows'))
            time.sleep(2);job=client.get('/api/v2/imports/'+job['job_id'])
        if job['status']!='succeeded': raise RuntimeError(job)
        active=client.get('/api/v2/dataset/active')
        if active['batch_id']!=job['batch_id']:
            client.request('POST','/api/v2/batches/'+job['batch_id']+'/activate',json.dumps({'expected_batch_id':active['batch_id']}).encode(),{'Content-Type':'application/json'})
        assert job['scanned_rows']==job['valid_rows']+job['rejected_rows']
        assert job['valid_rows']==job['duplicates']+sum(job['node_counts'])
        record={'size':size,'end_to_end_seconds':round(time.perf_counter()-started,3),'job':job,'queries':benchmark(client,root,job,first,args.repeats)}
        with closing(sqlite3.connect((root/'shards'/'node-0'/(job['batch_id']+'.sqlite')).as_uri()+'?mode=ro',uri=True)) as db: record['indexes']=[row[1] for row in db.execute('PRAGMA index_list(checkins)')]
        summary['runs'].append(record);(output/'benchmark.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
        print('COMPLETED',size,job['node_counts'],flush=True)
    client.request('POST','/api/v2/auth/logout')

if __name__=='__main__': main()
