"""Route only MCP/OAuth paths to a loopback ASGI worker; keep legacy API intact."""
import http.client
import os
import threading
import time
from urllib.parse import urlparse

REQUIRED = ('MCP_BASE_URL', 'MCP_GITHUB_CLIENT_ID', 'MCP_GITHUB_CLIENT_SECRET', 'MCP_GITHUB_USER_ID')


def is_mcp_path(path):
    return (path in {'/mcp', '/mcp/', '/authorize', '/token', '/register', '/revoke', '/consent'}
            or path.startswith('/auth/') or path.startswith('/.well-known/'))


def install(handler, db_path):
    # Missing OAuth setup must never start an anonymous server.
    ready = all(os.environ.get(key, '').strip() for key in REQUIRED)
    worker_port = None
    worker = None
    if ready:
        import socket
        import uvicorn
        from backend.project_mcp import build_mcp
        mcp = build_mcp(handler, db_path)
        host = urlparse(os.environ['MCP_BASE_URL']).netloc
        app = mcp.http_app(path='/mcp', json_response=True, stateless_http=True,
                           allowed_hosts=[host], allowed_origins=['https://chatgpt.com', os.environ['MCP_BASE_URL'].rstrip('/')])
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        sock.listen(128)
        worker_port = sock.getsockname()[1]
        worker = uvicorn.Server(uvicorn.Config(
            app, log_level='warning', access_log=False,
            proxy_headers=True, forwarded_allow_ips='127.0.0.1'))
        thread = threading.Thread(target=worker.run, kwargs={'sockets': [sock]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 15
        while not worker.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not worker.started:
            sock.close()
            raise RuntimeError('MCP worker did not start')

    def proxy(self):
        if worker_port is None:
            self._json({'error': 'MCP OAuth setup is incomplete'}, 503)
            return
        # This adapter uses JSON responses and deliberately offers no SSE stream.
        if self.command == 'GET' and urlparse(self.path).path in {'/mcp', '/mcp/'}:
            self._json({'error': 'Use HTTP POST for MCP'}, 405)
            return
        if self.headers.get('Transfer-Encoding'):
            self._json({'error': 'transfer encoding unsupported'}, 400)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 <= length <= 262144:
                self._json({'error': 'body too large'}, 413)
                return
            body = self.rfile.read(length) if length else None
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() in {'accept', 'authorization', 'content-type', 'cookie',
                                        'origin', 'mcp-protocol-version', 'mcp-session-id', 'last-event-id'}}
            headers['Host'] = urlparse(os.environ['MCP_BASE_URL']).netloc
            headers['X-Forwarded-Proto'] = 'https'
            connection = http.client.HTTPConnection('127.0.0.1', worker_port, timeout=45)
            try:
                connection.request(self.command, self.path, body=body, headers=headers)
                response = connection.getresponse()
                data = response.read()
                self.send_response(response.status)
                for key, value in response.getheaders():
                    if key.lower() not in {'connection', 'transfer-encoding', 'content-length', 'server', 'date'}:
                        self.send_header(key, value)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            finally:
                connection.close()
        except (ValueError, UnicodeError):
            self._json({'error': 'invalid request'}, 400)
        except (OSError, http.client.HTTPException):
            self._json({'error': 'MCP service unavailable'}, 502)

    for method in ('GET', 'POST', 'DELETE', 'OPTIONS'):
        previous = getattr(handler, 'do_' + method, None)
        def dispatch(self, previous=previous):
            if is_mcp_path(urlparse(self.path).path):
                return proxy(self)
            if previous:
                return previous(self)
            self._json({'error': 'method not allowed'}, 405)
        setattr(handler, 'do_' + method, dispatch)

    # OAuth callback query parameters contain short-lived codes; never log them.
    previous_log = handler.log_message
    def log_message(self, format, *args):
        if is_mcp_path(urlparse(self.path).path):
            return
        return previous_log(self, format, *args)
    handler.log_message = log_message
    return worker
