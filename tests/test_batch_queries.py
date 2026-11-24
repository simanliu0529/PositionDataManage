"""Exact-query regressions for bounded-prefix reads and cancelled responses."""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.batch_node import BatchNodeHandler, read_checkins
from backend.control import APIError
from backend.http_io import Handler


class BatchQueries(unittest.TestCase):
    BATCH = 'B-' + 'a' * 24
    WIDE = {'west': -100.0, 'east': -20.0, 'south': -40.0, 'north': 40.0}

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db = sqlite3.connect(self.root / (self.BATCH + '.sqlite'))
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE checkins (
                id INTEGER PRIMARY KEY, user_id TEXT NOT NULL,
                checkin_time TEXT NOT NULL, latitude REAL NOT NULL,
                longitude REAL NOT NULL, location_id TEXT NOT NULL
            );
            CREATE INDEX idx_checkin_time ON checkins(checkin_time DESC,id DESC);
            CREATE INDEX idx_user_time ON checkins(user_id,checkin_time DESC,id DESC);
            CREATE INDEX idx_latitude ON checkins(latitude);
        ''')
        moment = datetime(2010, 1, 1, tzinfo=timezone.utc)
        self.records = [
            (index + 1, 'user-' + str(index),
             (moment + timedelta(minutes=index // 3)).isoformat(timespec='seconds'),
             65.0 if index % 4 == 0 else 20.0 + index % 3,
             120.0 if index % 5 == 0 else -70.0,
             'place-' + str(index))
            for index in range(240)
        ]
        self.replace(self.records)

    def tearDown(self):
        self.db.close()
        self.temporary.cleanup()

    def replace(self, records):
        self.db.execute('DELETE FROM checkins')
        self.db.executemany('INSERT INTO checkins VALUES (?,?,?,?,?,?)', records)
        self.db.commit()

    def complete(self, maximum, *, west=-100.0, east=-20.0, south=-40.0,
                 north=40.0, start=None, end=None, user=None, location=None):
        """Independent full SELECT: no production predicate/plan builder used."""
        sql = '''SELECT * FROM checkins
                 WHERE longitude >= ? AND longitude <= ?
                   AND latitude >= ? AND latitude <= ?'''
        values = [west, east, south, north]
        if start is not None:
            sql += ' AND checkin_time >= ?'
            values.append(start)
        if end is not None:
            sql += ' AND checkin_time < ?'
            values.append(end)
        if user is not None:
            sql += ' AND user_id = ?'
            values.append(user)
        if location is not None:
            sql += ' AND location_id = ?'
            values.append(location)
        # Fetch the complete ordered match set, then limit in Python so that
        # this oracle cannot accidentally share the bounded-prefix strategy.
        rows = self.db.execute(sql + ' ORDER BY checkin_time DESC,id DESC', values).fetchall()
        return [dict(row) for row in rows[:maximum]]

    def assert_complete(self, rows, maximum, **filters):
        self.assertEqual([dict(row) for row in rows], self.complete(maximum, **filters))

    def test_wide_prefix_matches_complete_ordered_query(self):
        with patch('backend.batch_node.PREFIX_ROWS', 4), patch('backend.batch_node.MAX_PREFIX_ROWS', 12):
            rows, strategy = read_checkins(self.db, self.WIDE, 3)
        self.assertEqual(strategy, 'time_prefix')
        self.assertEqual(len(rows), 3)
        self.assert_complete(rows, 3)

    def test_time_filtered_prefix_uses_half_open_range(self):
        start = '2010-01-01T00:20:00+00:00'
        end = '2010-01-01T00:30:00+00:00'
        filters = {**self.WIDE, 'start': start, 'end': end}
        with patch('backend.batch_node.PREFIX_ROWS', 4), patch('backend.batch_node.MAX_PREFIX_ROWS', 12):
            rows, strategy = read_checkins(self.db, filters, 3)
        self.assertEqual(strategy, 'time_prefix')
        self.assert_complete(rows, 3, start=start, end=end)
        self.assertTrue(all(start <= row['checkin_time'] < end for row in rows))

    def test_equal_times_preserve_descending_id_order(self):
        timestamp = '2010-01-01T12:00:00+00:00'
        self.replace([(identifier, 'u' + str(identifier), timestamp, 20, -70, 'p')
                      for identifier in (104, 101, 105, 102, 103)])
        rows, strategy = read_checkins(self.db, self.WIDE, 3)
        self.assertEqual(strategy, 'time_prefix')
        self.assertEqual([row['id'] for row in rows], [105, 104, 103])
        self.assert_complete(rows, 3)

    def test_insufficient_prefix_falls_back_and_finds_older_matches(self):
        self.replace([
            (index, 'u' + str(index), f'2010-01-01T00:00:{index:02d}+00:00',
             20 if index <= 4 else 70, -70, 'p')
            for index in range(1, 9)
        ])
        with patch('backend.batch_node.PREFIX_ROWS', 2), patch('backend.batch_node.MAX_PREFIX_ROWS', 3):
            rows, strategy = read_checkins(self.db, self.WIDE, 2)
        self.assertEqual(strategy, 'filtered')
        self.assertEqual([row['id'] for row in rows], [4, 3])
        self.assert_complete(rows, 2)

    def test_genuinely_empty_box_returns_no_sample_matches(self):
        filters = {**self.WIDE, 'west': 80.0, 'east': 90.0}
        with patch('backend.batch_node.PREFIX_ROWS', 3), patch('backend.batch_node.MAX_PREFIX_ROWS', 8):
            rows, strategy = read_checkins(self.db, filters, 5)
        self.assertEqual(strategy, 'filtered')
        self.assertEqual(rows, [])
        self.assert_complete(rows, 5, west=80.0, east=90.0)

    def test_user_and_location_filters_keep_exact_result(self):
        for key, oracle_name, value in [('user_id', 'user', 'user-239'),
                                        ('location_id', 'location', 'place-239')]:
            with self.subTest(filter=key):
                rows, strategy = read_checkins(self.db, {**self.WIDE, key: value}, 10)
                self.assertEqual(strategy, 'filtered')
                self.assert_complete(rows, 10, **{oracle_name: value})

    def test_candidate_maximum_includes_offset_and_one_more_row(self):
        offset, limit = 5, 3
        maximum = offset + limit + 1
        with patch('backend.batch_node.PREFIX_ROWS', 16), patch('backend.batch_node.MAX_PREFIX_ROWS', 32):
            rows, _ = read_checkins(self.db, self.WIDE, maximum)
        self.assertEqual(len(rows), maximum)
        self.assert_complete(rows, maximum)
        full_matches = self.complete(10001)
        self.assertEqual([dict(row) for row in rows[offset:offset + limit]],
                         full_matches[offset:offset + limit])

    def test_export_sentinel_maximum_returns_all_when_fewer_than_10001(self):
        with patch('backend.batch_node.PREFIX_ROWS', 8), patch('backend.batch_node.MAX_PREFIX_ROWS', 16):
            rows, strategy = read_checkins(self.db, self.WIDE, 10001)
        self.assertEqual(strategy, 'filtered')
        self.assertGreater(len(rows), 16)
        self.assertLess(len(rows), 10001)
        self.assert_complete(rows, 10001)

    def test_http_paths_supply_query_prefix_or_export_sentinel_maximum(self):
        for path, maximum in [('/checkins?limit=3&offset=5', 9), ('/export', 10001)]:
            with self.subTest(path=path):
                handler = object.__new__(BatchNodeHandler)
                handler.server = SimpleNamespace(root=self.root)
                separator = '&' if '?' in path else '?'
                handler.path = path + separator + 'batch_id=' + self.BATCH
                handler.json = Mock()
                with patch('backend.batch_node.read_checkins', wraps=read_checkins) as reader:
                    handler.get()
                self.assertEqual(reader.call_args.args[2], maximum)
                status, body = handler.json.call_args.args
                self.assertEqual(status, 200)
                # All coordinates are valid here: the unfiltered path should
                # expose the exact newest rows required by each endpoint.
                expected = sorted(self.records, key=lambda row: (row[2], row[0]), reverse=True)[:maximum]
                self.assertEqual([row['id'] for row in body['items']], [row[0] for row in expected])

    def test_zero_budget_times_out_and_clears_progress_handler(self):
        # A few thousand local fixture rows guarantee at least one SQLite VM
        # progress callback. Zero budget is deterministic and requires no sleep.
        self.db.executescript('''
            WITH RECURSIVE sequence(value) AS (
                VALUES(1) UNION ALL SELECT value+1 FROM sequence WHERE value<12000
            )
            INSERT INTO checkins
            SELECT 10000+value,'bulk-'||value,'2010-01-01T00:00:00+00:00',
                   20,-70,'bulk' FROM sequence;
        ''')
        with patch('backend.batch_node.QUERY_SECONDS', 0):
            with self.assertRaises(APIError) as caught:
                read_checkins(self.db, {**self.WIDE, 'west': 80, 'east': 90}, 50)
        self.assertEqual((caught.exception.status, caught.exception.code), (504, 'QUERY_TIMEOUT'))
        # This aggregate executes enough VM instructions to fail if the expired
        # handler is still installed; SELECT 1 alone would not establish that.
        count, total = self.db.execute('SELECT count(*),sum(id) FROM checkins').fetchone()
        self.assertEqual(count, len(self.records) + 12000)
        self.assertGreater(total, 0)
        rows, _ = read_checkins(self.db, self.WIDE, 2)
        self.assert_complete(rows, 2)


class DisconnectedResponse(unittest.TestCase):
    def test_cancelled_client_does_not_trigger_a_second_error_response(self):
        for error_type in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            with self.subTest(error=error_type.__name__):
                handler = object.__new__(Handler)
                handler.close_connection = False
                handler.send_response = Mock()
                handler.send_header = Mock()
                handler.end_headers = Mock()
                handler.wfile = SimpleNamespace(write=Mock(side_effect=error_type('client left')))
                handler.get = lambda: handler.json(200, {'ok': True})
                handler.do_GET()
                handler.send_response.assert_called_once_with(200)
                handler.wfile.write.assert_called_once()
                self.assertTrue(handler.close_connection)

    def test_disconnect_while_sending_headers_is_also_terminal(self):
        handler = object.__new__(Handler)
        handler.close_connection = False
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock(side_effect=ConnectionResetError('headers cancelled'))
        handler.wfile = SimpleNamespace(write=Mock())
        handler.get = lambda: handler.json(200, {'ok': True})
        handler.do_GET()
        handler.send_response.assert_called_once_with(200)
        handler.wfile.write.assert_not_called()
        self.assertTrue(handler.close_connection)


if __name__ == '__main__':
    unittest.main()
