"""Home Assistant Ingress stand-in (Core + Supervisor in one hop) for the smoke test.

Follows homeassistant/components/hassio/ingress.py and supervisor/api/ingress.py:
  * only /api/hassio_ingress/<token>/<path> of the configured token; anything else is a 404;
  * the prefix is stripped and the request goes to the add-on's ingress port;
  * request headers are copied except Content-Length/-Encoding, Transfer-Encoding,
    Accept-Encoding and the WebSocket handshake ones; Core adds X-Hass-Source, X-Ingress-Path,
    X-Forwarded-For (+ the browser address), X-Forwarded-Host and X-Forwarded-Proto (kept when the
    client already sent them); the Supervisor appends Core's address (172.30.32.1) to
    X-Forwarded-For and adds X-Remote-User-*;
  * response headers are copied except Transfer-Encoding, Content-Length, Content-Type and
    Content-Encoding (Content-Type is set from the upstream's), Set-Cookie kept as is;
  * /__ha_panel.html?src=… is the HA frontend's panel: an iframe of the same origin.

This container must have the Supervisor's address 172.30.32.2 (the add-on's nginx only accepts it).
    python ha_ingress_emulator.py <listen port> <add-on base URL, e.g. http://172.30.33.10:8099> <token>
"""

import asyncio
import sys

import aiohttp
from aiohttp import hdrs, web
from multidict import CIMultiDict

PORT = int(sys.argv[1])
ADDON = sys.argv[2].rstrip("/")
TOKEN = sys.argv[3]
CORE_ADDRESS = "172.30.32.1"

DROP_REQUEST = {
    h.lower()
    for h in (
        hdrs.CONTENT_LENGTH, hdrs.CONTENT_ENCODING, hdrs.TRANSFER_ENCODING, hdrs.ACCEPT_ENCODING,
        hdrs.SEC_WEBSOCKET_EXTENSIONS, hdrs.SEC_WEBSOCKET_PROTOCOL, hdrs.SEC_WEBSOCKET_VERSION, hdrs.SEC_WEBSOCKET_KEY,
        "X-Hass-Source", "X-Supervisor-Token", "X-Hassio-Key",
    )
}
DROP_RESPONSE = {h.lower() for h in (hdrs.TRANSFER_ENCODING, hdrs.CONTENT_LENGTH, hdrs.CONTENT_TYPE, hdrs.CONTENT_ENCODING)}

PANEL = """<!doctype html><html><head><title>HA panel</title></head><body style="margin:0">
<iframe id="f" title="addon" style="width:100vw;height:100vh;border:0"></iframe>
<script>document.getElementById("f").src=new URLSearchParams(location.search).get("src")</script></body></html>"""


def request_headers(request: web.Request) -> CIMultiDict:
    headers = CIMultiDict((k, v) for k, v in request.headers.items() if k.lower() not in DROP_REQUEST)
    headers["X-Hass-Source"] = "core.ingress"
    headers["X-Ingress-Path"] = f"/api/hassio_ingress/{TOKEN}"
    peer = request.transport.get_extra_info("peername")[0] if request.transport else "127.0.0.1"
    forward_for = request.headers.get(hdrs.X_FORWARDED_FOR)
    chain = f"{forward_for}, {peer}" if forward_for else peer
    headers[hdrs.X_FORWARDED_FOR] = f"{chain}, {CORE_ADDRESS}"  # Supervisor appends Core
    headers[hdrs.X_FORWARDED_HOST] = request.headers.get(hdrs.X_FORWARDED_HOST) or request.host
    headers[hdrs.X_FORWARDED_PROTO] = request.headers.get(hdrs.X_FORWARDED_PROTO) or request.scheme
    headers["X-Remote-User-Id"] = "test-user-id"
    headers["X-Remote-User-Name"] = "test"
    headers["X-Remote-User-Display-Name"] = "Test"
    return headers


async def _ws_forward(src, dst):
    try:
        async for msg in src:
            if msg.type is aiohttp.WSMsgType.TEXT:
                await dst.send_str(msg.data)
            elif msg.type is aiohttp.WSMsgType.BINARY:
                await dst.send_bytes(msg.data)
    except RuntimeError:
        pass


async def ingress(request: web.Request):
    if request.match_info["token"] != TOKEN:
        return web.Response(status=404, text="HA Core 404\n")
    url = f"{ADDON}/{request.match_info['path']}"
    if request.query_string:
        url += "?" + request.query_string
    headers = request_headers(request)
    session: aiohttp.ClientSession = request.app["session"]
    if request.headers.get(hdrs.UPGRADE, "").lower() == "websocket":
        server = web.WebSocketResponse()
        await server.prepare(request)
        async with session.ws_connect(url, headers=headers) as client:
            await asyncio.wait(
                [asyncio.create_task(_ws_forward(server, client)), asyncio.create_task(_ws_forward(client, server))],
                return_when=asyncio.FIRST_COMPLETED,
            )
        return server
    body = await request.read()
    async with session.request(request.method, url, headers=headers, data=body or None,
                               allow_redirects=False, auto_decompress=False) as upstream:
        out = CIMultiDict((k, v) for k, v in upstream.headers.items() if k.lower() not in DROP_RESPONSE)
        response = web.StreamResponse(status=upstream.status, headers=out)
        if upstream.headers.get(hdrs.CONTENT_TYPE):
            response.headers[hdrs.CONTENT_TYPE] = upstream.headers[hdrs.CONTENT_TYPE]
        await response.prepare(request)
        async for chunk in upstream.content.iter_chunked(65536):
            await response.write(chunk)
        await response.write_eof()
        return response


async def panel(request):
    return web.Response(text=PANEL, content_type="text/html")


async def not_found(request):
    return web.Response(status=404, text="HA Core 404\n")


async def _session(app):
    app["session"] = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar(), timeout=aiohttp.ClientTimeout(total=None))
    yield
    await app["session"].close()


app = web.Application(client_max_size=0)
app.cleanup_ctx.append(_session)
app.router.add_get("/__ha_panel.html", panel)
app.router.add_route("*", r"/api/hassio_ingress/{token:[A-Za-z0-9_-]{16,128}}/{path:.*}", ingress)
app.router.add_route("*", "/{tail:.*}", not_found)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None)
