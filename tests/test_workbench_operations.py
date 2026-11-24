"""Startup safety and real workflow phase reporting, outside team modules."""
import hashlib
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from backend.batch_node import BatchNodeHandler
from backend.control import Store
from backend.diagnostics import local_checks,health,service_checks
from backend.security import create_user
from backend.server import create_app
from backend.worker import enqueue,process_job
from scripts import run_local


class QuietNode(BatchNodeHandler):
    def log_message(self,*_):pass


class StartupChecks(unittest.TestCase):
    def test_missing_data_check_is_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'absent';result=local_checks(root)
            self.assertFalse(result['ok']);self.assertFalse(root.exists())
            self.assertTrue(any(row['check']=='control_database' for row in result['checks']))

    def test_occupied_port_blocks_all_child_launches(self):
        with tempfile.TemporaryDirectory() as temporary,socket.socket() as listener:
            root=Path(temporary);store=Store(root);create_user(store,'viewer','startup-viewer-123','viewer')
            listener.bind(('127.0.0.1',0));listener.listen();port=listener.getsockname()[1]
            with patch.dict('os.environ',{'CHECKIN_DATA':str(root),'CHECKIN_SHARD_0':str(root/'shards'/'node-0'),'CHECKIN_SHARD_1':str(root/'shards'/'node-1')}):
                with patch('sys.argv',['run_local.py','--port',str(port),'--node-port','30000']),patch('scripts.run_local.subprocess.Popen') as launch:
                    with self.assertRaises(SystemExit) as error:run_local.main()
                    self.assertEqual(error.exception.code,1);launch.assert_not_called()
            self.assertEqual(len(list((root/'shards').glob('**/*.sqlite'))),0)

    def test_enabled_accounts_without_data_can_start_and_checks_do_not_leak_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);store=Store(root);secret='startup-account-private-123';create_user(store,'viewer',secret,'viewer')
            result=local_checks(root)
            self.assertTrue(result['ok']);self.assertIsNone(result['active_batch_id'])
            self.assertNotIn(secret,json.dumps(result));self.assertNotIn('password_hash',json.dumps(result))
            self.assertEqual(create_app(store).test_client().get('/healthz').json,{'service':'checkin-web','status':'ready'})
            self.assertEqual(create_app(store).test_client().get('/api/v2/overview').status_code,401)

    def test_wrong_service_identity_fails_readiness(self):
        import io
        for response in (b'[]',b'{"status":"ready","service":"other"}',b'{"status":"starting","service":"checkin-node"}'):
            with self.subTest(response=response),patch('backend.diagnostics.urlopen',return_value=io.BytesIO(response)):
                with self.assertRaises(ValueError):health('http://node','checkin-node')

    def test_missing_published_snapshot_is_not_healthy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);store=Store(root);create_user(store,'viewer','startup-account-private-123','viewer');batch='B-'+'f'*24
            with store.db() as db:
                db.execute("UPDATE dataset_state SET active_batch_id=?",(batch,))
                for i in range(2):db.execute('INSERT INTO batch_shards VALUES (?,?,?,?,1)',(batch,i,batch+'.sqlite',1))
            result=local_checks(root)
            self.assertFalse(result['ok']);self.assertEqual(sum(not c['ok'] for c in result['checks'] if c['check'].startswith('snapshot:')),2)


class ImportProgress(unittest.TestCase):
    def test_real_import_reports_verification_and_six_catalog_tasks(self):
        with tempfile.TemporaryDirectory() as temporary:
            store=Store(Path(temporary));servers=[]
            try:
                for root in store.shards:
                    server=ThreadingHTTPServer(('127.0.0.1',0),QuietNode);server.root=root;threading.Thread(target=server.serve_forever,daemon=True).start();servers.append(server)
                store.nodes=[f'http://127.0.0.1:{server.server_port}' for server in servers]
                source=store.root/'fixture.tsv';source.write_bytes(Path('sample/checkins.tsv').read_bytes())
                job_id,_=enqueue(store,source,'fixture.tsv',hashlib.sha256(source.read_bytes()).hexdigest(),'replace','test')
                snapshots=[];original=store.progress
                def record(job,status,report):
                    snapshots.append((status,json.loads(json.dumps(report))));return original(job,status,report)
                with patch.object(store,'progress',side_effect=record):process_job(store,job_id)
                job=store.job(job_id);self.assertEqual(job['status'],'succeeded',job)
                publishing=[r['progress'] for status,r in snapshots if status=='publishing' and r.get('progress',{}).get('stage')=='publishing']
                self.assertTrue(publishing);self.assertEqual(publishing[-1]['completed'],6)
                self.assertTrue(all(r['total']==6 for r in publishing));self.assertEqual(sorted(r['completed'] for r in publishing),[r['completed'] for r in publishing])
                verification=[r['progress'] for status,r in snapshots if status=='verifying' and r.get('progress',{}).get('stage')=='verifying']
                self.assertEqual(verification[-1]['completed'],2)
                checks=service_checks(local_checks(store.root),store.nodes[0],store.nodes)
                self.assertTrue(all(c['ok'] for c in checks if c['check'].startswith('published_data:')))
                self.assertEqual(sum(job['node_counts']),4)
            finally:
                for server in servers:server.shutdown();server.server_close()

if __name__=='__main__':unittest.main()
