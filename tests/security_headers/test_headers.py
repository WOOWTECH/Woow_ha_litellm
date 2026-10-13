"""Public ASGI response contract; downstream authentication is left intact."""
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import asyncio

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient


def middleware_class():
    path = Path(os.environ.get('LITELLM_SECURITY_MODULE', Path(__file__).resolve().parents[2] / 'litellm/patches/security_headers/security_headers_middleware.py'))
    spec = importlib.util.spec_from_file_location('under_test_security_headers', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SecurityHeadersMiddleware


def application():
    async def endpoint(request: Request):
        if request.url.path.startswith('/key/'):
            if request.headers.get('Authorization') != 'Bearer synthetic-fixture-key':
                return JSONResponse({'error': 'unauthorized'}, status_code=401)
            return JSONResponse({'key': 'synthetic-fixture-key'}, headers={'Cache-Control': 'public, max-age=600'})
        return HTMLResponse('<html><body>Next-compatible inline shell</body></html>')
    app = Starlette(routes=[Route('/{path:path}', endpoint, methods=['GET','POST'])])
    return middleware_class()(app)


class HeadersContract(unittest.TestCase):
    def test_credential_response_is_not_stored(self):
        with TestClient(application()) as client:
            response = client.post('/key/generate', headers={'Authorization': 'Bearer synthetic-fixture-key'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'key': 'synthetic-fixture-key'})
        self.assertEqual(response.headers.get('cache-control'), 'no-store')

    def test_html_has_narrow_csp_and_no_referrer(self):
        with TestClient(application()) as client:
            response = client.get('/ui/')
        csp = response.headers.get('content-security-policy', '')
        self.assertIn("object-src 'none'", csp)
        self.assertIn("base-uri 'none'", csp)
        self.assertEqual(response.headers.get('referrer-policy'), 'no-referrer')
        self.assertNotIn('script-src', csp)
        self.assertNotIn('default-src', csp)

    def respond(self, path='/', headers=(), request_headers=(), content_type='text/html', status=200, root_path=''):
        messages=[]
        raw=list(headers)
        if content_type is not None:raw.append((b'content-type',content_type.encode()))
        async def app(scope,receive,send):
            await send({'type':'http.response.start','status':status,'headers':raw})
            await send({'type':'http.response.body','body':b'first','more_body':True})
            await send({'type':'http.response.body','body':b'last','more_body':False})
        async def send(message):messages.append(message)
        async def receive():return {'type':'http.request','body':b''}
        asyncio.run(middleware_class()(app)({'type':'http','path':path,'root_path':root_path,'headers':list(request_headers)},receive,send))
        return messages

    def test_unauthorized_body_status_and_cookie_auth_unchanged(self):
        with TestClient(application()) as client:
            for headers in [{}, {'Cookie':'token=synthetic-fixture-key'}, {'Authorization':'Bearer wrong'}]:
                r=client.post('/key/generate',headers=headers)
                self.assertEqual(r.status_code,401)
                self.assertEqual(r.json(),{'error':'unauthorized'})
                self.assertEqual(r.headers['cache-control'],'no-store')

    def test_login_redirect_cookie_and_cdn_cache(self):
        cookies=[(b'set-cookie',b'token=synthetic; Secure; SameSite=Lax; Path=/'),(b'set-cookie',b'nonce=synthetic; HttpOnly; Secure')]
        sent=self.respond('/v2/login',headers=cookies+[(b'location',b'/ui/'),(b'cdn-cache-control',b'public, s-maxage=86400')],content_type=None,status=303)
        h=sent[0]['headers'];self.assertEqual(sent[0]['status'],303)
        self.assertEqual([x for x in h if x[0]==b'set-cookie'],cookies)
        self.assertIn((b'location',b'/ui/'),h)
        self.assertIn((b'cache-control',b'no-store'),h)
        self.assertIn((b'cdn-cache-control',b'no-store'),h)

    def test_anonymous_login_errors_and_credential_route_boundaries(self):
        for path in ['/key','/key/info','/credentials/x','/user/new','/invitation/new','/login','/v2/login','/v3/login','/sso/callback','/token','/authorize']:
            with self.subTest(path=path):
                self.assertIn((b'cache-control',b'no-store'),self.respond(path,content_type='application/json',status=401)[0]['headers'])
        for path in ['/keyboard','/credentials-lookalike','/tokenizer']:
            self.assertNotIn((b'cache-control',b'no-store'),self.respond(path,content_type='application/json')[0]['headers'])

    def test_ingress_root_and_already_stripped_path(self):
        for path in ['/prefix/key/list','/key/list']:
            self.assertIn((b'cache-control',b'no-store'),self.respond(path,root_path='/prefix')[0]['headers'])

    def test_unknown_session_route_setting_cookie_is_no_store(self):
        self.assertIn((b'cache-control',b'no-store'),self.respond('/future-session',headers=[(b'set-cookie',b'token=synthetic')])[0]['headers'])

    def test_authenticated_json_no_store_without_changing_stream(self):
        sent=self.respond('/model/info',request_headers=[(b'authorization',b'Bearer fixture')],content_type='application/json')
        self.assertIn((b'cache-control',b'no-store'),sent[0]['headers'])
        self.assertEqual(sent[1:], [{'type':'http.response.body','body':b'first','more_body':True},{'type':'http.response.body','body':b'last','more_body':False}])

    def test_public_next_assets_remain_cacheable(self):
        sent=self.respond('/ui/_next/static/chunks/app.js',headers=[(b'cache-control',b'public, max-age=31536000, immutable')],content_type='application/javascript')
        self.assertIn((b'cache-control',b'public, max-age=31536000, immutable'),sent[0]['headers'])
        self.assertNotIn(b"object-src",b';'.join(v for k,v in sent[0]['headers'] if k==b'content-security-policy'))

    def test_existing_nonce_framing_and_referrer_policies_preserved(self):
        policy=b"default-src 'self'; script-src 'nonce-fixture'; frame-ancestors 'self'; object-src 'self'"
        sent=self.respond('/woow/chatgpt',headers=[(b'content-security-policy',policy),(b'x-frame-options',b'SAMEORIGIN'),(b'referrer-policy',b'same-origin')])
        h=sent[0]['headers'];self.assertIn((b'content-security-policy',policy),h)
        self.assertIn((b'content-security-policy',b"base-uri 'none'"),h)
        self.assertIn((b'x-frame-options',b'SAMEORIGIN'),h)
        self.assertIn((b'referrer-policy',b'same-origin'),h)
        self.assertNotIn((b'content-security-policy',b"object-src 'none'; base-uri 'none'"),h)

    def test_multiple_enforcing_policies_unchanged_when_directives_defined(self):
        policies=[(b'content-security-policy',b"script-src 'nonce-fixture'; object-src 'none'"),(b'content-security-policy',b"base-uri 'self'; frame-ancestors 'self'")]
        sent=self.respond(headers=policies)
        self.assertEqual([x for x in sent[0]['headers'] if x[0]==b'content-security-policy'],policies)

    def test_report_only_does_not_prevent_enforcing_defaults(self):
        sent=self.respond(headers=[(b'content-security-policy-report-only',b"object-src 'none'; base-uri 'none'")])
        self.assertIn((b'content-security-policy',b"object-src 'none'; base-uri 'none'"),sent[0]['headers'])

    def test_error_statuses_bodies_and_headers_preserved(self):
        for status in [401,403,404,429,500]:
            sent=self.respond('/key/info',status=status,headers=[(b'www-authenticate',b'Bearer'),(b'retry-after',b'60')],content_type='application/json')
            self.assertEqual(sent[0]['status'],status)
            self.assertIn((b'www-authenticate',b'Bearer'),sent[0]['headers'])
            self.assertIn((b'retry-after',b'60'),sent[0]['headers'])
            self.assertIn((b'cache-control',b'no-store'),sent[0]['headers'])
            self.assertEqual(sent[1]['body']+sent[2]['body'],b'firstlast')

    def test_existing_hsts_and_opt_in_contract_preserved(self):
        with patch.dict(os.environ,{'LITELLM_ENABLE_HSTS':'false'}):
            self.assertFalse(any(k==b'strict-transport-security' for k,v in self.respond()[0]['headers']))
        with patch.dict(os.environ,{'LITELLM_ENABLE_HSTS':'true'}):
            self.assertIn((b'strict-transport-security',b'max-age=31536000; includeSubDomains'),self.respond()[0]['headers'])
            self.assertIn((b'strict-transport-security',b'max-age=123'),self.respond(headers=[(b'strict-transport-security',b'max-age=123')])[0]['headers'])

    def test_websocket_and_lifespan_bypass_http_header_logic(self):
        for kind in ['websocket','lifespan']:
            seen=[]
            async def app(scope,receive,send):seen.append(scope['type'])
            async def receive():return {}
            async def send(message):raise AssertionError('middleware must not send on its own')
            asyncio.run(middleware_class()(app)({'type':kind},receive,send))
            self.assertEqual(seen,[kind])


if __name__ == '__main__':
    unittest.main(verbosity=2)
