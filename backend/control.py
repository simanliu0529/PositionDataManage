"""Shared configuration, control storage and API errors for the complete service."""
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULES_VERSION = 'gowalla-upstream-238168f-db-1e1c602-web-v3'

class APIError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)

class Store:
    def __init__(self, root=None, shard_roots=None, nodes=None):
        self.root = Path(root or os.environ.get('CHECKIN_DATA', ROOT / 'data')).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.shards = [Path(p).resolve() for p in (shard_roots or [os.environ.get(f'CHECKIN_SHARD_{i}', self.root / 'shards' / f'node-{i}') for i in range(2)])]
        self.nodes = nodes or [os.environ.get(f'CHECKIN_NODE_{i}', f'http://127.0.0.1:{8100+i}') for i in range(2)]
        self.path = self.root / 'control.sqlite'
        for name in ['uploads', 'work']:
            (self.root / name).mkdir(exist_ok=True)
        for path in self.shards:
            path.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users (
              user_id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
              role TEXT NOT NULL CHECK(role IN ('admin','viewer')), enabled INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS sessions (
              session_key_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL,csrf TEXT NOT NULL,expires_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS login_limits (key TEXT PRIMARY KEY, attempts INTEGER NOT NULL, since REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS import_jobs (
              job_id TEXT PRIMARY KEY,mode TEXT NOT NULL,source_hash TEXT NOT NULL,source_path TEXT NOT NULL,
              filename TEXT NOT NULL,rules_version TEXT NOT NULL,status TEXT NOT NULL,attempt INTEGER NOT NULL,
              created_at REAL NOT NULL,heartbeat_at REAL,run_id TEXT,report TEXT NOT NULL DEFAULT '{}',
              batch_id TEXT,expected_batch_id TEXT,parent_job_id TEXT,created_by TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS unique_import ON import_jobs(mode,source_hash,rules_version) WHERE status != 'failed';
            CREATE TABLE IF NOT EXISTS batches (
              batch_id TEXT PRIMARY KEY,job_id TEXT NOT NULL,status TEXT NOT NULL,raw_count INTEGER NOT NULL,
              rejected_count INTEGER NOT NULL,duplicate_count INTEGER NOT NULL,published_at REAL,created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS batch_shards (
              batch_id TEXT NOT NULL,node_id INTEGER NOT NULL,storage_key TEXT NOT NULL,row_count INTEGER NOT NULL,
              ready INTEGER NOT NULL,PRIMARY KEY(batch_id,node_id));
            CREATE TABLE IF NOT EXISTS dataset_state (dataset_id TEXT PRIMARY KEY,active_batch_id TEXT);
            INSERT OR IGNORE INTO dataset_state VALUES ('gowalla',NULL);
            CREATE TABLE IF NOT EXISTS catalog_counts (
              batch_id TEXT NOT NULL,group_by TEXT NOT NULL,key TEXT NOT NULL,count INTEGER NOT NULL,
              PRIMARY KEY(batch_id,group_by,key));
            CREATE INDEX IF NOT EXISTS catalog_order ON catalog_counts(batch_id,group_by,count DESC,key);
            CREATE TABLE IF NOT EXISTS audit_log (
              id INTEGER PRIMARY KEY,action TEXT NOT NULL,actor TEXT NOT NULL,previous_batch_id TEXT,
              batch_id TEXT,created_at REAL NOT NULL);
            ''')
            # Web and worker may start against the same legacy database together.
            db.execute('BEGIN IMMEDIATE')
            columns = {row[1] for row in db.execute('PRAGMA table_info(import_jobs)')}
            if 'import_config' not in columns:
                db.execute("ALTER TABLE import_jobs ADD COLUMN import_config TEXT NOT NULL DEFAULT '{}'")

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA busy_timeout=30000')
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def active(self, db=None):
        if db is not None:
            return db.execute("SELECT active_batch_id FROM dataset_state WHERE dataset_id='gowalla'").fetchone()[0]
        with self.db() as conn:
            return self.active(conn)

    def batch(self, requested=None):
        batch_id = requested or self.active()
        if not batch_id:
            raise APIError(409, 'NO_ACTIVE_BATCH', '暂无已发布数据，请联系管理员导入。')
        with self.db() as db:
            row = db.execute("SELECT * FROM batches WHERE batch_id=? AND status IN ('active','historical')", (batch_id,)).fetchone()
        if row is None:
            raise APIError(409, 'BATCH_UNAVAILABLE', '此批次不可访问，请刷新后从最新批次查询。')
        return batch_id

    def job(self, job_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM import_jobs WHERE job_id=?', (job_id,)).fetchone()
        if row is None:
            raise APIError(404, 'JOB_NOT_FOUND', '任务不存在。')
        result = dict(row)
        result['import_config'] = json.loads(result['import_config'])
        result.update(json.loads(result.pop('report')))
        result.pop('source_path', None)
        result.pop('run_id', None)
        result.pop('staging_batch_id', None)
        return result

    def progress(self, job_id, status, report):
        with self.db() as db:
            db.execute('UPDATE import_jobs SET status=?,report=?,heartbeat_at=? WHERE job_id=?', (status,json.dumps(report,ensure_ascii=False),time.time(),job_id))
