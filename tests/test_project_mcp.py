import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.github import GitHubProvider
from starlette.testclient import TestClient

from backend.project_mcp import build_mcp, persistent_secret
from backend.mcp_bridge import is_mcp_path


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'audit.sqlite3'
        env = patch.dict(os.environ, {
            'MCP_BASE_URL': 'https://example.test',
            'MCP_GITHUB_CLIENT_ID': 'unit-test-client',
            'MCP_GITHUB_CLIENT_SECRET': 'unit-test-secret',
            'MCP_GITHUB_USER_ID': '94347935',
        })
        env.start()
        self.addCleanup(env.stop)
        self.handler = SimpleNamespace(
            trading=SimpleNamespace(get_account=lambda _: None),
            store=SimpleNamespace(list_events=lambda _: []),
        )
        self.mcp = build_mcp(self.handler, self.db)

    def test_keys_survive_recreation(self):
        private = self.db.parent / 'mcp_oauth'
        before = (private / 'signing.secret').read_bytes()
        build_mcp(self.handler, self.db)
        self.assertEqual((private / 'signing.secret').read_bytes(), before)
        self.assertEqual((private / 'signing.secret').stat().st_mode & 0o777, 0o600)
        self.assertNotIn(b'unit-test-secret', before)

    def test_all_generated_routes_are_proxied(self):
        app = self.mcp.http_app(path='/mcp', json_response=True, stateless_http=True)
        for route in app.routes:
            self.assertTrue(is_mcp_path(route.path), route.path)
        self.assertFalse(is_mcp_path('/api/accounts'))
        self.assertFalse(is_mcp_path('/api/broker/orders'))

    def test_discovery_and_unauthenticated_request(self):
        app = self.mcp.http_app(path='/mcp', json_response=True, stateless_http=True)
        with TestClient(app, base_url='https://example.test') as client:
            metadata = client.get('/.well-known/oauth-authorization-server')
            self.assertEqual(metadata.status_code, 200)
            self.assertIn('S256', metadata.json()['code_challenge_methods_supported'])
            denied = client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
                                 headers={'accept': 'application/json, text/event-stream'})
            self.assertEqual(denied.status_code, 401)
            self.assertIn('resource_metadata', denied.headers['www-authenticate'])

    def test_bad_redirect_registration_rejected(self):
        app = self.mcp.http_app(path='/mcp', json_response=True, stateless_http=True)
        with TestClient(app, base_url='https://example.test') as client:
            bad = client.post('/register', json={
                'redirect_uris': ['https://attacker.example/callback'],
                'token_endpoint_auth_method': 'none',
            })
            self.assertEqual(bad.status_code, 400)

    def test_registration_and_consent_survive_provider_recreation(self):
        app = self.mcp.http_app(path='/mcp', json_response=True, stateless_http=True)
        redirect = 'https://chatgpt.com/connector_platform_oauth_redirect'
        with TestClient(app, base_url='https://example.test') as client:
            registration = client.post('/register', json={
                'redirect_uris': [redirect], 'token_endpoint_auth_method': 'none',
                'client_name': 'Setup test', 'grant_types': ['authorization_code', 'refresh_token'],
                'response_types': ['code'],
            })
            self.assertEqual(registration.status_code, 201, registration.text)
            client_id = registration.json()['client_id']
        rebuilt = build_mcp(self.handler, self.db)
        app = rebuilt.http_app(path='/mcp', json_response=True, stateless_http=True)
        with TestClient(app, base_url='https://example.test') as client:
            result = client.get('/authorize', params={
                'client_id': client_id, 'redirect_uri': redirect,
                'response_type': 'code', 'scope': 'read:user', 'state': 'test-state',
                'code_challenge': 'a' * 43, 'code_challenge_method': 'S256',
                'resource': 'https://example.test/mcp',
            }, follow_redirects=False)
            self.assertIn(result.status_code, (302, 303, 307), result.text)
            self.assertIn('/consent', result.headers['location'])
            consent = client.get(result.headers['location'])
            self.assertEqual(consent.status_code, 200)
            self.assertIn('Setup test', consent.text)

    def test_real_loopback_proxy_and_legacy_fallback(self):
        import http.client
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from backend.mcp_bridge import install
        class Handler(BaseHTTPRequestHandler):
            def _json(self, body, status=200):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def do_GET(self):
                self._json({'legacy': True})
            def do_POST(self):
                self._json({'legacy': True})
            def log_message(self, *args):
                pass
        worker = install(Handler, self.db)
        self.addCleanup(setattr, worker, 'should_exit', True)
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        def request(path):
            connection = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=5)
            connection.request('GET', path)
            response = connection.getresponse()
            result = response.status, json.loads(response.read())
            connection.close()
            return result
        self.assertEqual(request('/api/health'), (200, {'legacy': True}))
        status, metadata = request('/.well-known/oauth-authorization-server')
        self.assertEqual(status, 200)
        self.assertEqual(metadata['issuer'], 'https://example.test/')
        self.assertEqual(request('/mcp')[0], 405)

    def test_owner_gate_and_tool_read_write(self):
        owner = AccessToken(token='test-only', client_id='test', scopes=['read:user'],
                            claims={'sub': '94347935'})
        stranger = owner.model_copy(update={'claims': {'sub': '111'}})
        app = self.mcp.http_app(path='/mcp', json_response=True, stateless_http=True)
        headers = {'accept': 'application/json, text/event-stream',
                   'authorization': 'Bearer test-only', 'mcp-protocol-version': '2025-06-18'}
        with TestClient(app, base_url='https://example.test') as client:
            with patch.object(GitHubProvider, 'load_access_token', AsyncMock(return_value=stranger)):
                r = client.post('/mcp', headers=headers,
                                json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
                self.assertEqual(r.status_code, 401)
            with patch.object(GitHubProvider, 'load_access_token', AsyncMock(return_value=owner)):
                def call(name, arguments):
                    result = client.post('/mcp', headers=headers, json={
                        'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                        'params': {'name': name, 'arguments': arguments},
                    })
                    self.assertEqual(result.status_code, 200, result.text)
                    body = result.json()['result']
                    self.assertFalse(body.get('isError'), body)
                    return body['structuredContent']
                context = call('get_project_context', {})
                self.assertIsNone(context['account'])
                self.assertIsNone(context['market_quote'])
                record = {
                    'kind': 'memory', 'content': {'text': 'MCP local setup test'},
                    'source': 'unit-test', 'event_time': '2026-09-07T18:00:00+08:00',
                    'idempotency_key': 'mcp-test-1',
                }
                saved = call('append_project_record', {'record': record})
                self.assertTrue(saved['created'])
                self.assertFalse(call('append_project_record', {'record': record})['created'])
                read = call('list_project_records', {})
                self.assertEqual(read['records'][0]['id'], saved['record']['id'])


if __name__ == '__main__':
    unittest.main()
