"""M4 batch 1 contracts (apply_m4_batch1.py) on the real proxy app; nothing leaves the process.

The provider boundary is httpx's transport; the fake api_base host picks the answer: ``echo*``
upstreams put the Authorization header they received (the deployment's own key) into their error
body, ``dead`` cannot be connected to, everything else answers normally. Spend logs go to a
recording DB writer, auth uses the real cache-backed path. Every credential below is fake.

R03 no route hands a client the deployment key, a JWT/access token, an internal host or raw
    connection detail; R04 a proxy_admin_viewer cannot run inference; R11 Responses stream events
    carry the requested model group; R15 /health and /health/latest are masked for admins too and
    ChatGPT endpoints show an api_base; R01 new names with '%' or '/' are refused, stored ones are not.
"""
import asyncio
import json
import types
import httpx
import pytest
import litellm
from litellm.proxy import proxy_server as server
from litellm.proxy._types import LiteLLM_UserTable, LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.common_utils.user_api_key_cache import UserApiKeyCache
from litellm.proxy.hooks.proxy_track_cost_callback import _ProxyDBLogger
from litellm.proxy.utils import hash_token

MASTER = 'sk-m4-batch1-master-fixture'
VIRTUAL = 'sk-m4-batch1-virtual-fixture'
VIEWER = 'sk-m4-batch1-viewer-fixture'
PROVIDER_KEY = 'sk-fx-good-echoauth-7f3a91'
RAW_KEY = 'woowRAWsecretVALUE12345'
JWT = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ3b293LWZha2UifQ.c2lnbmF0dXJlLWZha2U'
INTERNAL = 'http://mock-upstream:18080/v1'
CONNECT_FAILURE = ("Cannot connect to host 127.0.0.1:9 ssl:<ssl.SSLContext object at 0x7f00deadbeef> "
                   "[Connect call failed ('127.0.0.1', 9)]")
LEAKS = (PROVIDER_KEY, RAW_KEY, JWT, 'mock-upstream', '127.0.0.1', 'SSLContext', 'Connect call failed')
VIEWER_DENIED = 'user not allowed to access this OpenAI routes'
M = [{'role': 'user', 'content': 'offline'}]
HOST = 'upstream.invalid'


def _sse(events):
    return ''.join(f'event: {e["type"]}\ndata: {json.dumps(e)}\n\n' for e in events).encode()


def _response(status='completed', model='woowfake/model'):
    return {'id': 'resp_offline', 'object': 'response', 'created_at': 1, 'status': status, 'model': model,
            'output': [] if status != 'completed' else [
                {'type': 'message', 'id': 'msg_offline', 'status': 'completed', 'role': 'assistant',
                 'content': [{'type': 'output_text', 'text': 'OK', 'annotations': []}]}],
            'usage': None if status != 'completed' else {
                'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2,
                'input_tokens_details': {'cached_tokens': 0}, 'output_tokens_details': {'reasoning_tokens': 0}}}


RESPONSES_STREAM = [
    {'type': 'response.created', 'sequence_number': 0, 'response': _response('in_progress')},
    {'type': 'response.in_progress', 'sequence_number': 1, 'response': _response('in_progress')},
    {'type': 'response.output_text.delta', 'sequence_number': 2, 'item_id': 'msg_offline', 'output_index': 0,
     'content_index': 0, 'delta': 'OK'},
    {'type': 'response.completed', 'sequence_number': 3, 'response': _response()},
]


class Upstream:
    def __init__(self):
        self.calls = []

    async def handle(self, transport, request):
        scenario = request.url.host.split('.')[0]
        body = json.loads(request.content or b'{}')
        self.calls.append({'scenario': scenario, 'path': request.url.path, 'body': body})
        auth = request.headers.get('authorization', '')
        if scenario == 'dead':
            raise httpx.ConnectError(CONNECT_FAILURE, request=request)
        if scenario.startswith('echo'):
            received = auth.removeprefix('Bearer ') if scenario == 'echoraw' else 'Authorization: ' + auth
            message = f'fx upstream error 401 received {received} (upstream {INTERNAL})'
            return httpx.Response(401, request=request, json={
                'error': {'message': message, 'type': 'invalid_request_error', 'param': None, 'code': 'fx_401'}})
        path = request.url.path
        if scenario == 'midecho' and body.get('stream'):
            chunk = {'id': 'c1', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'woowfake/model',
                     'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': 'Hel'}, 'finish_reason': None}]}
            error = {'error': {'message': f'stream reset, received {auth} at {INTERNAL}', 'type': 'server_error',
                               'code': 'fx_500'}}
            return httpx.Response(200, request=request, headers={'content-type': 'text/event-stream'},
                                  content=f'data: {json.dumps(chunk)}\n\ndata: {json.dumps(error)}\n\n'.encode())
        if path.endswith('/embeddings'):
            return httpx.Response(200, request=request, json={
                'object': 'list', 'model': 'woowfake/model', 'usage': {'prompt_tokens': 1, 'total_tokens': 1},
                'data': [{'object': 'embedding', 'index': 0, 'embedding': [0.1, 0.2]}]})
        if path.endswith('/responses'):
            if body.get('stream'):
                return httpx.Response(200, request=request, headers={'content-type': 'text/event-stream'},
                                      content=_sse(RESPONSES_STREAM))
            return httpx.Response(200, request=request, json=_response())
        usage = {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}
        if body.get('stream'):
            chunks = [{'id': 'c1', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'woowfake/model',
                       'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': 'OK'}, 'finish_reason': 'stop'}]},
                      {'id': 'c1', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'woowfake/model',
                       'choices': [], 'usage': usage}]
            content = ''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + 'data: [DONE]\n\n'
            return httpx.Response(200, request=request, headers={'content-type': 'text/event-stream'},
                                  content=content.encode())
        return httpx.Response(200, request=request, json={
            'id': 'c1', 'object': 'chat.completion', 'created': 1, 'model': 'woowfake/model', 'usage': usage,
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'OK'}, 'finish_reason': 'stop'}]})


def _deployment(name, host, api_key=PROVIDER_KEY, model='openai/woowfake/model', model_id=None):
    return {'model_name': name, 'litellm_params': {'model': model, 'api_key': api_key, 'api_base': f'https://{host}.{HOST}/v1'},
            'model_info': {'id': model_id or 'dep-' + name}}


MODEL_LIST = [
    _deployment('echo', 'echo'), _deployment('echoraw', 'echoraw', api_key=RAW_KEY),
    _deployment('echojwt', 'echojwt', api_key=JWT), _deployment('dead', 'dead'),
    _deployment('midecho', 'midecho'), _deployment('ok', 'ok'), _deployment('respgroup', 'ok'),
    _deployment('legacy%name', 'ok', model_id='dep-legacy'),
]


class FakePrisma:
    """Only what /health and /health/latest touch; any other database access fails loudly."""

    def __init__(self):
        self.saved = []
        self.rows = []

    async def save_health_check_result(self, **kwargs):
        self.saved.append(kwargs)

    async def get_all_latest_health_checks(self):
        return self.rows


class Gateway:
    def __init__(self, app, upstream, spend, prisma):
        self.app, self.upstream, self.spend, self.prisma = app, upstream, spend, prisma

    async def send(self, method, path, body=None, token=VIRTUAL, settle=0.3):
        transport = httpx.ASGITransport(app=self.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url='http://offline.invalid', timeout=60) as client:
            headers = {'Authorization': 'Bearer ' + token, 'content-type': 'application/json'}
            result = await client.request(method, path, headers=headers,
                                          content=json.dumps(body) if body is not None else None)
            await result.aread()
            if settle:
                await asyncio.sleep(settle)
            return result

    def request(self, method, path, body=None, token=VIRTUAL):
        result = asyncio.run(self.send(method, path, body, token))
        assert MASTER not in result.text and VIRTUAL not in result.text and VIEWER not in result.text
        return result


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.setattr(server, 'master_key', MASTER)
    monkeypatch.setattr(server, 'general_settings', {})
    monkeypatch.setattr(server, 'user_custom_auth', None)
    monkeypatch.setattr(server, 'user_model', None)
    monkeypatch.setattr(server, 'store_model_in_db', True)
    cache = UserApiKeyCache()
    for user_id, role in (('offline-user', 'internal_user'), ('viewer-user', 'proxy_admin_viewer')):
        cache.set_cache(key=user_id, value=LiteLLM_UserTable(user_id=user_id, user_role=role), model_type=LiteLLM_UserTable)
    for key, user_id, role in ((VIRTUAL, 'offline-user', LitellmUserRoles.INTERNAL_USER),
                               (VIEWER, 'viewer-user', LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY)):
        cache.set_cache(key=hash_token(key), model_type=UserAPIKeyAuth, value=UserAPIKeyAuth(
            api_key=hash_token(key), token=hash_token(key), user_role=role, models=[], user_id=user_id,
            metadata={}, spend=0.0, key_alias='m4-batch1-' + user_id))
    monkeypatch.setattr(server, 'user_api_key_cache', cache)
    prisma = FakePrisma()
    monkeypatch.setattr(server, 'prisma_client', prisma)

    async def floor(counter_key, **kwargs):
        return 0.0
    monkeypatch.setattr(server, '_authoritative_floor_spend', floor)
    server.spend_counter_cache.in_memory_cache.flush_cache()
    router = litellm.Router(model_list=[json.loads(json.dumps(d)) for d in MODEL_LIST], num_retries=0)
    monkeypatch.setattr(server, 'llm_router', router)
    monkeypatch.setattr(server, 'llm_model_list', router.model_list)
    upstream = Upstream()
    monkeypatch.setattr(litellm, 'disable_aiohttp_transport', True)
    monkeypatch.setattr(litellm, 'drop_params', True)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request',
                        lambda transport, request: upstream.handle(transport, request))
    spend = []

    async def update_database(**kwargs):
        spend.append(kwargs)
    monkeypatch.setattr(server.proxy_logging_obj.db_spend_update_writer, 'update_database', update_database)
    monkeypatch.setattr(litellm, 'callbacks', [_ProxyDBLogger()])
    yield Gateway(server.app, upstream, spend, prisma)
    server.spend_counter_cache.in_memory_cache.flush_cache()


def leaked(result):
    headers = '\n'.join(f'{k}: {v}' for k, v in result.headers.items())
    return [s for s in LEAKS if s in result.text or s in headers]


ROUTES = {
    'chat': ('/v1/chat/completions', lambda m: {'model': m, 'messages': M}),
    'chat-stream': ('/v1/chat/completions', lambda m: {'model': m, 'messages': M, 'stream': True}),
    'completions': ('/v1/completions', lambda m: {'model': m, 'prompt': 'offline'}),
    'completions-stream': ('/v1/completions', lambda m: {'model': m, 'prompt': 'offline', 'stream': True}),
    'embeddings': ('/v1/embeddings', lambda m: {'model': m, 'input': 'offline'}),
    'responses': ('/v1/responses', lambda m: {'model': m, 'input': 'offline'}),
    'responses-stream': ('/v1/responses', lambda m: {'model': m, 'input': 'offline', 'stream': True}),
    'messages': ('/v1/messages', lambda m: {'model': m, 'messages': M, 'max_tokens': 32}),
    'messages-stream': ('/v1/messages', lambda m: {'model': m, 'messages': M, 'max_tokens': 32, 'stream': True}),
}


# R03: one mask for every client-facing upstream error ----------------------------------------
@pytest.mark.parametrize('token', [MASTER, VIRTUAL], ids=['master', 'virtual'])
@pytest.mark.parametrize('scenario', ['echo', 'echoraw', 'echojwt'])
@pytest.mark.parametrize('route', list(ROUTES))
def test_upstream_echo_never_reaches_the_client(gateway, route, scenario, token):
    path, body = ROUTES[route]
    result = gateway.request('POST', path, body(scenario), token=token)
    assert gateway.upstream.calls, 'the provider must have been called'
    assert result.status_code >= 400 or 'error' in result.text
    assert leaked(result) == [], result.text[:600]
    assert 'fx upstream error 401' in result.text  # the provider's own message is still there


@pytest.mark.parametrize('route', ['chat-stream', 'completions-stream'])
def test_midstream_echo_is_masked_in_sse_error_frames(gateway, route):
    path, body = ROUTES[route]
    result = gateway.request('POST', path, body('midecho'))
    assert 'error' in result.text and 'stream reset' in result.text  # 200 SSE frame, or 500 before the first byte
    assert leaked(result) == [], result.text[:800]


@pytest.mark.parametrize('route', list(ROUTES))
def test_connection_failures_say_connection_error_without_address(gateway, route):
    path, body = ROUTES[route]
    result = gateway.request('POST', path, body('dead'))
    assert 'Connection error' in result.text, result.text[:600]
    assert leaked(result) == [], result.text[:600]


@pytest.mark.parametrize('exc', [
    lambda: __import__('litellm.llms.base_llm.chat.transformation', fromlist=['BaseLLMException']).BaseLLMException(
        status_code=401, message=f'received Authorization: Bearer {JWT} at {INTERNAL}'),
    lambda: RuntimeError(CONNECT_FAILURE),
], ids=['provider', 'connection'])
def test_messages_sse_error_event_is_masked(exc):
    """/v1/messages turns a mid-stream failure into an SSE error event inside the SDK adapter."""
    from litellm.llms.anthropic.experimental_pass_through.adapters import streaming_iterator
    text = streaming_iterator._mid_stream_error_sse_event(exc()).decode()
    assert text.startswith('event: error\n')
    assert [s for s in LEAKS if s in text] == [], text


def test_resolved_chatgpt_access_token_is_masked_by_value():
    from litellm.litellm_core_utils.woow_client_guard import client_error_message
    from litellm.llms.chatgpt.common_utils import get_chatgpt_default_headers
    token = 'opaqueCHATGPTaccess-token-0123456789'
    headers = get_chatgpt_default_headers(token, None)
    assert headers['Authorization'] == 'Bearer ' + token  # the request itself is unchanged
    assert token not in client_error_message(f'upstream said: token {token} is expired')


def test_responses_websocket_failure_frame_is_masked():
    from litellm.proxy.response_api_endpoints.endpoints import _responses_ws_failure_frame
    frame = json.loads(_responses_ws_failure_frame(RuntimeError(f'Bearer {PROVIDER_KEY} at {INTERNAL}')))
    assert PROVIDER_KEY not in frame['error']['message'] and 'mock-upstream' not in frame['error']['message']


def test_ordinary_errors_keep_their_text(gateway):
    result = gateway.request('POST', '/v1/chat/completions', {'model': 'missing-model', 'messages': M})
    assert result.status_code in (400, 401, 404) and 'missing-model' in result.text


# R04: proxy_admin_viewer cannot run inference -------------------------------------------------
VIEWER_DENIED_ROUTES = [
    ('/v1/chat/completions', {'model': 'ok', 'messages': M}),
    ('/chat/completions', {'model': 'ok', 'messages': M, 'stream': True}),
    ('/v1/completions', {'model': 'ok', 'prompt': 'x'}),
    ('/v1/embeddings', {'model': 'ok', 'input': 'x'}),
    ('/v1/responses', {'model': 'ok', 'input': 'x'}),
    ('/v1/messages', {'model': 'ok', 'messages': M, 'max_tokens': 8}),
    ('/v1/images/generations', {'model': 'ok', 'prompt': 'x'}),
    ('/v1/audio/speech', {'model': 'ok', 'input': 'x', 'voice': 'alloy'}),
]


@pytest.mark.parametrize('path,body', VIEWER_DENIED_ROUTES, ids=[p for p, _ in VIEWER_DENIED_ROUTES])
def test_viewer_inference_is_refused_before_any_provider_call(gateway, path, body):
    result = gateway.request('POST', path, body, token=VIEWER)
    assert result.status_code == 403, result.text[:400]
    assert VIEWER_DENIED in result.text
    # No provider call and nothing billed (a 403 may still be recorded as a failed request, cost 0).
    assert gateway.upstream.calls == [] and all(row.get('response_cost') == 0 for row in gateway.spend)


def test_viewer_reads_and_other_roles_are_unchanged(gateway):
    models = gateway.request('GET', '/v1/models', token=VIEWER)
    assert models.status_code == 200 and 'ok' in {m['id'] for m in models.json()['data']}
    assert gateway.request('POST', '/v1/chat/completions', {'model': 'ok', 'messages': M}).status_code == 200
    assert gateway.request('POST', '/v1/chat/completions', {'model': 'ok', 'messages': M}, token=MASTER).status_code == 200


# R11: Responses stream events carry the requested model group ---------------------------------
def test_streaming_responses_events_name_the_model_group(gateway):
    result = gateway.request('POST', '/v1/responses', {'model': 'respgroup', 'input': 'x', 'stream': True})
    assert result.status_code == 200
    events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith('data: ') and line[6:] != '[DONE]']
    models = [e['response']['model'] for e in events if isinstance(e.get('response'), dict)]
    assert len(models) == 3 and set(models) == {'respgroup'}, models
    assert any(e.get('type') == 'response.output_text.delta' for e in events)


def test_non_streaming_responses_already_name_the_model_group(gateway):
    result = gateway.request('POST', '/v1/responses', {'model': 'respgroup', 'input': 'x'})
    assert result.status_code == 200 and result.json()['model'] == 'respgroup'


# R15: /health for admins too, the health table and ChatGPT api_base ---------------------------
def test_health_is_masked_for_admins_and_in_the_health_table(gateway):
    result = gateway.request('GET', '/health?model=echo', token=MASTER)
    assert result.status_code in (200, 503)
    endpoints = result.json()['unhealthy_endpoints']
    assert endpoints and all('\n' not in e['error'] for e in endpoints)
    assert 'fx upstream error 401' in endpoints[0]['error'] and endpoints[0]['api_base'].endswith(HOST + '/v1')
    assert leaked(result) == [] and 'stack trace' not in result.text and 'Traceback' not in result.text
    stored = json.dumps(gateway.prisma.saved, default=str)
    assert gateway.prisma.saved and PROVIDER_KEY not in stored and 'Traceback' not in stored


def test_health_latest_masks_rows_written_before(gateway):
    gateway.prisma.rows = [types.SimpleNamespace(
        health_check_id='h1', model_name='echo', model_id='dep-echo', status='unhealthy', healthy_count=0,
        unhealthy_count=1, response_time_ms=1.0, details=None, checked_by='admin', checked_at=None, created_at=None,
        error_message=f'litellm.AuthenticationError: received Authorization: Bearer {PROVIDER_KEY}\n'
                      'stack trace: Traceback (most recent call last):\n  File "/app/x.py"')]
    result = gateway.request('GET', '/health/latest', token=MASTER)
    row = result.json()['latest_health_checks']['dep-echo']
    assert row['error_message'].startswith('litellm.AuthenticationError') and leaked(result) == []
    assert 'Traceback' not in row['error_message']


def test_chatgpt_health_endpoint_shows_api_base():
    from litellm.proxy.health_check import _clean_endpoint_data
    from litellm.proxy.health_endpoints._health_endpoints import _strip_admin_only_fields_from_health_result
    data = {'model': 'chatgpt/gpt-5.5', 'api_key': 'woow-account:default', 'error': 'x'}
    admin = _clean_endpoint_data(dict(data), details=True)
    assert admin['api_base'].startswith('https://chatgpt.com/backend-api') and 'api_key' not in admin
    assert 'api_base' not in _clean_endpoint_data(dict(data), details=False)
    stripped = _strip_admin_only_fields_from_health_result({'unhealthy_endpoints': [admin]})
    assert 'api_base' not in stripped['unhealthy_endpoints'][0]  # non-admin callers still never see it


# R01: no new names the HA Ingress cannot address ----------------------------------------------
def refused_by_name_rule(result):
    return result.status_code == 400 and 'Home Assistant sidebar' in result.text


@pytest.mark.parametrize('name', ['x%2Fy', 'x/y', '100%'])
def test_new_credential_names_with_percent_or_slash_are_refused(gateway, name):
    result = gateway.request('POST', '/credentials', {'credential_name': name, 'credential_info': {}, 'credential_values': {'api_key': 'k'}},
                             token=MASTER)
    assert refused_by_name_rule(result), result.text[:400]
    assert result.json()['error']['param'] == 'credential_name'


@pytest.mark.parametrize('name', ['x y', '我的 OpenAI 金鑰', 'plain-name_1.2:3'])
def test_other_credential_names_pass_the_rule(gateway, name):
    result = gateway.request('POST', '/credentials', {'credential_name': name, 'credential_info': {}, 'credential_values': {'api_key': 'k'}},
                             token=MASTER)
    assert not refused_by_name_rule(result)


def test_credential_rename_is_refused_but_a_stored_name_keeps_working(gateway):
    rename = gateway.request('PATCH', '/credentials/plain', {'credential_name': 'new/name', 'credential_info': {}}, token=MASTER)
    assert refused_by_name_rule(rename)
    keep = gateway.request('PATCH', '/credentials/x%25y', {'credential_name': 'x%y', 'credential_info': {}}, token=MASTER)
    assert not refused_by_name_rule(keep)


@pytest.mark.parametrize('body,param', [
    ({'model_name': 'm%20x', 'litellm_params': {'model': 'openai/x'}}, 'model_name'),
    ({'model_name': 'ok-name', 'litellm_params': {'model': 'openai/x'}, 'model_info': {'id': 'a/b'}}, 'model_info.id'),
    ({'model_name': 'ok-name', 'litellm_params': {'model': 'openai/x'}, 'model_info': {'id': 'a%b'}}, 'model_info.id'),
])
def test_new_models_with_unsafe_name_or_id_are_refused(gateway, body, param):
    result = gateway.request('POST', '/model/new', body, token=MASTER)
    assert refused_by_name_rule(result) and result.json()['error']['param'] == param


def test_provider_prefixed_model_names_pass_the_rule(gateway):
    result = gateway.request('POST', '/model/new', {'model_name': 'chatgpt/gpt-5.5', 'litellm_params': {'model': 'openai/x'}},
                             token=MASTER)
    assert not refused_by_name_rule(result)


@pytest.mark.parametrize('method,path,body,refused', [
    ('POST', '/model/update', {'model_name': 'r%x', 'model_info': {'id': 'dep-ok'}, 'litellm_params': {'model': 'openai/x'}}, True),
    ('PATCH', '/model/dep-ok/update', {'model_name': 'r%x'}, True),
    ('POST', '/model/update', {'model_name': 'legacy%name', 'model_info': {'id': 'dep-legacy'},
                               'litellm_params': {'model': 'openai/x'}}, False),
    ('PATCH', '/model/dep-legacy/update', {'model_name': 'legacy%name'}, False),
    ('PATCH', '/model/dep-ok/update', {'model_name': 'chatgpt/renamed'}, False),
])
def test_model_rename_rule(gateway, method, path, body, refused):
    assert refused_by_name_rule(gateway.request(method, path, body, token=MASTER)) is refused
