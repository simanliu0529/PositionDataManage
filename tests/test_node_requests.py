"""Node transport errors stay distinguishable without real network waits."""
import io
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from backend.control import APIError, Store
from backend.security import create_user
from backend.server import create_app
from backend.worker import node_request


class NodeRequests(unittest.TestCase):
    def error(self, expected_status, expected_code, raised):
        with patch('backend.worker.urlopen', side_effect=raised), self.assertLogs('backend.worker', level='WARNING') as captured:
            with self.assertRaises(APIError) as caught:
                node_request('http://127.0.0.1:8100', '/checkins?user_id=private-user')
        self.assertEqual((caught.exception.status, caught.exception.code), (expected_status, expected_code))
        self.assertNotIn('private-user', '\n'.join(captured.output))
        self.assertNotIn('secret.sqlite', caught.exception.message)
        self.assertNotIn('SELECT', caught.exception.message)
        self.assertIn(expected_code, '\n'.join(captured.output))
        return caught.exception

    @staticmethod
    def http_error(status, payload):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode('utf-8')
        return HTTPError('http://127.0.0.1:8100/checkins', status,
                         'SELECT secret FROM E:/secret.sqlite', {}, io.BytesIO(body))

    def test_successful_object_response_keeps_existing_eight_second_budget(self):
        with patch('backend.worker.urlopen', return_value=io.BytesIO(b'{"items":[],"count":0}')) as opener:
            self.assertEqual(node_request('http://node', '/count'), {'items': [], 'count': 0})
        opener.assert_called_once_with('http://node/count', timeout=8)

    def test_direct_and_url_wrapped_socket_timeouts_are_504(self):
        for raised in (TimeoutError('secret.sqlite'), socket.timeout('secret.sqlite'),
                       URLError(TimeoutError('secret.sqlite')), URLError(socket.timeout('secret.sqlite'))):
            with self.subTest(error=type(raised).__name__):
                error = self.error(504, 'NODE_TIMEOUT', raised)
                self.assertIn('缩小查询条件', error.message)

    def test_connection_failures_are_503_instead_of_query_timeout(self):
        for raised in (ConnectionRefusedError('secret.sqlite'), ConnectionResetError('secret.sqlite'),
                       URLError(ConnectionRefusedError('secret.sqlite')), OSError('secret.sqlite')):
            with self.subTest(error=type(raised).__name__):
                self.error(503, 'NODE_UNAVAILABLE', raised)

    def test_missing_batch_and_sql_query_timeout_keep_safe_specific_codes(self):
        missing = self.http_error(409, {'code': 'BATCH_UNAVAILABLE', 'message': 'E:/secret.sqlite'})
        self.error(409, 'BATCH_UNAVAILABLE', missing)
        timeout = self.http_error(504, {'code': 'QUERY_TIMEOUT', 'message': 'SELECT * FROM E:/secret.sqlite'})
        error = self.error(504, 'QUERY_TIMEOUT', timeout)
        self.assertIn('缩小时间或地图范围', error.message)
        self.assertTrue(missing.fp.closed)
        self.assertTrue(timeout.fp.closed)

    def test_other_http_errors_remain_safe_and_distinguishable(self):
        for status, payload, expected_status, expected_code in (
            (500, {'code': 'INTERNAL', 'message': 'SELECT * FROM secret.sqlite'}, 502, 'NODE_HTTP_ERROR'),
            (404, b'private HTML error in E:/secret.sqlite', 502, 'NODE_HTTP_ERROR'),
            (409, {'code': 'UNKNOWN_ERROR', 'message': 'secret.sqlite'}, 502, 'NODE_HTTP_ERROR'),
            (429, {}, 503, 'NODE_BUSY'),
            (504, {}, 504, 'NODE_TIMEOUT'),
        ):
            with self.subTest(http_status=status):
                error = self.error(expected_status, expected_code, self.http_error(status, payload))
                if expected_code == 'NODE_HTTP_ERROR':
                    self.assertIn(str(status), error.message)

    def test_invalid_json_or_non_object_success_body_is_502(self):
        for payload in (b'not JSON', b'\xff', b'null', b'[]', b'123', b'true', b'"string"'):
            with self.subTest(payload=payload):
                with patch('backend.worker.urlopen', return_value=io.BytesIO(payload)):
                    with self.assertLogs('backend.worker', level='WARNING'), self.assertRaises(APIError) as caught:
                        node_request('http://node', '/checkins')
                self.assertEqual((caught.exception.status, caught.exception.code), (502, 'INVALID_NODE_RESPONSE'))

    def test_failure_during_body_read_is_classified_as_transport_timeout(self):
        class TimedOutBody(io.BytesIO):
            def read(self, *_):
                raise socket.timeout('secret.sqlite')
        with patch('backend.worker.urlopen', return_value=TimedOutBody()):
            with self.assertLogs('backend.worker', level='WARNING'), self.assertRaises(APIError) as caught:
                node_request('http://node', '/checkins')
        self.assertEqual((caught.exception.status, caught.exception.code), (504, 'NODE_TIMEOUT'))

    def test_failure_logs_strip_credentials_filters_and_exception_details(self):
        with patch('backend.worker.urlopen', side_effect=OSError('SELECT secret FROM E:/secret.sqlite')):
            with self.assertLogs('backend.worker', level='WARNING') as captured, self.assertRaises(APIError):
                node_request('http://node-user:secret-password@node:8100', '/checkins?user_id=private-user')
        output = '\n'.join(captured.output)
        self.assertIn('node=node:8100 route=/checkins', output)
        for secret in ('node-user', 'secret-password', 'private-user', 'secret.sqlite', 'SELECT'):
            self.assertNotIn(secret, output)

    def test_one_node_timeout_returns_error_without_partial_query_items(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary), nodes=['http://node-0', 'http://node-1'])
            batch = 'B-' + 'a' * 24
            with store.db() as db:
                db.execute("INSERT INTO batches VALUES (?,'fixture','active',1,0,0,1,1)", (batch,))
                db.execute("UPDATE dataset_state SET active_batch_id=? WHERE dataset_id='gowalla'", (batch,))
            create_user(store, 'viewer', 'node-query-viewer-123', 'viewer')
            client = create_app(store).test_client()
            login = client.post('/api/v2/auth/login', json={'username': 'viewer', 'password': 'node-query-viewer-123'})
            self.assertEqual(login.status_code, 200)
            item = {'id': 1, 'user_id': 'visible-only-if-all-nodes-succeed', 'checkin_time': '2010-01-01T00:00:00+00:00',
                    'latitude': 30, 'longitude': 114, 'location_id': 'fixture'}

            def transport(url, timeout):
                self.assertEqual(timeout, 8)
                if url.startswith('http://node-0/'):
                    return io.BytesIO(json.dumps({'batch_id': batch, 'items': [item]}).encode('utf-8'))
                raise socket.timeout('simulated node-1 read timeout')

            with patch('backend.worker.urlopen', side_effect=transport) as opener:
                with self.assertLogs('backend.worker', level='WARNING'):
                    response = client.get('/api/v2/checkins?west=-180&east=180&south=-90&north=90')
            self.assertEqual(opener.call_count, 2)
            self.assertEqual(response.status_code, 504)
            self.assertEqual(response.json['code'], 'NODE_TIMEOUT')
            self.assertTrue(response.json['request_id'])
            self.assertNotIn('items', response.json)
            self.assertNotIn('visible-only-if-all-nodes-succeed', response.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
