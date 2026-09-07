import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from backend.project_memory import ProjectMemory, ConflictError
from backend.project_api import install


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'audit.sqlite3'
        self.store = ProjectMemory(self.path)

    def payload(self, key='one', kind='memory', **extra):
        return dict(kind=kind, content={'text': 'test only'}, source='unit-test',
                    event_time='2026-09-07T17:00:00+08:00', idempotency_key=key, **extra)

    def test_reopen_and_idempotency(self):
        payload = self.payload()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.store.append(payload), range(4)))
        self.assertEqual(sum(created for _, created in results), 1)
        reopened = ProjectMemory(self.path)
        self.assertEqual(len(reopened.list()['records']), 1)
        self.assertEqual(reopened.list()['records'][0]['id'], results[0][0]['id'])
        with self.assertRaises(ConflictError):
            reopened.append(dict(payload, content={'text': 'changed'}))

    def test_corrections_keep_history_and_reject_stale_parent(self):
        original, _ = self.store.append(self.payload())
        change = self.payload('two', supersedes=original['id'], expected_version=1)
        corrected, _ = self.store.append(change)
        self.assertEqual(corrected['version'], 2)
        self.assertEqual(len(self.store.current()), 1)
        self.assertEqual(len(self.store.list()['records']), 2)
        with self.assertRaises(ConflictError):
            self.store.append(dict(change, idempotency_key='three'))

    def test_review_chain_and_candidate_only(self):
        decision, _ = self.store.append(self.payload(kind='decision'))
        review, _ = self.store.append(self.payload('review', 'review', related_ids=[decision['id']]))
        learning, _ = self.store.append(self.payload('learning', 'learning', related_ids=[review['id']]))
        self.assertEqual(learning['learning_status'], 'candidate')
        with self.assertRaises(ValueError):
            self.store.append(self.payload('bad', 'review', related_ids=[learning['id']]))
        with self.assertRaises(ValueError):
            self.store.append(self.payload('bad2', 'learning', related_ids=['missing']))
        page = self.store.list(limit=1)
        self.assertTrue(page['has_more'])
        self.assertEqual(self.store.list(after=page['next_after'])['records'][0]['id'], review['id'])

    def test_invalid_data(self):
        for payload in [[], self.payload(event_time_override='x'),
                        dict(self.payload(), event_time='2026-09-07'),
                        dict(self.payload(), content={'n': float('nan')}),
                        dict(self.payload(), kind='trade')]:
            with self.assertRaises(ValueError):
                self.store.append(payload)
        self.assertEqual(self.store.list()['records'], [])

    def test_http_auth_and_persistence(self):
        class Handler(BaseHTTPRequestHandler):
            def _json(self, obj, status=200):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def do_GET(self):
                self._json({'old': True})
            def do_POST(self):
                self._json({'old': True})
            def log_message(self, *args):
                pass
        install(Handler, self.path)
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        def request(method, path, payload=None, token=None):
            c = HTTPConnection('127.0.0.1', httpd.server_port, timeout=5)
            headers = {'X-Admin-Token': token} if token else {}
            c.request(method, path, json.dumps(payload) if payload is not None else None, headers)
            r = c.getresponse()
            result = r.status, json.loads(r.read())
            c.close()
            return result
        with patch.dict(os.environ, {'PAPER_TRADING_API_TOKEN': 'local-test-token'}):
            self.assertEqual(request('GET', '/api/project/records')[0], 401)
            self.assertEqual(request('POST', '/api/project/records', self.payload(), 'wrong')[0], 401)
            self.assertEqual(request('GET', '/api/project/context')[0], 401)
            status, body = request('POST', '/api/project/records', self.payload(), 'local-test-token')
            self.assertEqual(status, 201)
            self.assertEqual(request('POST', '/api/project/records', self.payload(), 'local-test-token')[0], 200)
            read = request('GET', '/api/project/records', token='local-test-token')[1]
            self.assertEqual(read['records'][0]['id'], body['record']['id'])
            self.assertEqual(request('GET', '/legacy')[1], {'old': True})
        with patch.dict(os.environ, {'PAPER_TRADING_API_TOKEN': ''}):
            self.assertEqual(request('GET', '/api/project/records')[0], 503)


if __name__ == '__main__':
    unittest.main()
