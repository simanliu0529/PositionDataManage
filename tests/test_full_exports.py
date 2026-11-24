"""Full exports against real HTTP nodes and temporary published snapshots."""
import csv
import io
import json
import threading
import time
import unittest
import os
from unittest.mock import patch
from urllib.parse import urlencode

from backend.control import APIError
import test_data_services as fixtures


class FullExports(unittest.TestCase):
    setUp=fixtures.DataServices.setUp
    tearDown=fixtures.DataServices.tearDown
    seed=fixtures.DataServices.seed

    def create(self, query='', output='csv'):
        return self.client.post('/api/v2/exports',json={'query':query,'format':output},headers=self.headers)

    def wait(self, job_id):
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            response=self.client.get('/api/v2/exports/'+job_id)
            self.assertEqual(response.status_code,200,response.json)
            if response.json['status']!='running': return response.json
            time.sleep(.02)
        self.fail('Export did not finish')

    def test_complete_csv_over_multiple_windows_and_geojson_filtered(self):
        rows=[('=formula','2010-01-01T00:00:00+00:00',30,114,str(i)) for i in range(41005)]
        rows += [('other','2010-01-02T00:00:00+00:00',31,115,'outside')]
        batch=self.seed(rows)
        created=self.create('batch_id='+batch)
        self.assertEqual(created.status_code,202,created.json)
        job=self.wait(created.json['job_id'])
        self.assertEqual(job['status'],'succeeded',job)
        self.assertEqual(job['exported_count'],41006)
        self.assertEqual(job['scanned_count'],41006)
        response=self.client.get('/api/v2/exports/'+job['job_id']+'/download')
        try:
            records=list(csv.DictReader(io.StringIO(response.data.decode('utf-8-sig'))))
            self.assertEqual(len(records),41006)
            self.assertEqual(len({(r['node'],r['id']) for r in records}),41006)
            self.assertEqual(sum(r['user_id']=="'=formula" for r in records),41005)
            self.assertEqual({r['batch_id'] for r in records},{batch})
        finally: response.close()
        filters={'user_id':'=formula','start':'2010-01-01T00:00:00Z','end':'2010-01-02T00:00:00Z','center_lon':114,'center_lat':30,'radius_m':1}
        created=self.create(urlencode(filters),'geojson')
        job=self.wait(created.json['job_id'])
        self.assertEqual(job['status'],'succeeded',job)
        response=self.client.get('/api/v2/exports/'+job['job_id']+'/download')
        try:
            data=json.loads(response.data)
            self.assertEqual(len(data['features']),41005)
            self.assertEqual(data['metadata']['exported_count'],41005)
            self.assertEqual({f['properties']['user_id'] for f in data['features']},{'=formula'})
        finally: response.close()
        # A valid empty filter is also a complete file, not a failed export.
        created=self.create('west=0&east=1&south=0&north=1','geojson')
        job=self.wait(created.json['job_id'])
        self.assertEqual(job['status'],'succeeded',job)
        self.assertEqual(job['exported_count'],0)
        created=self.create('user_id=other')
        job=self.wait(created.json['job_id'])
        self.assertEqual(job['status'],'succeeded',job)
        self.assertEqual(job['exported_count'],1)
        self.assertEqual(job['scanned_count'],1)

    def test_ownership_csrf_cancel_busy_and_no_partial_download(self):
        self.seed([('u','2010-01-01T00:00:00+00:00',30,114,'l')])
        started=threading.Event();release=threading.Event()
        def blocked(*args):
            started.set();release.wait(5)
            return {'batch_id':'B-'+'a'*24,'items':[],'next_after':-(2**63),'scanned':0,'done':True}
        with patch('backend.export_jobs.node_request',side_effect=blocked):
            response=self.create();job_id=response.json['job_id']
            self.assertTrue(started.wait(2))
            self.assertEqual(self.create().status_code,409)
            self.assertEqual(self.client.get('/api/v2/exports/'+job_id+'/download').status_code,409)
            viewer=self.app.test_client()
            auth=viewer.post('/api/v2/auth/login',json={'username':'viewer','password':'data-service-viewer-123'})
            for suffix in ('','/download'):
                self.assertEqual(viewer.get('/api/v2/exports/'+job_id+suffix).status_code,404)
            self.assertEqual(viewer.post('/api/v2/exports/'+job_id+'/cancel',headers={'X-CSRF-Token':auth.json['csrf_token']}).status_code,404)
            self.assertEqual(self.client.post('/api/v2/exports',json={'query':'','format':'csv'}).status_code,403)
            self.assertEqual(self.client.post('/api/v2/exports/'+job_id+'/cancel',headers=self.headers).status_code,200)
            release.set();job=self.wait(job_id)
        self.assertEqual(job['status'],'cancelled',job)
        self.assertFalse((self.app.export_jobs.root/(job_id+'.part')).exists())
        self.assertEqual(self.client.get('/api/v2/exports/'+job_id+'/download').status_code,409)

    def test_node_failure_invalid_parameters_and_restart(self):
        self.seed([('u','2010-01-01T00:00:00+00:00',30,114,'l')])
        for query in ('batch_id=B-'+'b'*24,'start=not-a-date','west=1','user_id=a&user_id=b'):
            self.assertIn(self.create(query).status_code,(400,409))
        with patch('backend.export_jobs.node_request',side_effect=APIError(503,'NODE_UNAVAILABLE','Node unavailable')):
            response=self.create();job=self.wait(response.json['job_id'])
        self.assertEqual(job['status'],'failed')
        self.assertFalse((self.app.export_jobs.root/(job['job_id']+'.part')).exists())
        # An interrupted job can never expose its unfinished output after restart.
        job['status']='running'
        with self.store.db() as db: job['owner']=db.execute("SELECT user_id FROM users WHERE username='admin'").fetchone()[0]
        self.app.export_jobs.save(job)
        status=self.client.get('/api/v2/exports/'+job['job_id'])
        self.assertEqual(status.json['status'],'failed')
        self.assertNotIn('owner',status.json)
        self.assertEqual(self.client.get('/api/v2/exports/'+job['job_id']+'/download').status_code,409)

    def test_progress_retries_transient_windows_sharing_without_partial_json(self):
        manager=self.app.export_jobs;manager.root.mkdir(exist_ok=True)
        job={'job_id':'E-'+'c'*32,'owner':'test','status':'running','created_at':time.time()}
        manager.save(job)
        original=os.replace;attempts=[]
        def sharing_failure(source,target):
            attempts.append(1)
            if len(attempts)<3: raise PermissionError('Simulated Windows sharing violation')
            return original(source,target)
        with patch('backend.export_jobs.os.replace',side_effect=sharing_failure):
            manager.save({**job,'exported_count':20000})
        self.assertEqual(len(attempts),3)
        self.assertEqual(json.loads((manager.root/(job['job_id']+'.json')).read_text())['exported_count'],20000)


if __name__=='__main__': unittest.main()
