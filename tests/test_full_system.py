import io
import json
import math
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from backend.control import Store
from backend.security import create_user
from backend.server import create_app
from backend.worker import process_job, run_worker
from backend.spatial import distance, EARTH_RADIUS
from backend.batch_node import BatchNodeHandler

SAMPLE=Path('sample/checkins.tsv').read_bytes()

class QuietNode(BatchNodeHandler):
    def log_message(self,*_): pass

class FullSystem(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.store=Store(self.root)
        self.servers=[]
        for root in self.store.shards:
            server=ThreadingHTTPServer(('127.0.0.1',0),QuietNode);server.root=root
            threading.Thread(target=server.serve_forever,daemon=True).start();self.servers.append(server)
        self.store.nodes=[f'http://127.0.0.1:{s.server_port}' for s in self.servers]
        create_user(self.store,'admin','test-admin-unique-123','admin');create_user(self.store,'viewer','test-viewer-unique-123','viewer')
        self.app=create_app(self.store);self.client=self.app.test_client()
        result=self.client.post('/api/v2/auth/login',json={'username':'admin','password':'test-admin-unique-123'})
        self.assertEqual(result.status_code,200);self.csrf=result.json['csrf_token'];self.headers={'X-CSRF-Token':self.csrf}

    def tearDown(self):
        for server in self.servers: server.shutdown();server.server_close()
        self.temp.cleanup()

    def upload(self,data=SAMPLE,mode='replace',filename='fixture.tsv'):
        response=self.client.post('/api/v2/imports',data={'file':(io.BytesIO(data),filename),'mode':mode},headers=self.headers)
        response.request.environ['wsgi.input'].close()
        return response

    def publish(self,data=SAMPLE):
        response=self.upload(data);self.assertIn(response.status_code,(200,202),response.json)
        if response.status_code==202: process_job(self.store,response.json['job_id'])
        job=self.store.job(response.json['job_id']);self.assertEqual(job['status'],'succeeded',job)
        return job

    def test_auth_csrf_roles_and_logout_all_paths(self):
        anonymous=self.app.test_client()
        for path in ['/api/checkins','/api/nodes','/api/v2/checkins','/api/v2/catalog','/api/v2/imports']:
            self.assertEqual(anonymous.get(path).status_code,401)
        self.assertEqual(self.client.post('/api/v2/imports').status_code,403)
        self.assertEqual(self.client.post('/api/v2/auth/logout',headers={**self.headers,'Origin':'https://evil.invalid'}).status_code,403)
        viewer=self.app.test_client();csrf=viewer.post('/api/v2/auth/login',json={'username':'viewer','password':'test-viewer-unique-123'}).json['csrf_token']
        self.assertEqual(viewer.get('/api/v2/batches').status_code,403)
        self.assertEqual(viewer.post('/api/v2/imports',headers={'X-CSRF-Token':csrf}).status_code,403)
        self.assertEqual(self.client.post('/api/v2/auth/logout',headers=self.headers).status_code,204)
        self.assertEqual(self.client.get('/api/checkins').status_code,401)
        self.assertEqual(self.client.get('/api/v2/checkins').status_code,401)

    def test_disabled_account_revokes_sessions(self):
        with self.store.db() as db: db.execute("UPDATE users SET enabled=0 WHERE username='admin'")
        self.assertEqual(self.client.get('/api/v2/auth/me').status_code,401)

    def test_login_throttle_and_cookie(self):
        client=self.app.test_client()
        for _ in range(8):
            self.assertEqual(client.post('/api/v2/auth/login',json={'username':'unknown','password':'wrong'}).status_code,401)
        self.assertEqual(client.post('/api/v2/auth/login',json={'username':'unknown','password':'wrong'}).status_code,429)
        row=self.client.get('/api/v2/auth/me');self.assertEqual(row.json['role'],'admin')
        with self.store.db() as db:
            self.assertTrue(db.execute('SELECT password_hash FROM users').fetchone()[0].startswith('$argon2id$'))
            self.assertEqual(len(db.execute('SELECT session_key_hash FROM sessions').fetchone()[0]),64)

    def test_dry_run_dedupe_and_reports(self):
        response=self.upload(mode='dry_run');self.assertEqual(response.status_code,202)
        self.assertFalse(response.json['reused'])
        process_job(self.store,response.json['job_id']);job=self.store.job(response.json['job_id'])
        self.assertEqual(job['status'],'succeeded',job);self.assertIsNone(job['batch_id']);self.assertIsNone(self.store.active())
        self.assertEqual((job['scanned_rows'],job['valid_rows'],job['rejected_rows'],job['duplicates'],job['node_counts']),(6,5,1,1,[2,2]))
        self.assertEqual(job['reasons']['invalid_coordinates']['sample_lines'],[6])
        self.assertEqual(job['database_import']['valid'],4)
        self.assertEqual(job['database_import']['inserted'],4)
        self.assertEqual(len(job['database_import']['source_sha256']),64)
        self.assertNotIn('source',job['database_import'])
        reused=self.upload(mode='dry_run');self.assertEqual(reused.status_code,200);self.assertTrue(reused.json['reused'])
        self.assertEqual(self.client.get('/api/v2/dataset/active').json['code'],'NO_ACTIVE_BATCH')
        self.assertFalse(list(self.store.shards[0].glob('*.sqlite')))
        self.assertTrue(self.publish()['batch_id'])

    def test_publish_query_catalog_and_time(self):
        job=self.publish();batch=job['batch_id']
        active=self.client.get('/api/v2/dataset/active').json;self.assertEqual(active['total_count'],4)
        user=self.client.get('/api/v2/checkins?user_id=1').json;self.assertEqual(user['queried_nodes'],[1]);self.assertEqual(len(user['items']),1)
        time=self.client.get('/api/v2/checkins?start=2010-08-02T09:15:00Z&end=2010-08-03T11:30:00Z').json;self.assertEqual(time['items'][0]['user_id'],'2');self.assertEqual(len(time['items']),1)
        location=self.client.get('/api/v2/checkins?location_id=101').json;self.assertEqual(len(location['items']),1)
        catalog=self.client.get('/api/v2/catalog?group_by=month').json;self.assertEqual(catalog['items'],[{'key':'2010-08','count':4}]);self.assertEqual(catalog['batch_id'],batch)
        catalog=self.client.get('/api/v2/catalog?group_by=user&q=1').json;self.assertEqual(catalog['items'],[{'key':'1','count':1}])
        self.assertEqual(len(self.client.get('/api/checkins').json['items']),4)

    def test_rectangle_boundaries_combination_and_pagination(self):
        self.publish()
        query='/api/v2/checkins?west=114.36&east=115&south=30.54&north=31'
        result=self.client.get(query).json
        self.assertEqual([row['user_id'] for row in result['items']],['1'])
        combined=self.client.get(query+'&user_id=1&location_id=101&start=2010-08-01T12:00:00Z&end=2010-08-02T00:00:00Z').json
        self.assertEqual(len(combined['items']),1)
        self.assertEqual(self.client.get(query+'&user_id=2').json['items'],[])
        first=self.client.get('/api/v2/checkins?limit=2').json
        second=self.client.get('/api/v2/checkins?limit=2&offset=2&batch_id='+first['batch_id']).json
        self.assertTrue(first['has_more']);self.assertFalse(second['has_more'])
        self.assertEqual([row['user_id'] for row in first['items']+second['items']],['4','3','2','1'])

    def test_radius_before_pagination_poles_dateline_and_boundary(self):
        # Include exact spherical-radius boundary and just outside, dateline and pole cases.
        angular=math.degrees(1000/EARTH_RADIUS)
        lines=[f'1\t2010-08-01T00:00:00Z\t0\t{angular:.15f}\tinside',f'2\t2010-08-02T00:00:00Z\t0\t{angular+1e-5:.15f}\toutside', '3\t2010-08-03T00:00:00Z\t0\t179.999\tdate','4\t2010-08-04T00:00:00Z\t0\t-179.999\tdate','5\t2010-08-05T00:00:00Z\t89.9999\t90\tpole']
        self.publish(('\n'.join(lines)+'\n').encode())
        result=self.client.get('/api/v2/checkins?center_lon=0&center_lat=0&radius_m=1000&limit=1').json
        self.assertEqual([row['location_id'] for row in result['items']],['inside']);self.assertFalse(result['has_more'])
        dateline=self.client.get('/api/v2/checkins?center_lon=180&center_lat=0&radius_m=1000').json;self.assertEqual(len(dateline['items']),2)
        pole=self.client.get('/api/v2/checkins?center_lon=0&center_lat=90&radius_m=1000').json;self.assertEqual(len(pole['items']),1)

    def test_fixed_batch_pagination_rollback_and_conflict(self):
        old=self.publish()['batch_id'];first=self.client.get('/api/v2/checkins?limit=2').json
        new=self.publish(b'7\t2011-01-01T00:00:00Z\t30\t114\t999\n')['batch_id']
        reused=self.upload();self.assertEqual(reused.status_code,200);self.assertTrue(reused.json['reused'])
        self.assertEqual(reused.json['batch_id'],old);self.assertEqual(self.store.active(),new)
        second=self.client.get('/api/v2/checkins?limit=2&offset=2&batch_id='+old).json
        self.assertEqual(len(first['items']+second['items']),4);self.assertEqual(second['batch_id'],old)
        response=self.client.post('/api/v2/batches/'+old+'/activate',json={'expected_batch_id':old},headers=self.headers);self.assertEqual(response.status_code,409)
        response=self.client.post('/api/v2/batches/'+old+'/activate',json={'expected_batch_id':new},headers=self.headers);self.assertEqual(response.status_code,200)
        self.assertEqual(self.store.active(),old);self.assertEqual(self.client.get('/api/v2/dataset/active').json['total_count'],4)
        self.assertEqual(self.client.get('/api/v2/checkins?batch_id=B-'+'0'*24).status_code,409)
        self.assertEqual(len(self.client.get('/api/v2/batches').json['audit']),3)

    def test_node_failure_prevents_publish_and_retry(self):
        old=self.publish()['batch_id'];original=self.store.nodes
        response=self.upload(b'2\t2012-01-01T00:00:00Z\t1\t1\tnew\n');job=response.json['job_id']
        self.store.nodes=['http://127.0.0.1:1',original[1]];process_job(self.store,job)
        self.assertEqual(self.store.job(job)['status'],'failed');self.assertEqual(self.store.active(),old)
        self.store.nodes=original
        response=self.client.post('/api/v2/imports/'+job+'/retry',headers=self.headers);self.assertEqual(response.status_code,202)
        process_job(self.store,response.json['job_id']);self.assertEqual(self.store.job(response.json['job_id'])['status'],'succeeded')
        self.assertNotEqual(self.store.active(),old)
        self.assertEqual(self.client.post('/api/v2/imports/'+response.json['job_id']+'/retry',headers=self.headers).status_code,409)

    def test_invalid_files_filters_and_hidden_sources(self):
        for data in [b'bad columns\n',b'\xff\xfe\x00']:
            response=self.upload(data);process_job(self.store,response.json['job_id']);self.assertEqual(self.store.job(response.json['job_id'])['status'],'failed')
        self.assertEqual(self.upload(b'').status_code,400)
        self.assertEqual(self.upload(filename='bad.exe').status_code,415)
        for query in ['west=0','center_lat=0&center_lon=0','center_lon=0&center_lat=0&radius_m=50001','west=0&east=1&south=0&north=1&center_lon=0&center_lat=0&radius_m=1','limit=201','offset=10001','user_id=1&user_id=2']:
            response=self.client.get('/api/v2/checkins?'+query);self.assertEqual(response.status_code,400,query);self.assertIn('request_id',response.json)
        self.assertEqual(self.client.get('/uploads/test.tsv').status_code,404)
        self.assertNotIn('source_path',self.client.get('/api/v2/imports').json['items'][0])
        with self.client.get('/system.js') as response: self.assertEqual(response.status_code,200)

    def test_large_multipart_size_limit_and_literal_prefix(self):
        payload=b'1\t2010-01-01T00:00:00Z\t30\t114\t1\n'*20000
        response=self.upload(payload,mode='dry_run')
        self.assertEqual(response.status_code,202,response.json)
        with patch.dict('os.environ',{'CHECKIN_MAX_UPLOAD_BYTES':'100'}):
            limited=create_app(self.store).test_client()
            csrf=limited.post('/api/v2/auth/login',json={'username':'admin','password':'test-admin-unique-123'}).json['csrf_token']
            response=limited.post('/api/v2/imports',data={'file':(io.BytesIO(b'x'*101),'large.tsv')},headers={'X-CSRF-Token':csrf})
            self.assertEqual(response.status_code,413)
        self.publish(b'%\t2010-01-01T00:00:00Z\t30\t114\t1\n1\t2010-01-01T00:00:00Z\t30\t114\t1\n')
        result=self.client.get('/api/v2/catalog?group_by=user&q=%25').json
        self.assertEqual(result['items'],[{'key':'%','count':1}])

    def test_offline_map_config_and_tiles_require_login(self):
        root=self.root/'tiles'/'0'/'0';root.mkdir(parents=True)
        (root/'0.png').write_bytes(b'fake-tile-for-transport-test')
        self.assertEqual(self.app.test_client().get('/tiles/0/0/0.png').status_code,401)
        with self.client.get('/tiles/0/0/0.png') as response: self.assertEqual(response.status_code,200)
        self.assertEqual(self.client.get('/tiles/0/2/0.png').status_code,404)
        self.assertIn('tile_url',self.client.get('/api/v2/config').json['map'])

    def test_unpublished_snapshots_are_not_queryable(self):
        import time
        batch='B-'+'a'*24
        with self.store.db() as db:
            db.execute("INSERT INTO batches VALUES (?,?,'ready',1,0,0,NULL,?)",(batch,'not-published',time.time()))
        result=self.client.get('/api/v2/checkins?batch_id='+batch)
        self.assertEqual(result.status_code,409)
        self.assertEqual(result.json['code'],'BATCH_UNAVAILABLE')

    def test_web_uses_complete_upstream_cleaner(self):
        rows=[
            'u1\t2010-01-01T00:00:00Z\t30\t114\tA',
            'u1\t2010-01-01T00:00:00Z\t30\t114\tA',
            'u1\t2010-01-01T00:00:01Z\t40\t116\tB',
            'u1\t2010-01-01T00:00:00Z\t30\t115\tC',
            'u2\t2010-01-02T00:00:00Z\t0\t0\tZERO',
            'a\t2010-01-02T00:00:00Z\t31\t115\tP',
            'b\t2010-01-02T00:00:00Z\t30.12345678\t114.12345678\tP',
            'c\t2010-01-02T00:00:00Z\t30.12345678\t114.12345678\tP',
            ''
        ]
        job=self.publish(('\n'.join(rows)+'\n').encode())
        stats=job['cleaning_stats']
        self.assertEqual(stats['null_island'],1)
        self.assertEqual(stats['duplicate'],1)
        self.assertEqual(stats['speed_anomaly'],1)
        self.assertEqual(stats['conflict_same_time'],1)
        # Assert the unchanged upstream report honestly. It counts all three
        # rows at a repaired POI, whereas only one coordinate actually changes;
        # test_cleaner_regression retains that defect as an expected failure.
        self.assertEqual(stats['coord_repair'],3)
        self.assertEqual(stats['blank_line'],1)
        self.assertEqual(job['scanned_rows'],9)
        self.assertEqual(job['valid_rows'],job['duplicates']+sum(job['node_counts']))
        points=self.client.get('/api/v2/checkins?location_id=P').json['items']
        self.assertEqual(len(points),3)
        self.assertTrue(all(row['latitude']==30.123457 and row['longitude']==114.123457 for row in points))
        self.assertEqual(sum(row['outcome']=='coordinate_repaired' for row in job['quality_samples']),1)
        self.assertFalse(job['reasons']['speed_anomaly']['line_samples_available'])
        self.assertEqual(job['reasons']['null_island']['sample_lines'],[5])
        self.assertTrue(job['rules_version'].startswith('gowalla-upstream-238168f-db-1e1c602-web-v3:'))

    def test_new_rules_do_not_reuse_legacy_jobs(self):
        response=self.upload(mode='dry_run');old_id=response.json['job_id']
        with self.store.db() as db:
            db.execute("UPDATE import_jobs SET rules_version='gowalla-normalize-v1',status='succeeded' WHERE job_id=?",(old_id,))
        new=self.upload(mode='dry_run')
        self.assertEqual(new.status_code,202)
        self.assertNotEqual(new.json['job_id'],old_id)

    def test_worker_restart_recovers_and_processes_queue(self):
        response=self.upload(mode='dry_run');interrupted=response.json['job_id']
        with self.store.db() as db: db.execute("UPDATE import_jobs SET status='loading',heartbeat_at=0 WHERE job_id=?",(interrupted,))
        stop=threading.Event();stop.set();run_worker(self.store,stop)
        self.assertEqual(self.store.job(interrupted)['status'],'failed')
        retry=self.client.post('/api/v2/imports/'+interrupted+'/retry',headers=self.headers).json['job_id']
        stop=threading.Event();thread=threading.Thread(target=run_worker,args=(self.store,stop),daemon=True);thread.start()
        import time
        for _ in range(100):
            if self.store.job(retry)['status'] in ('succeeded','failed'):break
            time.sleep(.05)
        stop.set();thread.join(timeout=5)
        self.assertEqual(self.store.job(retry)['status'],'succeeded')

    def test_csv_json_jsonl_imports_have_same_real_results(self):
        import csv
        from cleaning.clean import FIELDS
        rows=[['7','2010-10-19T10:00:00+08:00','30.27','-97.75','a'],
              ['7','2010-10-19T10:00:00+08:00','30.27','-97.75','a'],
              ['8','2010-10-20T00:00:00Z','95','-97.75','b']]
        stream=io.StringIO();writer=csv.writer(stream);writer.writerow(FIELDS);writer.writerows(rows)
        objects=[dict(zip(FIELDS,row)) for row in rows]
        payloads=[('sample.csv',stream.getvalue().encode()),('sample.json',json.dumps(objects).encode()),
                  ('sample.jsonl',('\n'.join(json.dumps(o) for o in objects)+'\n').encode())]
        for filename,payload in payloads:
            with self.subTest(filename=filename):
                response=self.upload(payload,filename=filename)
                self.assertEqual(response.status_code,202,response.json)
                process_job(self.store,response.json['job_id']);job=self.store.job(response.json['job_id'])
                self.assertEqual(job['status'],'succeeded',job)
                self.assertEqual((job['scanned_rows'],job['rejected_rows'],job['duplicates'],job['node_counts']),(3,1,1,[0,1]))
                result=self.client.get('/api/v2/checkins').json
                self.assertEqual(len(result['items']),1)
                self.assertEqual(result['items'][0]['checkin_time'],'2010-10-19T02:00:00+00:00')
                self.assertTrue(job['quality_samples'][0]['after'])
                self.assertEqual(job['quality_samples'][1]['outcome'],'not_retained')

    def test_field_mapping_identity_preview_and_retry(self):
        payload=b'person,at,y,x,venue\n7,2010-01-01T00:00:00Z,30,114,P\n'
        preview=self.client.post('/api/v2/imports/preview',data={'file':(io.BytesIO(payload),'custom.csv')},headers=self.headers)
        self.assertEqual(preview.status_code,200,preview.json)
        self.assertEqual(preview.json['columns'],['person','at','y','x','venue'])
        self.assertIsNotNone(preview.json['mapping_error'])
        mapping={'user_id':'person','checkin_time':'at','latitude':'y','longitude':'x','location_id':'venue'}
        def upload(mapping):
            return self.client.post('/api/v2/imports',data={'file':(io.BytesIO(payload),'custom.csv'),'mode':'replace','field_mapping':json.dumps(mapping)},headers=self.headers)
        first=upload(mapping);self.assertEqual(first.status_code,202)
        process_job(self.store,first.json['job_id']);job=self.store.job(first.json['job_id'])
        self.assertEqual(job['status'],'succeeded',job)
        self.assertEqual(job['field_mapping'],mapping)
        self.assertEqual(upload(mapping).json['job_id'],job['job_id'])
        changed={**mapping,'latitude':'x','longitude':'y'}
        second=upload(changed);self.assertEqual(second.status_code,202)
        self.assertNotEqual(second.json['job_id'],job['job_id'])
        original=self.store.nodes;self.store.nodes=['http://127.0.0.1:1',original[1]]
        process_job(self.store,second.json['job_id']);self.store.nodes=original
        retry=self.client.post('/api/v2/imports/'+second.json['job_id']+'/retry',headers=self.headers)
        self.assertEqual(retry.status_code,202)
        self.assertEqual(retry.json['import_config']['field_mapping'],changed)

    def test_malformed_formats_never_replace_active_data(self):
        batch=self.publish()['batch_id']
        for filename,payload in [('truncated.json',b'[{"user_id":"7"}'),('nested.json',b'{"items":[]}'),('bad.csv',b'user_id,latitude\n7,30\n'),('bad.jsonl',b'not-json\n')]:
            with self.subTest(filename=filename):
                response=self.upload(payload,filename=filename)
                self.assertEqual(response.status_code,202,response.json)
                process_job(self.store,response.json['job_id'])
                self.assertEqual(self.store.job(response.json['job_id'])['status'],'failed')
                self.assertEqual(self.store.active(),batch)
        bad=self.client.post('/api/v2/imports',data={'file':(io.BytesIO(SAMPLE),'test.tsv'),'coordinate_system':'GCJ02'},headers=self.headers)
        self.assertEqual(bad.status_code,400)
        self.assertEqual(bad.json['code'],'UNSUPPORTED_CRS')

    def test_representative_lifecycle_and_sample_evidence(self):
        job=self.publish(Path('sample/review-anomalies.tsv').read_bytes())
        self.assertEqual((job['scanned_rows'],job['rejected_rows'],job['duplicates'],sum(job['node_counts'])),(19,7,2,10))
        # The team module reports five POI rows as repaired and currently
        # chooses the wrong mode for this A/B/B/A/A sequence. These assertions
        # describe its actual integration contract, not correct consensus;
        # the independent regression suite exposes the upstream defect.
        self.assertEqual(job['cleaning_stats']['coord_repair'],5)
        changed=[row for row in job['quality_samples'] if row['outcome']=='coordinate_repaired']
        self.assertEqual(len(changed),3)
        self.assertTrue(any(row['reason']=='invalid_coordinates' for row in job['quality_samples'] if row['outcome']=='rejected'))
        for sample in job['quality_samples']:
            self.assertNotIn('_key',sample)
        catalog=self.client.get('/api/v2/catalog?group_by=location').json
        self.assertEqual(len(catalog['items']),6)
        plaza=self.client.get('/api/v2/checkins?location_id=synthetic-plaza').json['items']
        self.assertEqual(len(plaza),5)
        self.assertTrue(all(row['latitude']==31.27 and row['longitude']==-96.75 for row in plaza))

if __name__=='__main__': unittest.main()
