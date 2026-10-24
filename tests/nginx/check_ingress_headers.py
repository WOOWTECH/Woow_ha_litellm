"""Real nginx + synthetic loopback upstream; no HA, credentials, or external traffic.

Run with NGINX_BINARY=<local binary> python check_ingress_headers.py.
Set NGINX_BROWSER=1 with a Playwright interpreter for same/cross-origin iframe checks.
CI also exercises the actual application iframe in smoke/browser_check.py.
"""
import http.client
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit

ENTRY = '/api/hassio_ingress/syntheticFixture123'
DEFAULT = "frame-ancestors 'none'"
HARDENING = "object-src 'none'; base-uri 'none'"
CASES = {
    'default': ([DEFAULT], ["frame-ancestors 'self'"], 'DENY'),
    'default-multi': ([DEFAULT, HARDENING], ["frame-ancestors 'self'", HARDENING], 'DENY'),
    'default-last': ([HARDENING, DEFAULT], [HARDENING, "frame-ancestors 'self'"], 'DENY'),
    'default-middle': ([HARDENING, DEFAULT, "script-src 'nonce-fixture'"],
                       [HARDENING, "frame-ancestors 'self'", "script-src 'nonce-fixture'"], 'DENY'),
    # Only one standalone default is rewritten; another enforcing deny survives.
    'duplicate-deny': ([DEFAULT, DEFAULT], [DEFAULT, "frame-ancestors 'self'"], 'DENY'),
    'nonce-plugin': (["script-src 'nonce-fixture'; frame-ancestors 'self'", HARDENING],
                     ["script-src 'nonce-fixture'; frame-ancestors 'self'", HARDENING], 'SAMEORIGIN'),
    'explicit-deny': (["script-src 'nonce-fixture'; frame-ancestors 'none'", HARDENING],
                      ["script-src 'nonce-fixture'; frame-ancestors 'none'", HARDENING], 'DENY'),
    'report-uri': (["report-uri /frame-ancestors; object-src 'none'", "base-uri 'self'"],
                   ["report-uri /frame-ancestors; object-src 'none'", "base-uri 'self'"], 'SAMEORIGIN'),
    'no-policy': ([], [], 'SAMEORIGIN'),
}
BODY = b'<html><body id="fixture">synthetic UI<script nonce="fixture">window.fixtureRan=true</script></body></html>'


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _plain(self, status=200, extra=()):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(b'{"upstream": "%s %s"}' % (self.command.encode(), self.path.encode()))

    def do_DELETE(self):
        self._plain()

    def do_PATCH(self):
        self._plain()

    def do_PUT(self):
        self._plain()

    def do_GET(self):
        url = urlsplit(self.path.removeprefix(ENTRY))
        if url.path == '/header':
            # A response header of the requested size (R10: nginx's default 4k/8k buffer gave 502).
            size = int(parse_qs(url.query)['bytes'][0])
            self._plain(extra=[('X-Woow-Large', 'h' * size)])
            return
        if url.path.startswith('/credentials/'):
            self._plain()
            return
        if url.path == '/panel':
            case = parse_qs(url.query)['case'][0]
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(f'<iframe src="http://127.0.0.1:{self.server.nginx_port}/case/{case}"></iframe>'.encode())
            return
        case = url.path.removeprefix('/case/')
        if case not in CASES:
            self.send_error(404)
            return
        headers, _, framing = CASES[case]
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_header('X-Frame-Options', framing)
        self.send_header('X-Content-Type-Options', 'nosniff')
        for value in headers:
            self.send_header('Content-Security-Policy', value)
        self.end_headers()
        self.wfile.write(BODY)


def unused_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class IngressHeaders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='woow-nginx-headers-')
        cls.addClassCleanup(cls.tmp.cleanup)
        root = Path(cls.tmp.name)
        cls.port = unused_port()
        cls.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        cls.upstream.nginx_port = cls.port
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.upstream.server_close)
        cls.addClassCleanup(cls.upstream.shutdown)
        template = Path('/usr/share/woow-litellm/nginx/ingress.conf.tmpl')
        if not template.exists():
            template = Path(__file__).resolve().parents[2]/'litellm/rootfs/usr/share/woow-litellm/nginx/ingress.conf.tmpl'
        if os.environ.get('NGINX_TEMPLATE'):
            template = Path(os.environ['NGINX_TEMPLATE'])
        source = template.read_text()
        # Keep the actual maps, proxy directives and startup page. Remap only the
        # synthetic peer/listen/upstream/prefix; no changes to the installed site.
        source = source.replace('@INGRESS_ENTRY@', ENTRY).replace('listen 8099', f'listen 127.0.0.1:{cls.port}')
        source = source.replace('172.30.32.2', '127.0.0.1').replace('127.0.0.1:4000', f'127.0.0.1:{cls.upstream.server_port}')
        # Readiness flag and Ingress name guard marker: same names, in this disposable root.
        cls.run_dir = root / 'run'
        cls.run_dir.mkdir()
        source = source.replace('/run/woow/', f'{cls.run_dir}/')
        (root/'site.conf').write_text(source)
        # The image has woow-nginx but need not have nginx's compiled default user.
        # This disposable single-process fixture uses root, never product config.
        (root/'nginx.conf').write_text(f'''user root root;
daemon off;
master_process off;
pid {root}/nginx.pid;
error_log {root}/error.log warn;
events {{ worker_connections 64; }}
http {{
 access_log off;
 client_body_temp_path {root}/client;
 proxy_temp_path {root}/proxy;
 fastcgi_temp_path {root}/fastcgi;
 uwsgi_temp_path {root}/uwsgi;
 scgi_temp_path {root}/scgi;
 include {root}/site.conf;
}}
''')
        cls.nginx = subprocess.Popen([os.environ.get('NGINX_BINARY', 'nginx'), '-e', str(root/'error.log'), '-p', str(root), '-c', str(root/'nginx.conf')],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        cls.addClassCleanup(cls.stop_nginx)
        for _ in range(100):
            if cls.nginx.poll() is not None:
                raise AssertionError(cls.nginx.communicate()[1].decode() + ((root/'error.log').read_text() if (root/'error.log').exists() else ''))
            try:
                cls.request('/case/default')
                return
            except OSError:
                time.sleep(0.02)
        raise AssertionError('synthetic nginx did not listen')

    @classmethod
    def stop_nginx(cls):
        if cls.nginx.poll() is None:
            cls.nginx.terminate()
            try:
                cls.nginx.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.nginx.kill()
                cls.nginx.wait(timeout=5)
        cls.nginx.stderr.close()

    @classmethod
    def request(cls, path, method='GET'):
        conn = http.client.HTTPConnection('127.0.0.1', cls.port, timeout=5)
        try:
            conn.request(method, path)
            response = conn.getresponse()
            return response.status, response.headers, response.read()
        finally:
            conn.close()

    def test_actual_upstream_multi_policy_preserved(self):
        for case, (_, expected, framing) in CASES.items():
            with self.subTest(case=case):
                status, headers, body = self.request('/case/'+case)
                self.assertEqual(status, 200)
                self.assertEqual(body, BODY)
                # Modern nginx coalesces duplicate CSP fields with comma; CSP
                # keeps their independent enforcement semantics across that list.
                policies = [p.strip() for h in headers.get_all('Content-Security-Policy', []) for p in h.split(',')]
                self.assertEqual(policies, expected)
                self.assertEqual(headers['X-Frame-Options'], 'SAMEORIGIN')
                self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')

    def test_browser_if_requested(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=['--disable-background-networking'])
            try:
                for case, allowed in [('default-multi', True), ('default-middle', True), ('nonce-plugin', True), ('explicit-deny', False), ('duplicate-deny', False)]:
                    for cross_origin in (False, True):
                        with self.subTest(case=case, cross_origin=cross_origin):
                            page = browser.new_page()
                            parent_port = self.upstream.server_port if cross_origin else self.port
                            page.goto(f'http://127.0.0.1:{parent_port}/panel?case={case}', wait_until='networkidle')
                            frames = [f for f in page.frames if f != page.main_frame]
                            rendered = any(f.evaluate("!!document.getElementById('fixture') && window.fixtureRan === true") for f in frames)
                            self.assertEqual(rendered, allowed and not cross_origin)
                            page.close()
            finally:
                browser.close()

    def flag(self, name, present):
        path = self.run_dir / name
        if present:
            path.write_text('')
        else:
            path.unlink(missing_ok=True)
        self.addCleanup(path.unlink, missing_ok=True)

    def test_large_upstream_header_is_relayed(self):
        # R10: a 10 KiB response header was an nginx 502 ("starting" page) while LiteLLM had answered.
        status, headers, body = self.request('/header?bytes=10240')
        self.assertEqual(status, 200)
        self.assertEqual(len(headers['X-Woow-Large']), 10240)
        self.assertTrue(body.startswith(b'{"upstream"'))

    def test_upstream_502_after_ready_is_json_not_starting(self):
        # Header beyond proxy_buffer_size: nginx's own 502. Before readiness: the starting page;
        # after it: a JSON error (no auto-reload, nothing a client would retry as "starting").
        for ready in (True, False):
            with self.subTest(ready=ready):
                self.flag('litellm.ready', ready)
                status, headers, body = self.request('/header?bytes=40000')
                self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
                self.assertEqual(headers['Cache-Control'], 'no-store')
                if ready:
                    self.assertEqual(status, 502)
                    self.assertEqual(headers['Content-Type'], 'application/json')
                    error = json.loads(body)['error']
                    self.assertEqual((error['type'], error['code']), ('ingress_proxy_error', '502'))
                    self.assertNotIn(b'starting', body)
                else:
                    self.assertEqual(status, 503)
                    self.assertIn(b'LiteLLM is starting', body)

    def test_percent_writes_refused_only_while_percent_names_exist(self):
        # R01: the Supervisor decodes paths once more; with a literal-%, '#' or '?' name stored, a
        # write carrying '%' may hit another resource, so it is refused (409) and nothing reaches LiteLLM.
        cases = [('DELETE', '/credentials/x%2Fy'), ('PATCH', '/credentials/x%20y'), ('PUT', '/model/a%25b/update'),
                 ('DELETE', '/credentials/x%23y'), ('PATCH', '/credentials/x%3Fy'),
                 ('PATCH', '/model/abc/update?x=%20'), ('DELETE', '/credentials/plain'), ('GET', '/credentials/x%2Fy')]
        for guard in (True, False):
            self.flag('ingress-percent-guard', guard)
            for method, path in cases:
                with self.subTest(guard=guard, method=method, path=path):
                    status, headers, body = self.request(path, method)
                    refused = guard and method != 'GET' and '%' in path.split('?')[0]
                    if refused:
                        self.assertEqual(status, 409)
                        self.assertEqual(headers['Content-Type'], 'application/json')
                        self.assertEqual(json.loads(body)['error']['code'], '409')
                        self.assertIn(b'4000', body)
                    else:
                        self.assertEqual(status, 200)
                        self.assertEqual(json.loads(body)['upstream'], f'{method} {ENTRY}{path}')

    def test_z_startup_503_has_nosniff(self):
        # Last test: remove only the synthetic listener to exercise nginx's own502→503.
        self.upstream.shutdown()
        self.upstream.server_close()
        status, headers, body = self.request('/case/default')
        self.assertEqual(status, 503)
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn("frame-ancestors 'self'", headers['Content-Security-Policy'])
        self.assertIn(b'LiteLLM is starting', body)


if __name__ == '__main__':
    names = unittest.defaultTestLoader.getTestCaseNames(IngressHeaders)
    if os.environ.get('NGINX_BROWSER') != '1':
        names.remove('test_browser_if_requested')
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(IngressHeaders(name) for name in names))
    raise SystemExit(not result.wasSuccessful())
