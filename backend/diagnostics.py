"""Read-only local startup and service checks. Never initialize or migrate data."""
import importlib.util
import json
import os
import re
import socket
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen


def local_checks(root, ports=(), shards=None):
    root=Path(root).resolve()
    results=[]
    def add(name,ok,message): results.append({'check':name,'ok':bool(ok),'message':message})
    add('python',sys.version_info>=(3,11),'Python 3.11+ required')
    for name in ('flask','argon2','filelock'):
        add('dependency:'+name,importlib.util.find_spec(name) is not None,'Available' if importlib.util.find_spec(name) else 'Run: python -m pip install -r requirements.txt')
    for port in ports:
        if not isinstance(port,int) or not 1<=port<=65535: add('port:'+str(port),False,'Port must be between 1 and 65535');continue
        try:
            with socket.socket() as sock:
                if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):sock.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
                sock.bind(('127.0.0.1',port))
            add('port:'+str(port),True,'Available')
        except OSError: add('port:'+str(port),False,'Already occupied; visit the running service or choose different ports')
    control=root/'control.sqlite';active=None;counts={}
    try:
        with closing(sqlite3.connect(control.as_uri()+'?mode=ro',uri=True)) as db:
            enabled=db.execute('SELECT count(*) FROM users WHERE enabled=1').fetchone()[0]
            total=db.execute('SELECT count(*) FROM users').fetchone()[0]
            add('accounts',enabled>0,'Enabled accounts exist' if enabled else 'Enable an account: python -m backend.manage enable-user <username>' if total else 'Run: python -m backend.manage init')
            row=db.execute("SELECT active_batch_id FROM dataset_state WHERE dataset_id='gowalla'").fetchone();active=row[0] if row else None
            if active:
                entries=db.execute('SELECT node_id,storage_key,row_count FROM batch_shards WHERE batch_id=?',(active,)).fetchall()
                add('manifest',len(entries)==2 and {r[0] for r in entries}=={0,1},'Two published shard entries required')
                roots=shards or [Path(os.environ.get(f'CHECKIN_SHARD_{i}',root/'shards'/f'node-{i}')) for i in range(2)]
                for index,key,count in entries:
                    valid=index in (0,1) and bool(re.fullmatch(r'B-[a-f0-9]{24}\.sqlite',key))
                    exists=valid and (Path(roots[index])/key).is_file()
                    add('snapshot:'+str(index),exists,'Present' if exists else 'Missing snapshot; restore its file or correct CHECKIN_SHARD settings')
                    counts[index]=count
            else:add('dataset',True,'No active dataset yet; import a file after login')
    except sqlite3.Error:
        add('control_database',False,'Control database unavailable or incompatible. First setup: python -m backend.manage init. Existing data: check CHECKIN_DATA; do not overwrite it.')
    return {'ok':all(r['ok'] for r in results),'checks':results,'active_batch_id':active,'expected_counts':counts}


def health(url,service,timeout=2):
    with urlopen(url.rstrip('/')+'/healthz',timeout=timeout) as response: data=json.load(response)
    if not isinstance(data,dict) or data.get('status')!='ready' or data.get('service')!=service:raise ValueError('Unexpected service identity')
    return data


def service_checks(local,base,nodes):
    checks=[]
    for index,(url,identity) in enumerate([(base,'checkin-web'),*[(url,'checkin-node') for url in nodes]]):
        try:health(url,identity);checks.append({'check':'service:'+identity+':'+str(index),'ok':True,'message':'Ready'})
        except (OSError,ValueError):checks.append({'check':'service:'+identity+':'+str(index),'ok':False,'message':'Not ready; check ports, process logs and configured addresses'})
    batch=local['active_batch_id']
    if batch:
        for index,url in enumerate(nodes):
            try:
                with urlopen(url+'/count?'+urlencode({'batch_id':batch}),timeout=3) as response:data=json.load(response)
                ok=isinstance(data,dict) and data.get('batch_id')==batch and data.get('count')==local['expected_counts'].get(index)
                checks.append({'check':'published_data:'+str(index),'ok':ok,'message':'Batch and count match' if ok else 'Batch/count mismatch'})
            except (OSError,ValueError):checks.append({'check':'published_data:'+str(index),'ok':False,'message':'Published batch unavailable at node'})
    return checks
