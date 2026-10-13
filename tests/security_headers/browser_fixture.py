"""Loopback-only real browser checks, including pinned LiteLLM Next UI assets."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import threading
import time
import urllib.parse

from playwright.sync_api import sync_playwright
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
import uvicorn
from test_headers import middleware_class


def serve(app):
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            return server, thread, sock, port
        time.sleep(.025)
    raise RuntimeError('local fixture server did not start')


def main():
    observed = {'object_requests': 0, 'referrer_present': None}
    async def echo(request):
        observed['referrer_present'] = bool(request.headers.get('referer'))
        return JSONResponse({'ok': True}, headers={'Access-Control-Allow-Origin': '*'})
    es, et, eso, ep = serve(Starlette(routes=[Route('/echo', echo)]))
    async def fixture(request):
        return HTMLResponse(f'''<!doctype html><html><head>
        <base href="http://127.0.0.1:{ep}/injected/">
        <script>window.inlineScriptRan=true</script></head><body>
        <h1>Local security fixture</h1><button onclick="this.textContent='clicked'">click</button>
        <object data="http://127.0.0.1:{request.url.port}/probe-object" type="text/html"></object>
        </body></html>''')
    async def obj(request):
        observed['object_requests'] += 1
        return HTMLResponse('<p>fixture object</p>')
    async def defaults(request):
        path=request.url.path
        if path=='/.well-known/litellm-ui-config':
            return JSONResponse({'server_root_path': '', 'proxy_base_url': '', 'ui_base_path': '/ui'})
        if path=='/get_sso_settings':return JSONResponse({'sso_enabled': False})
        return JSONResponse({})
    assets=Path(os.environ['LITELLM_UI_ASSETS']).resolve()
    assert (assets/'index.html').exists()
    routes=[Route('/fixture',fixture),Route('/probe-object',obj),
            Mount('/litellm-asset-prefix',StaticFiles(directory=assets,html=True)),
            Mount('/ui',StaticFiles(directory=assets,html=True)),
            Route('/{path:path}',defaults,methods=['GET','POST'])]
    server,thread,sock,port=serve(middleware_class()(Starlette(routes=routes)))
    origin=f'http://127.0.0.1:{port}'
    result={'fixture':'localhost only, no live credentials or product objects','observed':observed,'external_requests_blocked':0,'checks':{}}
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=os.environ['CHROMIUM_EXECUTABLE'],headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
            context=browser.new_context()
            def guard(route):
                u=urllib.parse.urlsplit(route.request.url)
                if u.hostname!='127.0.0.1' or u.port not in [port,ep]:
                    result['external_requests_blocked']+=1;route.abort();return
                route.continue_()
            context.route('**/*',guard)
            page=context.new_page();page.set_default_timeout(15000)
            response=page.goto(origin+'/fixture',wait_until='networkidle')
            result['fixture_response_headers']={k:v for k,v in response.all_headers().items() if k in ['content-security-policy','referrer-policy','x-frame-options']}
            result['checks']['object_embedding_blocked']=observed['object_requests']==0
            result['checks']['base_injection_blocked']=page.evaluate('document.baseURI')==origin+'/fixture'
            result['checks']['inline_script_preserved']=page.evaluate('window.inlineScriptRan===true')
            page.get_by_role('button',name='click',exact=True).click()
            result['checks']['event_handlers_preserved']=page.get_by_role('button',name='clicked',exact=True).count()==1
            page.evaluate('(url)=>fetch(url)',f'http://127.0.0.1:{ep}/echo')
            result['checks']['cross_origin_referrer_absent']=observed['referrer_present'] is False
            errors=[];page.on('pageerror',lambda e:errors.append(hashlib.sha256(str(e).encode()).hexdigest()))
            res=page.goto(origin+'/ui/',wait_until='domcontentloaded');page.wait_for_timeout(3000)
            user=page.locator('input[placeholder="Enter your username"]')
            password=page.locator('input[placeholder="Enter your password"]')
            result['next_ui']={'http':res.status,'username_input':user.count(),'password_input':password.count(),'script_count':page.locator('script[src]').count(),'page_error_hashes':errors,'body_text_preview':page.locator('body').inner_text()[:600],'asset_index_sha256':hashlib.sha256((assets/'index.html').read_bytes()).hexdigest()}
            result['checks']['next_login_hydrated']=res.status==200 and user.count()==1 and password.count()==1
            if user.count()==1:
                user.fill('synthetic-fixture-user');result['checks']['next_input_interactive']=user.input_value()=='synthetic-fixture-user'
            context.close();browser.close()
    finally:
        for s,t,so in [(server,thread,sock),(es,et,eso)]:
            s.should_exit=True;t.join(timeout=5);so.close()
        result['cleanup']={'servers_stopped':not thread.is_alive() and not et.is_alive(),'temporary_browser_context_closed':True}
    result['status']='PASS' if all(result['checks'].values()) else 'FAIL'
    print(json.dumps(result,indent=2))
    return result


if __name__=='__main__':
    result=main()
    Path(os.environ['BROWSER_RECEIPT']).write_text(json.dumps(result,indent=2)+'\n')
    raise SystemExit(0 if result['status']=='PASS' else 1)
