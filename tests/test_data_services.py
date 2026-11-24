"""Real HTTP-node and authenticated API acceptance tests for data services."""
import csv
import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlencode

from backend.control import Store
from backend.data_services import register_data_services
from backend.security import create_user
from backend.server import create_app
from backend.worker import process_job
from backend.batch_node import BatchNodeHandler
from db.load import shard_for


class QuietNode(BatchNodeHandler):
    def log_message(self, *_):
        pass


class DataServices(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.servers = []
        for root in self.store.shards:
            server = ThreadingHTTPServer(('127.0.0.1', 0), QuietNode)
            server.root = root
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.servers.append(server)
        self.store.nodes = [f'http://127.0.0.1:{server.server_port}' for server in self.servers]
        create_user(self.store, 'admin', 'data-service-admin-123', 'admin')
        create_user(self.store, 'viewer', 'data-service-viewer-123', 'viewer')
        self.app = create_app(self.store)
        if not any(rule.rule == '/api/v2/overview' for rule in self.app.url_map.iter_rules()):
            register_data_services(self.app, self.store)
        self.client = self.app.test_client()
        response = self.client.post('/api/v2/auth/login', json={'username': 'admin', 'password': 'data-service-admin-123'})
        self.headers = {'X-CSRF-Token': response.json['csrf_token']}

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        self.temp.cleanup()

    def publish(self, data=None):
        data = data or Path('sample/checkins.tsv').read_bytes()
        response = self.client.post('/api/v2/imports', data={'file': (io.BytesIO(data), 'fixture.tsv'), 'mode': 'replace'}, headers=self.headers)
        response.request.environ['wsgi.input'].close()
        self.assertIn(response.status_code, (200, 202), response.json)
        if response.status_code == 202:
            process_job(self.store, response.json['job_id'])
        job = self.store.job(response.json['job_id'])
        self.assertEqual(job['status'], 'succeeded', job)
        return job

    def seed(self, rows):
        """Seed a published immutable fixture, bypassing unrelated cleaning."""
        batch = 'B-' + 'a' * 24
        now = time.time()
        counts = [0, 0]
        schema = Path('db/schema.sql').read_text(encoding='utf-8')
        for index, root in enumerate(self.store.shards):
            with closing(sqlite3.connect(root / (batch + '.sqlite'))) as db:
                db.executescript(schema)
                own = [row for row in rows if shard_for(row[0]) == index]
                db.executemany('INSERT INTO checkins(user_id,checkin_time,latitude,longitude,location_id) VALUES (?,?,?,?,?)', own)
                db.commit()
                counts[index] = len(own)
        with self.store.db() as db:
            db.execute("INSERT INTO import_jobs(job_id,mode,source_hash,source_path,filename,rules_version,status,attempt,created_at,report,batch_id,created_by) VALUES ('fixture','replace','fixture','hidden','fixture.tsv','fixture','succeeded',1,?,?,?,'admin')", (now, json.dumps({'scanned_rows': len(rows), 'rejected_rows': 0, 'duplicates': 0, 'node_counts': counts}), batch))
            db.execute("INSERT INTO batches VALUES (?,'fixture','active',?,0,0,?,?)", (batch, len(rows), now, now))
            for index, count in enumerate(counts):
                db.execute('INSERT INTO batch_shards VALUES (?,?,?,?,1)', (batch, index, batch + '.sqlite', count))
            db.execute("UPDATE dataset_state SET active_batch_id=? WHERE dataset_id='gowalla'", (batch,))
        return batch

    def test_empty_overview_authentication_and_viewer_visibility(self):
        anonymous = self.app.test_client()
        for path in ('/api/v2/overview', '/api/v2/checkins/export'):
            self.assertEqual(anonymous.get(path).status_code, 401)
        result = self.client.get('/api/v2/overview')
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json['has_data'])
        self.assertEqual(result.json['total_count'], 0)
        self.assertIsNone(result.json['quality']['retention_rate'])
        self.assertIn('tasks', result.json)
        viewer = self.app.test_client()
        viewer.post('/api/v2/auth/login', json={'username': 'viewer', 'password': 'data-service-viewer-123'})
        self.assertNotIn('tasks', viewer.get('/api/v2/overview').json)
        self.assertEqual(viewer.get('/api/v2/checkins/export').status_code, 409)
        self.assertEqual(self.client.get('/api/v2/overview?limit=1').status_code, 400)
        self.assertEqual(self.client.get('/api/v2/overview?batch_id=a&batch_id=b').status_code, 400)

    def test_complete_import_overview_reconciliation_and_legacy_report(self):
        job = self.publish()
        result = self.client.get('/api/v2/overview').json
        self.assertEqual(result['batch_id'], job['batch_id'])
        self.assertTrue(result['nodes_consistent'])
        self.assertTrue(result['catalog_consistent'])
        self.assertEqual(result['total_count'], 4)
        self.assertEqual(result['entities'], {'users': 4, 'locations': 4, 'months': 1})
        self.assertEqual(result['monthly_trend'], [{'month': '2010-08', 'count': 4}])
        self.assertEqual((result['quality']['raw'], result['quality']['rejected'], result['quality']['duplicates'], result['quality']['inserted']), (6, 1, 1, 4))
        self.assertAlmostEqual(result['quality']['retention_rate'], 66.6667)
        self.assertIn('不代表数据准确率', result['quality']['retention_label'])
        self.assertTrue(result['time_range']['start'].startswith('2010-08-01'))
        with self.store.db() as db:
            db.execute("UPDATE import_jobs SET report='{}' WHERE job_id=?", (job['job_id'],))
            db.execute('UPDATE batch_shards SET row_count=row_count+1 WHERE node_id=0')
        old = self.client.get('/api/v2/overview').json
        self.assertIsNone(old['quality']['raw'])
        self.assertIsNone(old['quality']['inserted'])
        self.assertIsNone(old['quality']['coordinate_repairs'])
        self.assertFalse(old['nodes_consistent'])
        self.assertEqual(old['total_count'], 4)
        with self.store.db() as db:
            db.execute('UPDATE import_jobs SET report=? WHERE job_id=?', (json.dumps({'scanned_rows': 0, 'node_counts': [0, 0], 'database_import': {'nodes': None}}), job['job_id']))
        zero = self.client.get('/api/v2/overview')
        self.assertEqual(zero.status_code, 200)
        self.assertIsNone(zero.json['quality']['retention_rate'])

    def test_filter_export_csv_geojson_matches_query_and_preserves_batch(self):
        old = self.publish()['batch_id']
        self.publish(b'7\t2011-01-01T00:00:00Z\t30\t114\t999\n')
        filters = {'batch_id': old, 'west': 114.36, 'east': 115, 'south': 30.54, 'north': 31,
                   'user_id': '1', 'start': '2010-08-01T00:00:00Z', 'end': '2010-08-02T00:00:00Z'}
        query = self.client.get('/api/v2/checkins?' + urlencode(filters)).json
        response = self.client.get('/api/v2/checkins/export?' + urlencode({**filters, 'format': 'csv', 'limit': 1, 'offset': 100}))
        self.assertEqual(response.status_code, 200, response.json)
        self.assertTrue(response.data.startswith(b'\xef\xbb\xbf'))
        records = list(csv.DictReader(io.StringIO(response.data.decode('utf-8-sig'))))
        self.assertEqual(len(records), len(query['items']))
        self.assertEqual(records[0]['user_id'], '1')
        self.assertEqual(records[0]['batch_id'], old)
        metadata = json.loads(unquote(response.headers['X-Export-Metadata']))
        self.assertEqual(metadata['exported_count'], 1)
        self.assertNotIn('offset', metadata['filters'])
        self.assertEqual(response.headers['X-Checkin-Batch'], old)
        self.assertIn(old + '.csv', response.headers['Content-Disposition'])
        response = self.client.get('/api/v2/checkins/export?' + urlencode({**filters, 'format': 'geojson'}))
        result = json.loads(response.data)
        self.assertEqual(result['metadata']['batch_id'], old)
        self.assertEqual(result['features'][0]['geometry']['coordinates'], [query['items'][0]['longitude'], query['items'][0]['latitude']])
        radius = {'batch_id': old, 'format': 'geojson', 'center_lon': 114.36, 'center_lat': 30.54, 'radius_m': 1000}
        self.assertEqual(len(json.loads(self.client.get('/api/v2/checkins/export?' + urlencode(radius)).data)['features']), 1)
        viewer = self.app.test_client()
        viewer.post('/api/v2/auth/login', json={'username': 'viewer', 'password': 'data-service-viewer-123'})
        self.assertEqual(viewer.get('/api/v2/checkins/export?batch_id=' + old).status_code, 200)
        empty = self.client.get('/api/v2/checkins/export?format=geojson&start=2030-01-01T00:00:00Z')
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(json.loads(empty.data)['features'], [])

    def test_csv_formula_protection_escaping_and_geojson_identifier_fidelity(self):
        identities = ['=SUM(1,2)', '+123', '-123', '@evil', '  =1+1', '\tcode', '\rcode', 'normal,"quoted"\nnext']
        rows = [(user, '2010-01-01T00:00:00+00:00', 30, 114, user) for user in identities]
        self.seed(rows)
        response = self.client.get('/api/v2/checkins/export?format=csv')
        self.assertEqual(response.status_code, 200)
        records = list(csv.DictReader(io.StringIO(response.data.decode('utf-8-sig'))))
        self.assertEqual(len(records), len(rows))
        exported = {row['user_id'] for row in records}
        self.assertEqual(exported, {"'" + value for value in identities[:-1]} | {identities[-1]})
        geojson = json.loads(self.client.get('/api/v2/checkins/export?format=geojson').data)
        self.assertEqual({f['properties']['user_id'] for f in geojson['features']}, set(identities))
        self.assertTrue(all(f['geometry']['coordinates'] == [114, 30] for f in geojson['features']))

    def test_export_hard_boundary_no_silent_truncation(self):
        rows = [(str(i), '2010-01-01T00:00:00+00:00', 30, 114, 'same') for i in range(10000)]
        batch = self.seed(rows)
        response = self.client.get('/api/v2/checkins/export?format=csv')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['X-Export-Count'], '10000')
        self.assertEqual(len(list(csv.DictReader(io.StringIO(response.data.decode('utf-8-sig'))))), 10000)
        index = shard_for('overflow')
        with closing(sqlite3.connect(self.store.shards[index] / (batch + '.sqlite'))) as db:
            db.execute("INSERT INTO checkins(user_id,checkin_time,latitude,longitude,location_id) VALUES ('overflow','2010-01-01T00:00:00+00:00',30,114,'same')")
            db.commit()
        response = self.client.get('/api/v2/checkins/export?format=geojson')
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json['code'], 'EXPORT_LIMIT_EXCEEDED')
        self.assertNotIn('Content-Disposition', response.headers)
        self.assertIn('缩小', response.json['message'])
        narrowed = self.client.get('/api/v2/checkins/export?user_id=overflow&format=geojson')
        self.assertEqual(narrowed.status_code, 200)
        self.assertEqual(json.loads(narrowed.data)['metadata']['exported_count'], 1)

    def test_faults_are_atomic_and_all_node_reads_pin_initial_active_batch(self):
        old = self.publish()['batch_id']
        new = self.publish(b'7\t2011-01-01T00:00:00Z\t30\t114\t999\n')['batch_id']
        with self.store.db() as db:
            db.execute("UPDATE dataset_state SET active_batch_id=? WHERE dataset_id='gowalla'", (old,))
        from backend.data_services import node_request as actual
        paths = []

        def change_active(base, path):
            paths.append(path)
            with self.store.db() as db:
                db.execute("UPDATE dataset_state SET active_batch_id=? WHERE dataset_id='gowalla'", (new,))
            return actual(base, path)

        with patch('backend.data_services.node_request', side_effect=change_active):
            response = self.client.get('/api/v2/checkins/export?format=geojson')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.data)['metadata']['batch_id'], old)
        self.assertTrue(all(old in path for path in paths))
        self.assertEqual(len(paths), 2)
        original = self.store.nodes
        self.store.nodes = [original[0], 'http://127.0.0.1:1']
        for path in ('/api/v2/checkins/export', '/api/v2/overview'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 503)
            self.assertNotIn('Content-Disposition', response.headers)
            self.assertIn('request_id', response.json)
        self.store.nodes = original
        with patch('backend.data_services.node_request', return_value={'batch_id': new, 'items': [], 'count': 0}):
            self.assertEqual(self.client.get('/api/v2/checkins/export?batch_id=' + old).status_code, 503)
            self.assertEqual(self.client.get('/api/v2/overview?batch_id=' + old).status_code, 503)
        for query in ('format=exe', 'user_id=1&user_id=2', 'offset=10001', 'west=0', 'center_lon=0&center_lat=0&radius_m=0'):
            self.assertEqual(self.client.get('/api/v2/checkins/export?' + query).status_code, 400)


if __name__ == '__main__':
    unittest.main()
