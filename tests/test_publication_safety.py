"""Regression tests for concurrent migration and immutable publication safety."""
import csv
import hashlib
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from backend.control import Store
from backend.security import create_user
from backend.server import create_app
from backend.worker import activate, process_job
from cleaning.clean import FIELDS
from backend.batch_node import BatchNodeHandler


class QuietNode(BatchNodeHandler):
    def log_message(self, *_):
        pass


class LegacyMigrationSafety(unittest.TestCase):
    def test_parallel_store_initialization_migrates_once_and_preserves_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # Build the real pre-migration table, including an existing task
            # and active-batch pointer. No newer Store runs before the race.
            with closing(sqlite3.connect(root / 'control.sqlite')) as db:
                db.executescript('''
                    PRAGMA journal_mode=WAL;
                    CREATE TABLE import_jobs (
                      job_id TEXT PRIMARY KEY,mode TEXT NOT NULL,source_hash TEXT NOT NULL,source_path TEXT NOT NULL,
                      filename TEXT NOT NULL,rules_version TEXT NOT NULL,status TEXT NOT NULL,attempt INTEGER NOT NULL,
                      created_at REAL NOT NULL,heartbeat_at REAL,run_id TEXT,report TEXT NOT NULL DEFAULT '{}',
                      batch_id TEXT,expected_batch_id TEXT,parent_job_id TEXT,created_by TEXT NOT NULL);
                    CREATE TABLE dataset_state(dataset_id TEXT PRIMARY KEY,active_batch_id TEXT);
                    INSERT INTO dataset_state VALUES ('gowalla','B-aaaaaaaaaaaaaaaaaaaaaaaa');
                    INSERT INTO import_jobs(job_id,mode,source_hash,source_path,filename,rules_version,status,attempt,created_at,created_by)
                      VALUES ('legacy-task','replace','legacy-hash','original-source','original.tsv','legacy-v1','succeeded',1,1,'admin');
                ''')
                self.assertNotIn('import_config', {row[1] for row in db.execute('PRAGMA table_info(import_jobs)')})
            gate = threading.Barrier(8)

            def initialize(_):
                gate.wait(timeout=10)
                store = Store(root)
                return store.active(), store.job('legacy-task')

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(initialize, range(8)))
            self.assertEqual(len(results), 8)
            for active, job in results:
                self.assertEqual(active, 'B-aaaaaaaaaaaaaaaaaaaaaaaa')
                self.assertEqual(job['filename'], 'original.tsv')
                self.assertEqual(job['source_hash'], 'legacy-hash')
                self.assertEqual(job['status'], 'succeeded')
                self.assertEqual(job['import_config'], {})
            with closing(sqlite3.connect(root / 'control.sqlite')) as db:
                columns = [row[1] for row in db.execute('PRAGMA table_info(import_jobs)')]
                self.assertEqual(columns.count('import_config'), 1)
                self.assertEqual(db.execute('SELECT count(*) FROM import_jobs').fetchone()[0], 1)
                self.assertEqual(db.execute('SELECT source_path FROM import_jobs').fetchone()[0], 'original-source')


class PublicationSafety(unittest.TestCase):
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
        create_user(self.store, 'admin', 'publication-test-admin-123', 'admin')
        self.app = create_app(self.store)
        self.client = self.app.test_client()
        login = self.client.post('/api/v2/auth/login', json={'username': 'admin', 'password': 'publication-test-admin-123'})
        self.headers = {'X-CSRF-Token': login.json['csrf_token']}

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        self.temp.cleanup()

    def upload(self, payload, filename='fixture.tsv', mode='replace'):
        response = self.client.post('/api/v2/imports', data={'file': (io.BytesIO(payload), filename), 'mode': mode}, headers=self.headers)
        response.request.environ['wsgi.input'].close()
        self.assertEqual(response.status_code, 202, response.json)
        return response.json['job_id']

    def publish_sample(self):
        job_id = self.upload(Path('sample/checkins.tsv').read_bytes())
        process_job(self.store, job_id)
        job = self.store.job(job_id)
        self.assertEqual(job['status'], 'succeeded', job)
        return job['batch_id']

    def test_report_failure_after_publish_and_rollback_keeps_historical_snapshots(self):
        old = self.publish_sample()
        job_id = self.upload(b'new-user\t2012-01-01T00:00:00Z\t30\t114\tnew-place\n')
        progress = self.store.progress
        published = []

        def fail_once_after_rollback(current_job, status, report):
            if status == 'succeeded' and current_job == job_id and not published:
                new = self.store.active()
                self.assertNotEqual(new, old)
                published.append(new)
                # Simulate an administrator rollback immediately after the
                # publication transaction and before the final report update.
                activate(self.store, old, new, 'admin')
                raise RuntimeError('simulated post-publication report write failure')
            return progress(current_job, status, report)

        with patch.object(self.store, 'progress', side_effect=fail_once_after_rollback):
            process_job(self.store, job_id)
        self.assertEqual(len(published), 1)
        new = published[0]
        job = self.store.job(job_id)
        self.assertEqual(job['status'], 'succeeded', job)
        self.assertEqual(job['batch_id'], new)
        self.assertIn('simulated post-publication', job['post_publish_warning'])
        self.assertEqual(self.store.active(), old)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT status FROM batches WHERE batch_id=?', (new,)).fetchone()[0], 'historical')
            self.assertEqual(db.execute('SELECT sum(count) FROM catalog_counts WHERE batch_id=?', (new,)).fetchone()[0], 3)
        for root in self.store.shards:
            self.assertTrue((root / (new + '.sqlite')).is_file())
            with closing(sqlite3.connect((root / (new + '.sqlite')).as_uri() + '?mode=ro', uri=True)) as db:
                self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0], 'ok')
        historical = self.client.get('/api/v2/checkins?batch_id=' + new)
        self.assertEqual(historical.status_code, 200, historical.json)
        self.assertEqual([row['user_id'] for row in historical.json['items']], ['new-user'])
        overview = self.client.get('/api/v2/overview?batch_id=' + new)
        self.assertEqual(overview.status_code, 200, overview.json)
        self.assertTrue(overview.json['nodes_consistent'])
        self.assertTrue(overview.json['catalog_consistent'])
        self.assertEqual(self.client.get('/api/v2/dataset/active').json['total_count'], 4)
        self.assertFalse((self.store.root / 'work' / job_id).exists())

    def test_parse_rejection_samples_keep_original_values_hashes_and_counts(self):
        old = self.publish_sample()
        good = dict(zip(FIELDS, ('good-user', '2012-01-01T00:00:00Z', '30', '114', 'good-place')))
        stream = io.StringIO(newline='')
        writer = csv.writer(stream)
        writer.writerow(FIELDS)
        writer.writerow(good.values())
        writer.writerow(['wrong', 'column', 'count'])
        cases = [
            ('fixture.csv', stream.getvalue().encode('utf-8'), 2, 1, {2: ['wrong', 'column', 'count']}),
            ('fixture.json', json.dumps([123, good, None, False]).encode('utf-8'), 4, 3, {1: 123, 3: None, 4: False}),
        ]
        for filename, payload, scanned, rejected, sources in cases:
            with self.subTest(format=filename):
                job_id = self.upload(payload, filename, mode='dry_run')
                process_job(self.store, job_id)
                job = self.store.job(job_id)
                self.assertEqual(job['status'], 'succeeded', job)
                self.assertEqual(job['source_hash'], hashlib.sha256(payload).hexdigest())
                self.assertEqual((job['scanned_rows'], job['rejected_rows'], job['duplicates'], sum(job['node_counts'])), (scanned, rejected, 0, 1))
                samples = {row['record_number']: row for row in job['parse_rejections']}
                self.assertEqual(set(samples), set(sources))
                evidence = {row['record_number']: row for row in job['quality_samples'] if row['outcome'] == 'rejected'}
                for number, source in sources.items():
                    self.assertEqual(json.loads(samples[number]['source']), source)
                    self.assertEqual(json.loads(evidence[number]['before']), source)
                    self.assertNotIn('INVALID_RECORD', evidence[number]['before'])
                    self.assertTrue(evidence[number]['parse_detail'])
                    self.assertIsNone(evidence[number]['after'])
                with self.store.db() as db:
                    saved_source = Path(db.execute('SELECT source_path FROM import_jobs WHERE job_id=?', (job_id,)).fetchone()[0])
                self.assertEqual(saved_source.read_bytes(), payload)
                self.assertEqual(job['import_config']['format'], Path(filename).suffix[1:])
                self.assertIsNone(job['batch_id'])
                self.assertEqual(self.store.active(), old)
                self.assertEqual(self.client.get('/api/v2/dataset/active').json['total_count'], 4)


if __name__ == '__main__':
    unittest.main()
