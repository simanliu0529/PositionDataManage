"""Regression tests using constructed records only; no course data is required."""

import csv
import json
import sqlite3
import threading
import unittest
from contextlib import closing, contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

from backend.app import AppHandler
from cleaning.clean import FIELDS
from db.load import FILENAMES, fingerprint, load, shard_for
from db.node import NodeHandler
from db.verify import verify


def write_csv(path, rows, *, header=FIELDS, encoding="utf-8"):
    with path.open("w", encoding=encoding, newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerows(rows)


def record(user="1", time="2010-08-01T12:00:00Z", lat="30", lon="114", location="101"):
    return [user, time, lat, lon, location]


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "clean.csv"
        self.output = self.root / "database"

    def test_dedupe_uses_normalized_time_and_numeric_coordinates(self):
        write_csv(self.source, [record(), record(time="2010-08-01T14:00:00+02:00", lat="30.0")])
        result = load(self.source, self.output, batch_size=1)
        self.assertEqual((result["valid"], result["duplicates"], result["inserted"]), (2, 1, 1))
        self.assertTrue(verify(self.output)["passed"])

    def test_ids_remain_text_and_sql_is_parameterized(self):
        user = "001'; DROP TABLE checkins; --"
        write_csv(self.source, [record(user=user, location="000101")])
        load(self.source, self.output)
        with closing(sqlite3.connect(self.output / FILENAMES[shard_for(user)])) as conn:
            self.assertEqual(conn.execute("SELECT user_id, location_id FROM checkins").fetchone(), (user, "000101"))

    def test_empty_bom_csv(self):
        write_csv(self.source, [], encoding="utf-8-sig")
        self.assertEqual(load(self.source, self.output)["node_counts"], [0, 0])

    def test_invalid_rows_abort_after_previously_committed_batch(self):
        invalid = [record(lat="NaN"), record(lon="inf"), record(lat="91"), record(user=" "),
                   record(time="2010-08-01T12:00:00"), record(time="2010-08-01T12:00:00.1Z"),
                   record(time="not-a-date"), ["too", "few"]]
        for row in invalid:
            with self.subTest(row=row):
                write_csv(self.source, [record(), row])
                with self.assertRaisesRegex(ValueError, "line 3"):
                    load(self.source, self.output, batch_size=1)
                self.assertEqual(list(self.output.iterdir()), [])

    def test_header_mismatch(self):
        write_csv(self.source, [], header=reversed(FIELDS))
        with self.assertRaisesRegex(ValueError, "header"):
            load(self.source, self.output)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_existing_database_is_preserved(self):
        write_csv(self.source, [record()])
        load(self.source, self.output)
        before = {p.name: fingerprint(p) for p in self.output.iterdir()}
        with self.assertRaises(FileExistsError):
            load(self.source, self.output)
        self.assertEqual(before, {p.name: fingerprint(p) for p in self.output.iterdir()})

    def test_concurrent_import_lock_is_preserved(self):
        write_csv(self.source, [])
        self.output.mkdir()
        lock = self.output / ".import.lock"
        lock.write_text("another importer", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            load(self.source, self.output)
        self.assertEqual(lock.read_text(encoding="utf-8"), "another importer")

    def test_publication_failure_removes_partial_pair(self):
        write_csv(self.source, [record()])
        original_rename = Path.rename

        def fail_second(path, target):
            if path.name == "node-1.sqlite":
                raise PermissionError("simulated publication failure")
            return original_rename(path, target)

        with patch.object(Path, "rename", fail_second):
            with self.assertRaises(PermissionError):
                load(self.source, self.output)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_sql_constraints_reject_invalid_coordinates(self):
        write_csv(self.source, [])
        load(self.source, self.output)
        with closing(sqlite3.connect(self.output / FILENAMES[0])) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO checkins(user_id,checkin_time,latitude,longitude,location_id) "
                             "VALUES ('1','2010-08-01T12:00:00+00:00',91,114,'101')")


class QuietNode(NodeHandler):
    def log_message(self, *args):
        pass


class QuietApp(AppHandler):
    def log_message(self, *args):
        pass


@contextmanager
def running_api(directory):
    servers = []
    threads = []
    try:
        for filename in FILENAMES:
            server = ThreadingHTTPServer(("127.0.0.1", 0), QuietNode)
            server.database = (directory / filename).resolve()
            servers.append(server)
        app = ThreadingHTTPServer(("127.0.0.1", 0), QuietApp)
        app.nodes = [f"http://127.0.0.1:{s.server_port}" for s in servers]
        servers.append(app)
        for server in servers:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            threads.append(thread)
        yield f"http://127.0.0.1:{app.server_port}", servers
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.users = [next(str(n) for n in range(100) if shard_for(str(n)) == i) for i in (0, 1)]
        rows = [record(user=user, time=f"2010-08-0{day}T12:00:00Z", location=str(day))
                for day in (1, 2, 3) for user in self.users]
        write_csv(self.root / "clean.csv", rows)
        self.directory = self.root / "db"
        load(self.root / "clean.csv", self.directory)

    def get(self, base, filters):
        with urlopen(base + "/api/checkins?" + urlencode(filters), timeout=10) as response:
            return json.load(response)

    def test_full_verification_and_query_contract(self):
        with running_api(self.directory) as (base, _):
            result = verify(self.directory, api_url=base, full_integrity=True)
            self.assertEqual(len(result["api_checks"]), 8)
            page = self.get(base, {"limit": 2, "offset": 1})
            self.assertEqual(page["queried_nodes"], [0, 1])
            self.assertEqual([(r["node"], r["location_id"]) for r in page["items"]], [(0, "3"), (1, "2")])
            self.assertTrue(page["has_more"])
            for i, user in enumerate(self.users):
                result = self.get(base, {"user_id": user})
                self.assertEqual(result["queried_nodes"], [i])
                self.assertEqual(len(result["items"]), 3)
            result = self.get(base, {"start": "2010-08-02T14:00:00+02:00", "end": "2010-08-03T12:00:00Z"})
            self.assertEqual([r["location_id"] for r in result["items"]], ["2", "2"])
            with self.assertRaises(HTTPError) as error:
                self.get(base, {"limit": 201})
            self.assertEqual(error.exception.code, 400)

    def test_required_node_failure_returns_503(self):
        with running_api(self.directory) as (base, servers):
            servers[1].shutdown()
            servers[1].server_close()
            with self.assertRaises(HTTPError) as error:
                self.get(base, {})
            self.assertEqual(error.exception.code, 503)
            self.assertEqual(self.get(base, {"user_id": self.users[0]})["queried_nodes"], [0])


if __name__ == "__main__":
    unittest.main()
