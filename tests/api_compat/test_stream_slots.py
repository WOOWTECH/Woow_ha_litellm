"""API-09 contracts (apply_stream_slot_release.py): a streamed request gives its max_parallel_requests
slot back, with an SSE keepalive interval as well as without one; nothing leaves the process.

The real proxy app runs with the v3 parallel request limiter registered the way proxy startup does it
(the ``parallel_request_limiter`` proxy hook, also in ``litellm.callbacks``) and a virtual key with
``max_parallel_requests=1``. Requests are driven at the ASGI interface the way uvicorn does (ASGI spec
2.3: the client vanishing turns into ``http.disconnect``, later writes are dropped), each in a task of
its own, like the server's request tasks. The provider boundary is httpx's transport; the fake
api_base host picks its behaviour: ``quick`` answers at once, ``hold`` stops after its first event
(``holdlate`` after the second: the /v1/messages adapter turns the first two chat chunks into its first
events), ``slow`` and ``slowhold`` withhold their response (the first token) longer than the keepalive
interval, ``silent`` until the client is gone. Whether the slot is free is asked the way a client
would: a second, non-streaming request with the same key gets 200, or 429 while the slot is taken.

LiteLLM 1.104.0 creates the limiter's per-request stash inside the task that
``open_sse_before_first_byte`` forks when a keepalive interval is set, so the stream's end and the
disconnect cleanup never find the slot (it stays taken for PARALLEL_REQUEST_SLOT_TTL_SECONDS, 3600 s),
and a request cancelled before its first token releases nothing at all. Guards: a request still open
keeps its slot (the limit is not loosened), and without a keepalive interval, as well as for
non-streaming requests, the slot is released as before. Every key and response below is synthetic.
"""
import asyncio
import json

import httpx
import litellm
import pytest
from litellm.caching.dual_cache import DualCache
from litellm.proxy import proxy_server as server
from litellm.proxy._types import LiteLLM_UserTable, LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.common_utils.user_api_key_cache import UserApiKeyCache
from litellm.proxy.hooks.parallel_request_limiter_v3 import _PROXY_MaxParallelRequestsHandler_v3
from litellm.proxy.hooks.proxy_track_cost_callback import _ProxyDBLogger
from litellm.proxy.utils import InternalUsageCache, hash_token

MASTER = 'sk-api09-master-fixture'
LIMITED = 'sk-api09-one-parallel-request-fixture'
KEEPALIVE = 1.0  # seconds; the PaaS chart uses 15, a shorter one keeps these contracts quick
SLOW_TTFT = 2.5  # > KEEPALIVE: the response opens with ': ping' before the first event
HOLD = 20.0  # an upstream that holds stays open well past every deadline below
RELEASE_WITHIN = 2.0
WAIT = 10.0
M = [{'role': 'user', 'content': 'offline'}]
USAGE = {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}
ROUTES = {
    'chat': ('/v1/chat/completions', {'messages': M}),
    'responses': ('/v1/responses', {'input': 'offline'}),
    'messages': ('/v1/messages', {'messages': M, 'max_tokens': 32}),
}


def _chunk(delta, finish=None, **extra):
    return {'id': 'c1', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'woowfake/model',
            'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}], **extra}


CHAT_FRAMES = [b'data: ' + json.dumps(c).encode() + b'\n\n' for c in (
    _chunk({'role': 'assistant', 'content': 'O'}), _chunk({'content': 'K'}, 'stop'),
    {'id': 'c1', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'woowfake/model', 'choices': [], 'usage': USAGE},
)] + [b'data: [DONE]\n\n']
COMPLETION = {'id': 'c1', 'object': 'chat.completion', 'created': 1, 'model': 'woowfake/model', 'usage': USAGE,
              'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'OK'}, 'finish_reason': 'stop'}]}


def _response(status):
    done = status == 'completed'
    return {'id': 'resp_api09', 'object': 'response', 'created_at': 1, 'status': status, 'model': 'woowfake/model',
            'output': [{'type': 'message', 'id': 'msg_api09', 'status': 'completed', 'role': 'assistant',
                        'content': [{'type': 'output_text', 'text': 'OK', 'annotations': []}]}] if done else [],
            'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2, 'input_tokens_details': {'cached_tokens': 0},
                      'output_tokens_details': {'reasoning_tokens': 0}} if done else None}


RESPONSES_FRAMES = [f'event: {e["type"]}\ndata: {json.dumps(e)}\n\n'.encode() for e in (
    {'type': 'response.created', 'sequence_number': 0, 'response': _response('in_progress')},
    {'type': 'response.output_text.delta', 'sequence_number': 1, 'item_id': 'msg_api09', 'output_index': 0,
     'content_index': 0, 'delta': 'OK'},
    {'type': 'response.completed', 'sequence_number': 2, 'response': _response('completed')},
)]


HOLD_AFTER = {'hold': 1, 'slowhold': 1, 'holdlate': 2}  # frames sent before the upstream stalls


async def _frames(frames, hold_after):
    for index, frame in enumerate(frames, 1):
        yield frame
        if index == hold_after:
            await asyncio.sleep(HOLD)


class Upstream:
    def __init__(self):
        self.calls = []
        self.cancelled = []

    async def handle(self, request):
        scenario = request.url.host.split('.')[0]
        body = json.loads(request.content or b'{}')
        self.calls.append(scenario)
        delay = {'slow': SLOW_TTFT, 'slowhold': SLOW_TTFT, 'silent': HOLD, 'busy': SLOW_TTFT}.get(scenario, 0)
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self.cancelled.append(scenario)
            raise
        if not body.get('stream'):
            return httpx.Response(200, request=request, json=COMPLETION)
        frames = RESPONSES_FRAMES if request.url.path.endswith('/responses') else CHAT_FRAMES
        return httpx.Response(200, request=request, headers={'content-type': 'text/event-stream'},
                              content=_frames(frames, HOLD_AFTER.get(scenario, 0)))


def _deployment(name):
    return {'model_name': name, 'model_info': {'id': 'dep-' + name},
            'litellm_params': {'model': 'openai/woowfake/model', 'api_key': 'sk-fx-provider',
                               'api_base': f'https://{name}.upstream.invalid/v1'}}


def _is_ping(data):
    return data.lstrip().startswith(b':')


def _is_event(data):
    return b'data:' in data and b'"type": "ping"' not in data and b'"type":"ping"' not in data


class Client:
    """One request at the ASGI interface, the way uvicorn serves it (spec 2.3)."""

    def __init__(self, path, body):
        self.path, self.body = path, json.dumps(body).encode()
        self.status, self.frames = None, []
        self.pinged, self.got_event, self.finished, self.gone = (asyncio.Event() for _ in range(4))

    async def run(self, app):
        scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.3'}, 'http_version': '1.1',
                 'method': 'POST', 'scheme': 'http', 'path': self.path, 'raw_path': self.path.encode(),
                 'query_string': b'', 'root_path': '', 'client': ('127.0.0.1', 50123), 'server': ('offline.invalid', 80),
                 'headers': [(b'host', b'offline.invalid'), (b'authorization', b'Bearer ' + LIMITED.encode()),
                             (b'content-type', b'application/json')]}
        delivered = False

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': self.body, 'more_body': False}
            await self.gone.wait()
            return {'type': 'http.disconnect'}

        async def send(message):
            if self.gone.is_set():
                return  # the connection is closed: uvicorn drops what is still written
            if message['type'] == 'http.response.start':
                self.status = message['status']
            elif message['type'] == 'http.response.body':
                data = bytes(message.get('body', b''))
                if data:
                    self.frames.append(data)
                    if _is_ping(data):
                        self.pinged.set()
                    elif _is_event(data):
                        self.got_event.set()
                if not message.get('more_body', False):
                    self.finished.set()

        await app(scope, receive, send)

    def text(self):
        return b''.join(self.frames).decode()


class Lab:
    def __init__(self, upstream):
        self.upstream = upstream
        self.tasks = []

    def start(self, route, model, stream=True):
        path, body = ROUTES[route]
        client = Client(path, {**body, 'model': model, 'stream': stream})
        self.tasks.append(asyncio.create_task(client.run(server.app)))  # a fresh context, like a request task
        return client

    async def probe(self):
        async def send():
            transport = httpx.ASGITransport(app=server.app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url='http://offline.invalid', timeout=WAIT) as client:
                result = await client.post('/v1/chat/completions', headers={'Authorization': 'Bearer ' + LIMITED},
                                           json={'model': 'quick', 'messages': M})
                return result.status_code
        status = await asyncio.create_task(send())
        assert status in (200, 429), status
        return status

    async def released_within(self, seconds=RELEASE_WITHIN):
        """Seconds until the key's slot is free again (a probe is not 429), or None after `seconds`."""
        loop = asyncio.get_running_loop()
        start = loop.time()
        while True:
            if await self.probe() == 200:
                return loop.time() - start
            if loop.time() - start >= seconds:
                return None
            await asyncio.sleep(0.05)

    async def wait(self, event, what):
        try:
            await asyncio.wait_for(event.wait(), WAIT)
        except TimeoutError:
            raise AssertionError('timed out waiting for ' + what) from None

    async def settle(self):
        """Every request has ended (the app returned) after its client left or read to the end."""
        done, pending = await asyncio.wait(self.tasks, timeout=WAIT)
        assert not pending, 'request still running'
        for task in done:
            task.result()


def run(scenario, upstream):
    async def main():
        lab = Lab(upstream)
        try:
            return await scenario(lab)
        finally:
            for task in lab.tasks:
                task.cancel()
            await asyncio.gather(*lab.tasks, return_exceptions=True)
    return asyncio.run(main())


@pytest.fixture
def upstream(monkeypatch):
    monkeypatch.setattr(server, 'master_key', MASTER)
    monkeypatch.setattr(server, 'general_settings', {})
    monkeypatch.setattr(server, 'user_custom_auth', None)
    monkeypatch.setattr(server, 'user_model', None)
    cache = UserApiKeyCache()
    cache.set_cache(key='offline-user', value=LiteLLM_UserTable(user_id='offline-user', user_role='internal_user'),
                    model_type=LiteLLM_UserTable)
    cache.set_cache(key=hash_token(LIMITED), model_type=UserAPIKeyAuth, value=UserAPIKeyAuth(
        api_key=hash_token(LIMITED), token=hash_token(LIMITED), user_role=LitellmUserRoles.INTERNAL_USER, models=[],
        user_id='offline-user', metadata={}, spend=0.0, key_alias='api09-one-parallel', max_parallel_requests=1))
    monkeypatch.setattr(server, 'user_api_key_cache', cache)
    monkeypatch.setattr(server, 'prisma_client', object())  # spend rows go to update_database below

    async def floor(counter_key, **kwargs):
        return 0.0
    monkeypatch.setattr(server, '_authoritative_floor_spend', floor)
    server.spend_counter_cache.in_memory_cache.flush_cache()
    # LiteLLM's process-wide callback lists keep what earlier contract files registered (other limiter
    # instances included, which would clear this stash's slot without releasing it here) and stop
    # taking entries at MAX_CALLBACKS: start from empty lists, restored after the test.
    for name in ('_async_success_callback', 'success_callback', '_async_failure_callback', 'failure_callback'):
        monkeypatch.setattr(litellm, name, [])
    names = ('quick', 'hold', 'holdlate', 'slow', 'slowhold', 'silent', 'busy')
    router = litellm.Router(model_list=[_deployment(name) for name in names], num_retries=0)
    monkeypatch.setattr(server, 'llm_router', router)
    monkeypatch.setattr(server, 'llm_model_list', router.model_list)
    fake = Upstream()
    monkeypatch.setattr(litellm, 'disable_aiohttp_transport', True)
    monkeypatch.setattr(litellm, 'drop_params', True)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', lambda transport, request: fake.handle(request))

    async def spend_row(**kwargs):
        return None
    monkeypatch.setattr(server.proxy_logging_obj.db_spend_update_writer, 'update_database', spend_row)
    # Registered the way ProxyLogging._add_proxy_hooks does at startup: one limiter instance is both the
    # pre-call/success/failure callback and the hook the disconnect cleanup releases through.
    limiter = _PROXY_MaxParallelRequestsHandler_v3(InternalUsageCache(dual_cache=DualCache()))
    monkeypatch.setitem(server.proxy_logging_obj.proxy_hook_mapping, 'parallel_request_limiter', limiter)
    monkeypatch.setattr(litellm, 'callbacks', [_ProxyDBLogger(), limiter])
    monkeypatch.setattr(litellm, 'sse_keepalive_ping_interval_seconds', KEEPALIVE)
    return fake


def keepalive_off(monkeypatch):
    monkeypatch.setattr(litellm, 'sse_keepalive_ping_interval_seconds', None)


def _ended(route, client):
    text = client.text()
    return {'chat': 'data: [DONE]', 'responses': 'response.completed', 'messages': 'message_stop'}[route] in text


async def _read_to_the_end(lab, route, model='quick'):
    client = lab.start(route, model)
    await lab.wait(client.finished, 'the end of the stream')
    await lab.settle()
    assert client.status == 200 and _ended(route, client), client.text()[-300:]
    return client, await lab.released_within()


async def _leave_after_first_event(lab, route, model=None):
    client = lab.start(route, model or ('holdlate' if route == 'messages' else 'hold'))
    await lab.wait(client.got_event, 'the first event')
    assert await lab.probe() == 429, 'the open stream must hold its slot'
    client.gone.set()
    released = await lab.released_within()
    await lab.settle()
    assert client.status == 200 and not _ended(route, client)
    return client, released


# --- with a keepalive interval: the fix -------------------------------------------------------------


@pytest.mark.parametrize('route', ROUTES)
def test_slot_is_free_right_after_a_stream_read_to_the_end(upstream, route):
    """(a)/(d) The stream's own success callback releases the slot; before the fix it stayed taken."""
    client, released = run(lambda lab: _read_to_the_end(lab, route), upstream)
    assert b': ping' not in b''.join(client.frames)  # answered inside one interval: the fast path
    assert released is not None, f'{route}: slot still taken {RELEASE_WITHIN}s after the stream ended'


def test_slot_is_free_after_a_slow_first_token_stream_read_to_the_end(upstream):
    """The keepalive opened the response (': ping' first); the relayed stream releases at its end."""
    client, released = run(lambda lab: _read_to_the_end(lab, 'chat', model='slow'), upstream)
    assert client.frames[0].startswith(b': ping')
    assert released is not None, f'slot still taken {RELEASE_WITHIN}s after the stream ended'


@pytest.mark.parametrize('route', ROUTES)
def test_slot_is_released_when_the_client_leaves_after_the_first_event(upstream, route):
    """(b)/(d) API-09: the disconnect cleanup releases the slot within 2 s."""
    _, released = run(lambda lab: _leave_after_first_event(lab, route), upstream)
    assert released is not None, f'{route}: slot still taken {RELEASE_WITHIN}s after the client left'


def test_slot_is_released_when_the_client_leaves_a_slow_first_token_stream_after_its_first_event(upstream):
    client, released = run(lambda lab: _leave_after_first_event(lab, 'chat', model='slowhold'), upstream)
    assert client.frames[0].startswith(b': ping')
    assert released is not None, f'slot still taken {RELEASE_WITHIN}s after the client left'


@pytest.mark.parametrize('route', ROUTES)
def test_slot_is_released_when_the_client_leaves_after_only_a_ping(upstream, route):
    """(c) The first token is slower than the keepalive and the client leaves after ': ping': the request
    is cancelled while the upstream call is pending, which no logging callback sees."""
    async def scenario(lab):
        client = lab.start(route, 'silent')
        await lab.wait(client.pinged, 'the first keepalive ping')
        assert not client.got_event.is_set()
        assert await lab.probe() == 429, 'the open stream must hold its slot'
        client.gone.set()
        released = await lab.released_within()
        await lab.settle()
        return client, released

    client, released = run(scenario, upstream)
    assert client.status == 200 and all(_is_ping(frame) for frame in client.frames)
    assert upstream.cancelled == ['silent']  # the pending upstream call was abandoned, not answered
    assert released is not None, f'{route}: slot still taken {RELEASE_WITHIN}s after the client left'


# --- guards: the limit still holds, nothing changes without a keepalive or a stream ----------------


@pytest.mark.parametrize('route,model,phase', [
    ('chat', 'hold', 'event'), ('chat', 'silent', 'ping'), ('chat', 'slowhold', 'event'),
    ('responses', 'hold', 'event'), ('responses', 'silent', 'ping'),
    ('messages', 'holdlate', 'event'), ('messages', 'silent', 'ping'),
])
def test_an_open_stream_keeps_its_slot(upstream, route, model, phase):
    """(e) While the stream is open, a second request with the key is refused (max_parallel_requests=1)."""
    async def scenario(lab):
        client = lab.start(route, model)
        await lab.wait(client.got_event if phase == 'event' else client.pinged, 'the first ' + phase)
        statuses = [await lab.probe() for _ in range(3)]
        client.gone.set()
        await lab.settle()
        return statuses

    assert run(scenario, upstream) == [429, 429, 429]


@pytest.mark.parametrize('route', ROUTES)
@pytest.mark.parametrize('how', ['read to the end', 'leave after the first event'])
def test_without_keepalive_slots_are_released_as_before(upstream, monkeypatch, route, how):
    """(f) No keepalive interval (the HA add-on's default): no ': ping', and the slot comes back."""
    keepalive_off(monkeypatch)
    flow = _read_to_the_end if how == 'read to the end' else _leave_after_first_event
    client, released = run(lambda lab: flow(lab, route), upstream)
    assert b': ping' not in b''.join(client.frames)
    assert released is not None, f'{route}: slot still taken {RELEASE_WITHIN}s ({how})'


def test_without_keepalive_a_slow_first_token_stream_releases_as_before(upstream, monkeypatch):
    keepalive_off(monkeypatch)
    client, released = run(lambda lab: _leave_after_first_event(lab, 'chat', model='slowhold'), upstream)
    assert b': ping' not in b''.join(client.frames)
    assert released is not None


def test_non_streaming_requests_hold_and_release_their_slot_as_before(upstream):
    """(g) The keepalive is for streams only: a non-streaming request holds its slot while it runs and
    frees it when it answers, with the interval set."""
    async def scenario(lab):
        busy = lab.start('chat', 'busy', stream=False)
        await asyncio.sleep(0.5)
        during = [await lab.probe() for _ in range(2)]
        await lab.wait(busy.finished, 'the slow answer')
        await lab.settle()
        assert busy.status == 200 and json.loads(busy.text())['choices'][0]['message']['content'] == 'OK'
        return during, await lab.released_within()

    during, released = run(scenario, upstream)
    assert during == [429, 429]
    assert released is not None


def test_back_to_back_non_streaming_requests_are_not_refused(upstream):
    async def scenario(lab):
        return [await lab.probe() for _ in range(5)]

    assert run(scenario, upstream) == [200] * 5
