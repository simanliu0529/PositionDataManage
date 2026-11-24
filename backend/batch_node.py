"""Read-only HTTP node. Every process sees only its own snapshot directory."""
import argparse
import json
import re
import sqlite3
import time
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from backend.http_io import Handler
from backend.control import APIError
from backend.spatial import params, where_clause, distance

QUERY_SECONDS = 6
PREFIX_ROWS = 8192
MAX_PREFIX_ROWS = 32768


def read_checkins(db, filters, maximum):
    """Read an exact ordered result, with a shared budget for both plans.

    A wide latitude index range can fetch millions of table rows before sorting
    for a tiny LIMIT. First inspect a bounded prefix in the existing time order.
    Enough matches in that prefix are necessarily the newest matching rows. If
    there are fewer, run the complete filtered query; never return a sample as a
    complete answer. No schema, index or snapshot contents are changed.
    """
    deadline = time.monotonic() + QUERY_SECONDS
    db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 10000)
    where, values = where_clause(filters)
    try:
        wide = ('west' in filters and filters['north'] - filters['south'] >= 10
                and 'user_id' not in filters and 'location_id' not in filters)
        if wide:
            time_filters = {key: filters[key] for key in ('start', 'end') if key in filters}
            time_where, time_values = where_clause(time_filters)
            prefix_size = min(MAX_PREFIX_ROWS, max(PREFIX_ROWS, maximum * 8))
            rows = db.execute(
                f'''SELECT * FROM (
                    SELECT * FROM checkins WHERE {time_where}
                    ORDER BY checkin_time DESC,id DESC LIMIT ?
                ) AS recent WHERE {where}
                ORDER BY checkin_time DESC,id DESC LIMIT ?''',
                (*time_values, prefix_size, *values, maximum)).fetchall()
            if len(rows) >= maximum:
                return rows, 'time_prefix'
        rows = db.execute(
            f'SELECT * FROM checkins WHERE {where} ORDER BY checkin_time DESC,id DESC LIMIT ?',
            (*values, maximum)).fetchall()
        return rows, 'filtered'
    except sqlite3.OperationalError as error:
        if getattr(error, 'sqlite_errorcode', None) == sqlite3.SQLITE_INTERRUPT:
            raise APIError(504, 'QUERY_TIMEOUT', '当前范围查询耗时较长，请缩小空间或时间范围后重试。') from error
        raise APIError(503, 'NODE_QUERY_FAILED', '节点查询执行失败，请检查服务日志。') from error
    finally:
        db.set_progress_handler(None, 0)


class BatchNodeHandler(Handler):
    def get(self):
        url = urlsplit(self.path)
        try:
            if url.path=='/healthz': return self.json(200,{'status':'ready','service':'checkin-node'})
            if url.path not in ('/count','/checkins','/export','/export-chunk'): raise APIError(404,'NOT_FOUND','路径不存在')
            p, limit, offset = params(url.query, ('after',) if url.path=='/export-chunk' else ())
            after=p.pop('after',str(-(2**63)))
            if url.path=='/export-chunk':
                try:
                    after=int(after)
                    if not -(2**63)<=after<2**63: raise ValueError
                except ValueError: raise APIError(400,'INVALID_CURSOR','导出游标无效') from None
            batch = p.pop('batch_id', '')
            if not re.fullmatch(r'B-[a-f0-9]{24}',batch): raise APIError(400,'INVALID_BATCH','批次编号无效')
            path = self.server.root / (batch+'.sqlite')
            if not path.is_file(): raise APIError(409,'BATCH_UNAVAILABLE','节点未就绪')
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
                db.row_factory=sqlite3.Row
                if url.path=='/count': return self.json(200,{'batch_id':batch,'count':db.execute('SELECT count(*) FROM checkins').fetchone()[0]})
                db.create_function('haversine',4,distance,deterministic=True)
                if url.path=='/export-chunk':
                    # Walk primary-key windows, including windows with no matches.
                    # No OFFSET growth, full-result sort, schema change or sampling.
                    deadline=time.monotonic()+QUERY_SECONDS
                    db.set_progress_handler(lambda:int(time.monotonic()>=deadline),10000)
                    try:
                        # Equality filters can use existing team indexes to avoid
                        # walking a whole shard for one user's handful of rows.
                        keys=[key for key in ('user_id','location_id') if key in p]
                        narrow=''.join(' AND '+key+'=?' for key in keys)
                        ids=db.execute('SELECT id FROM checkins WHERE id>?'+narrow+' ORDER BY id LIMIT 20000',(after,*(p[key] for key in keys))).fetchall()
                        end=ids[-1][0] if ids else after
                        where,values=where_clause(p)
                        rows=db.execute(f'SELECT * FROM checkins NOT INDEXED WHERE id>? AND id<=? AND ({where}) ORDER BY id',(after,end,*values)).fetchall() if ids else []
                        return self.json(200,{'batch_id':batch,'items':[dict(row) for row in rows], 'next_after':end,'scanned':len(ids),'done':len(ids)<20000})
                    except sqlite3.OperationalError as error:
                        raise APIError(504 if getattr(error,'sqlite_errorcode',None)==sqlite3.SQLITE_INTERRUPT else 503,'EXPORT_READ_FAILED','导出分块读取失败，未生成完整文件。') from error
                    finally: db.set_progress_handler(None,0)
                # One bounded internal read avoids repeated growing-prefix
                # requests when assembling an all-matching export at the API.
                maximum=10001 if url.path=='/export' else offset+limit+1
                rows,strategy=read_checkins(db,p,maximum)
                self.json(200,{'batch_id':batch,'items':[dict(row) for row in rows],'query_strategy':strategy})
        except APIError as error:
            self.json(error.status,{'code':error.code,'message':error.message})

def serve(root,port,host='127.0.0.1'):
    root=Path(root).resolve();root.mkdir(parents=True,exist_ok=True)
    server=ThreadingHTTPServer((host,port),BatchNodeHandler);server.root=root
    server.serve_forever()

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('directory',type=Path);parser.add_argument('port',type=int);parser.add_argument('--host',default='127.0.0.1')
    args=parser.parse_args();serve(args.directory,args.port,args.host)
