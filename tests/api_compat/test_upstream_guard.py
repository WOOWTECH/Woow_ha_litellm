"""Upstream guard contracts (apply_upstream_guard.py) on the real proxy app; nothing leaves the process.

The provider boundary is httpx's transport, answering like OpenRouter: error bodies carry the
gateway's own account (``user_id``), ``error.metadata`` (raw upstream text, provider name, BYOK
flag, flagged input) and provider headers; the scenario is chosen by the fake api_base host. The
spend-log boundary is the real _ProxyDBLogger writing to a recording DB writer; auth uses the real
cache-backed path. Every identifier below is fake (``*FAKE*``).
"""
import asyncio
import json
import logging
import re
import httpx
import pytest
import litellm
from litellm.proxy import proxy_server as server
from litellm.proxy._types import LiteLLM_UserTable, LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.common_utils.user_api_key_cache import UserApiKeyCache
from litellm.proxy.hooks.proxy_track_cost_callback import _ProxyDBLogger
from litellm.proxy.utils import hash_token

MASTER = 'sk-upstream-guard-master-fixture'
VIRTUAL = 'sk-upstream-guard-virtual-fixture'
TINY = 'sk-upstream-guard-tiny-budget-fixture'
CANARY = 'upstream-guard-provider-secret-sentinel'
M = [{'role': 'user', 'content': 'offline'}]

USER_ID = 'user_FAKEgatewayACCOUNT01'
ORG_ID = 'org-FAKEbyokORG0001'
UPSTREAM_REQ = 'req_FAKEupstream0001'
GEN_ID = 'gen-FAKEgeneration01'
X_REQUEST_ID = 'req_FAKExrequest0001'
CF_RAY = 'FAKEcfRAY-TPE'
PROBE_HEADER, PROBE_VALUE = 'x-woowfake-probe', 'probe-FAKE-value'
FLAGGED = 'FLAGGED-INPUT-SENTINEL'
FAKE_KEY = 'sk-or-v1-' + '0123456789abcdef' * 4
HOST = 'upstream.invalid'
HEX64 = re.compile(r'(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])')
LEAK_SENTINELS = (
    'user_FAKE', 'org-FAKE', 'req_FAKE', 'gen-FAKE', 'metadata', 'provider_name', 'is_byok', FLAGGED,
    'openrouter.ai', 'User ID:', 'add your own key', 'llm_provider-cf-ray', 'llm_provider-x-request-id',
    PROBE_HEADER, PROBE_VALUE, CF_RAY, 'API Base', 'Messages:', 'WoowFakeProvider', FAKE_KEY, CANARY,
)
# x-litellm-model-api-base still names the internal api_base on success/stream responses (P3, not here).
P3_HEADERS = frozenset({'x-litellm-model-api-base'})


def _error(status, message, metadata=None):
    body = {'error': {'message': message, 'code': status}, 'user_id': USER_ID}
    if metadata is not None:
        body['error']['metadata'] = metadata
    return status, body


ERRORS = {
    'or400': _error(400, 'temperature: Invalid input: expected number, received string'),
    'or400nested': _error(400, 'Provider returned error', {
        'raw': json.dumps({'error': {'message': "Invalid 'tools[0].name': string too long " + UPSTREAM_REQ}}),
        'provider_name': 'WoowFakeProvider', 'is_byok': False}),
    'or400key': _error(400, 'Invalid upstream key ' + FAKE_KEY),
    'or400ctx': _error(400, "This model's maximum context length is 1000 tokens. However, you requested 5000 tokens"),
    'or401': _error(401, 'User not found.'),
    'or402': _error(402, 'This request requires more credits, or fewer max_tokens. You requested up to 4096 tokens, '
                         'but can only afford 1234. To increase, visit https://openrouter.ai/settings/credits'),
    'or403key': _error(403, 'Key limit exceeded (total limit). Manage it using https://openrouter.ai/settings/keys'),
    'or403mod': _error(403, 'WoowFakeProvider requires moderation on your input. Your input was flagged for "violence"', {
        'reasons': ['violence'], 'flagged_input': FLAGGED, 'provider_name': 'WoowFakeProvider'}),
    'or404': _error(404, 'No endpoints found matching your data policy. Configure: https://openrouter.ai/settings/privacy'),
    'or408': _error(408, 'Timed out', {'provider_name': 'WoowFakeProvider'}),
    'or413': _error(413, 'Request entity too large'),
    'or422': _error(422, 'Unprocessable: messages[0].content is empty'),
    'or429': _error(429, 'Provider returned error', {
        'raw': 'woowfake is temporarily rate-limited upstream. Please retry shortly, or add your own key to accumulate '
               'your rate limits: https://openrouter.ai/settings/integrations',
        'provider_name': 'WoowFakeProvider', 'is_byok': False}),
    'or429byok': _error(429, 'Provider returned error', {
        'raw': json.dumps({'error': {'message': f'Rate limit reached in organization {ORG_ID}. request id {UPSTREAM_REQ}'}}),
        'provider_name': 'WoowFakeProvider', 'is_byok': True}),
    'or500': _error(500, 'Internal Server Error', {'raw': f'upstream crashed {UPSTREAM_REQ} {GEN_ID}',
                                                   'provider_name': 'WoowFakeProvider'}),
    'or502': _error(502, 'Provider returned error', {'raw': f'<html>502 Bad Gateway {UPSTREAM_REQ}</html>',
                                                    'provider_name': 'WoowFakeProvider'}),
    'or503': _error(503, 'provider overloaded', {'available_providers': ['WoowFakeProvider']}),
    'or504': _error(504, 'Gateway timeout', {'provider_name': 'WoowFakeProvider'}),
}
MIDSTREAM = {'midstream': 502, 'midstream-strcode': 'server_error'}


def _error_headers(status):
    headers = {'x-request-id': X_REQUEST_ID, 'cf-ray': CF_RAY, PROBE_HEADER: PROBE_VALUE,
               'x-ratelimit-remaining-requests': '7'}
    if status == 429:
        headers['retry-after'] = '7'
    return headers


def _sse(chunks):
    return (''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + 'data: [DONE]\n\n').encode()


def _chunk(delta, finish=None, **extra):
    return dict({'id': 'gen-offline-ok', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'woowfake/model',
                 'provider': 'WoowFakeProvider',
                 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}, **extra)


class OpenRouter:
    """OpenRouter-shaped provider at the transport boundary; ``<scenario>.upstream.invalid`` picks the answer."""

    def __init__(self):
        self.calls = []
        self.delay = 0.0
        self.usage = {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}
        self.content = 'OK'
        self.finish = 'stop'

    async def handle(self, transport, request):
        scenario = request.url.host.split('.')[0]
        body = json.loads(request.content or b'{}')
        self.calls.append({'scenario': scenario, 'path': request.url.path, 'body': body})
        if self.delay:
            await asyncio.sleep(self.delay)
        if scenario in ERRORS:
            status, payload = ERRORS[scenario]
            return httpx.Response(status, json=payload, headers=_error_headers(status), request=request)
        headers = {'x-request-id': X_REQUEST_ID, 'cf-ray': CF_RAY, PROBE_HEADER: PROBE_VALUE,
                   'x-ratelimit-remaining-requests': '7', 'retry-after': '1'}
        path = request.url.path
        if path.endswith('/embeddings'):
            return httpx.Response(200, headers=headers, request=request, json={
                'object': 'list', 'model': 'woowfake/model', 'provider': 'WoowFakeProvider',
                'data': [{'object': 'embedding', 'index': 0, 'embedding': [0.1, 0.2]}],
                'usage': {'prompt_tokens': 1, 'total_tokens': 1}})
        if path.endswith('/responses'):
            return httpx.Response(200, headers=headers, request=request, json=RESPONSES_OK)
        if scenario in MIDSTREAM and body.get('stream'):
            error = {'code': MIDSTREAM[scenario], 'message': 'Provider disconnected',
                     'metadata': {'raw': f'upstream reset {UPSTREAM_REQ} {GEN_ID}', 'provider_name': 'WoowFakeProvider',
                                  'headers': {'x-woowfake-probe': PROBE_VALUE}}, 'user_id': USER_ID}
            chunks = [_chunk({'role': 'assistant', 'content': 'Hel'}),
                      _chunk({'role': 'assistant', 'content': ''}, 'error', error=error, user_id=USER_ID)]
            return httpx.Response(200, headers={**headers, 'content-type': 'text/event-stream'},
                                  content=_sse(chunks), request=request)
        message = {'role': 'assistant', 'content': self.content}
        if body.get('stream'):
            chunks = [_chunk({'role': 'assistant', 'content': self.content}), _chunk({'content': ''}, self.finish),
                      dict(_chunk({}), choices=[], usage=self.usage)]
            return httpx.Response(200, headers={**headers, 'content-type': 'text/event-stream'},
                                  content=_sse(chunks), request=request)
        return httpx.Response(200, headers=headers, request=request, json={
            'id': 'gen-offline-ok', 'object': 'chat.completion', 'created': 1, 'model': 'woowfake/model',
            'provider': 'WoowFakeProvider',
            'choices': [{'index': 0, 'message': message, 'finish_reason': self.finish}], 'usage': self.usage})


RESPONSES_OK = {
    'id': 'resp_offline', 'object': 'response', 'created_at': 1, 'status': 'completed', 'model': 'woowfake/model',
    'output': [{'type': 'message', 'id': 'msg_offline', 'status': 'completed', 'role': 'assistant',
                'content': [{'type': 'output_text', 'text': 'OK', 'annotations': []}]}],
    'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2,
              'input_tokens_details': {'cached_tokens': 0}, 'output_tokens_details': {'reasoning_tokens': 0}},
}
PRICE = {'input_cost_per_token': 1.25e-6, 'output_cost_per_token': 2.5e-6}


def _deployment(name, host, model='openrouter/woowfake/model', model_info=None, **params):
    return {'model_name': name,
            'litellm_params': {'model': model, 'api_key': CANARY, 'api_base': f'https://{host}.{HOST}/api/v1', **params},
            'model_info': {'id': 'dep-' + name, **(model_info or {})}}


MODEL_LIST = [_deployment(s, s) for s in (*ERRORS, *MIDSTREAM)] + [
    _deployment('free', 'ok'),
    # Distinct backend models: LiteLLM registers custom model_info into the shared global cost map.
    _deployment('plain', 'ok', model='openrouter/woowfake/plain', model_info=dict(PRICE)),
    _deployment('reasoning', 'ok', model='openrouter/woowfake/reasoning', model_info=dict(PRICE, supports_reasoning=True)),
    _deployment('reasoning512', 'ok', model='openrouter/woowfake/reasoning512', model_info=dict(
        PRICE, supports_reasoning=True, woow_reasoning_output_reserve_tokens=512)),
    _deployment('unpriced', 'ok', model='openrouter/woowfake/unpriced'),
    _deployment('newmodel', 'ok', model='openrouter/woowfake/newmodel'),
    _deployment('routedefault', 'ok', model='openrouter/woowfake/newmodel', reasoning={'enabled': False}),
    _deployment('compat400', 'or400', model='openai/woowfake/model'),
    _deployment('compatctx', 'or400ctx', model='openai/woowfake/model'),
    _deployment('compat503', 'or503', model='openai/woowfake/model'),
]


class Gateway:
    def __init__(self, app, upstream, spend):
        self.app, self.upstream, self.spend = app, upstream, spend

    async def send(self, method, path, body=None, token=VIRTUAL, settle=0.3):
        transport = httpx.ASGITransport(app=self.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url='http://offline.invalid', timeout=60) as client:
            headers = {'Authorization': 'Bearer ' + token, 'content-type': 'application/json'}
            result = await client.request(method, path, headers=headers,
                                          content=json.dumps(body) if body is not None else None)
            await result.aread()
            if settle:
                await asyncio.sleep(settle)  # let detached success/failure logging tasks finish
            return result

    def request(self, method, path, body=None, token=VIRTUAL):
        result = asyncio.run(self.send(method, path, body, token))
        assert CANARY not in result.text and MASTER not in result.text and VIRTUAL not in result.text
        return result


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.setattr(server, 'master_key', MASTER)
    monkeypatch.setattr(server, 'general_settings', {})
    monkeypatch.setattr(server, 'user_custom_auth', None)
    monkeypatch.setattr(server, 'user_model', None)
    cache = UserApiKeyCache()
    cache.set_cache(key='offline-user', value=LiteLLM_UserTable(user_id='offline-user', user_role='internal_user'),
                    model_type=LiteLLM_UserTable)
    for key, budget in ((VIRTUAL, None), (TINY, 0.0001)):
        cache.set_cache(key=hash_token(key), model_type=UserAPIKeyAuth, value=UserAPIKeyAuth(
            api_key=hash_token(key), token=hash_token(key), user_role=LitellmUserRoles.INTERNAL_USER,
            models=[], user_id='offline-user', metadata={}, max_budget=budget, spend=0.0,
            key_alias='guard-offline-' + ('tiny' if budget else 'virtual')))
    monkeypatch.setattr(server, 'user_api_key_cache', cache)
    # Every row is already in the real memory cache; any database read would fail loudly.
    monkeypatch.setattr(server, 'prisma_client', object())

    async def floor(counter_key, **kwargs):  # healthy DB holding no spend yet
        return 0.0
    monkeypatch.setattr(server, '_authoritative_floor_spend', floor)
    server.spend_counter_cache.in_memory_cache.flush_cache()
    router = litellm.Router(model_list=[json.loads(json.dumps(d)) for d in MODEL_LIST], num_retries=0)
    monkeypatch.setattr(server, 'llm_router', router)
    monkeypatch.setattr(server, 'llm_model_list', router.model_list)
    upstream = OpenRouter()
    monkeypatch.setattr(litellm, 'disable_aiohttp_transport', True)
    monkeypatch.setattr(litellm, 'drop_params', True)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request',
                        lambda transport, request: upstream.handle(transport, request))
    spend = []

    async def update_database(**kwargs):
        spend.append(kwargs)
    monkeypatch.setattr(server.proxy_logging_obj.db_spend_update_writer, 'update_database', update_database)
    monkeypatch.setattr(litellm, 'callbacks', [_ProxyDBLogger()])
    for env in ('WOOW_PROVIDER_HEADERS', 'WOOW_TOKEN_LIMIT_VALIDATION', 'WOOW_BUDGET_ADMISSION',
                'WOOW_BUDGET_UNPRICED', 'WOOW_BUDGET_REASONING_RESERVE_TOKENS', 'WOOW_BUDGET_MIN_INPUT_TOKENS'):
        monkeypatch.delenv(env, raising=False)
    yield Gateway(server.app, upstream, spend)
    server.spend_counter_cache.in_memory_cache.flush_cache()


class Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def messages(self, prefix):
        return [r.getMessage() for r in self.records if r.getMessage().startswith(prefix)]


@pytest.fixture
def litellm_log():
    """LiteLLM's logger does not propagate to pytest's root handler; record it directly."""
    logger = logging.getLogger('LiteLLM')
    handler = Records()
    logger.addHandler(handler)
    yield handler
    logger.removeHandler(handler)


def leaks(result):
    headers = '\n'.join(f'{k}: {v}' for k, v in result.headers.items() if k.lower() not in P3_HEADERS)
    found = [s for s in LEAK_SENTINELS if s in result.text or s.lower() in headers.lower()]
    found += [f'llm_provider header {k}' for k in result.headers
              if k.lower().startswith('llm_provider-')
              and not (k.lower().startswith('llm_provider-x-ratelimit-') or k.lower() == 'llm_provider-retry-after')]
    found += ['64-hex'] * bool(HEX64.search(result.text) or HEX64.search(headers))
    found += ['api_base host'] * bool(HOST in result.text or HOST in headers)
    return found


def error_of(result):
    """(status, type, code, param) of an OpenAI or Anthropic error envelope."""
    body = result.json()
    error = body['error']
    return result.status_code, error.get('type'), error.get('code'), error.get('param')


def message_of(result):
    return result.json()['error']['message']


def sse_events(result):
    return [json.loads(line[6:]) for line in result.text.splitlines()
            if line.startswith('data: ') and line[6:].strip() != '[DONE]']


# P0: upstream error leak -------------------------------------------------------------------
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
ALL_STATUSES = tuple(ERRORS)
SAMPLE_STATUSES = ('or400', 'or401', 'or403mod', 'or429byok', 'or502')
LEAK_CASES = (
    [('chat', s) for s in ALL_STATUSES]
    + [(r, s) for r in ROUTES if r != 'chat' for s in SAMPLE_STATUSES]
    + [(r, s) for r in ('chat-stream', 'messages-stream') for s in MIDSTREAM]
)


@pytest.mark.parametrize('token', [MASTER, VIRTUAL], ids=['master', 'virtual'])
@pytest.mark.parametrize('route,scenario', LEAK_CASES)
def test_no_account_identifiers_leak(gateway, route, scenario, token):
    path, body = ROUTES[route]
    result = gateway.request('POST', path, body(scenario), token=token)
    assert gateway.upstream.calls, 'the provider must have been called'
    assert leaks(result) == [], result.text[:600]
    if scenario in MIDSTREAM:
        assert result.status_code == 200 and 'error' in result.text
    else:
        assert result.status_code >= 400


# (status, error.type, error.code, error.param) on HA 0.1.8 for the same fake provider answers,
# recorded on the replayed 0.1.8 chain (pristine 1.104.0 + every earlier installer); the guard
# changes messages only.
BASELINE_018 = {
    'chat': {'or400': (400, 'invalid_request_error', '400', None), 'or400nested': (400, 'invalid_request_error', '400', None), 'or400key': (400, 'invalid_request_error', '400', None), 'or400ctx': (400, 'invalid_request_error', '400', None), 'or401': (401, 'authentication_error', '401', None), 'or402': (402, 'invalid_request_error', '402', None), 'or403key': (403, 'permission_error', '403', None), 'or403mod': (403, 'permission_error', '403', None), 'or404': (404, 'invalid_request_error', '404', None), 'or408': (408, 'invalid_request_error', '408', None), 'or413': (413, 'invalid_request_error', '413', None), 'or422': (422, 'invalid_request_error', '422', None), 'or429': (429, 'throttling_error', '429', None), 'or429byok': (429, 'throttling_error', '429', None), 'or500': (500, 'internal_server_error', '500', None), 'or502': (502, 'internal_server_error', '502', None), 'or503': (503, 'internal_server_error', '503', None), 'or504': (504, 'internal_server_error', '504', None)},
    'chat-stream': {'or400': (400, 'invalid_request_error', '400', None), 'or401': (401, 'authentication_error', '401', None), 'or403mod': (403, 'permission_error', '403', None), 'or429byok': (429, 'throttling_error', '429', None), 'or502': (502, 'internal_server_error', '502', None)},
    'completions': {'or400': (400, None, '400', None), 'or401': (401, None, '401', None), 'or403mod': (403, None, '403', None), 'or429byok': (429, 'throttling_error', '429', None), 'or502': (502, None, '502', None)},
    'completions-stream': {'or400': (400, None, '400', None), 'or401': (401, None, '401', None), 'or403mod': (403, None, '403', None), 'or429byok': (429, 'throttling_error', '429', None), 'or502': (502, None, '502', None)},
    'embeddings': {'or400': (400, 'invalid_request_error', '400', None), 'or401': (401, 'authentication_error', '401', None), 'or403mod': (403, 'permission_error', '403', None), 'or429byok': (429, 'throttling_error', '429', None), 'or502': (502, 'internal_server_error', '502', None)},
    'responses': {'or400': (400, 'invalid_request_error', '400', None), 'or401': (401, 'authentication_error', '401', None), 'or403mod': (403, 'permission_error', '403', None), 'or429byok': (429, 'throttling_error', '429', None), 'or502': (502, 'internal_server_error', '502', None)},
    'responses-stream': {'or400': (400, 'invalid_request_error', '400', None), 'or401': (401, 'authentication_error', '401', None), 'or403mod': (403, 'permission_error', '403', None), 'or429byok': (429, 'throttling_error', '429', None), 'or502': (502, 'internal_server_error', '502', None)},
    'messages': {'or400': (400, 'invalid_request_error', None, None), 'or401': (401, 'authentication_error', None, None), 'or403mod': (403, 'permission_error', None, None), 'or429byok': (429, 'rate_limit_error', None, None), 'or502': (502, 'api_error', None, None)},
    'messages-stream': {'or400': (400, 'invalid_request_error', None, None), 'or401': (401, 'authentication_error', None, None), 'or403mod': (403, 'permission_error', None, None), 'or429byok': (429, 'rate_limit_error', None, None), 'or502': (502, 'api_error', None, None)},
}


@pytest.mark.parametrize('route,scenario', [(r, s) for r, rows in BASELINE_018.items() for s in rows])
def test_envelope_unchanged_vs_baseline(gateway, route, scenario):
    path, body = ROUTES[route]
    assert error_of(gateway.request('POST', path, body(scenario))) == BASELINE_018[route][scenario]


def test_400_keeps_actionable_message(gateway):
    result = gateway.request('POST', '/v1/chat/completions', {'model': 'or400', 'messages': M})
    message = message_of(result)
    assert result.status_code == 400
    assert ('OpenrouterException - {"error":{"message":"temperature: Invalid input: expected number, '
            'received string","code":400}}') in message
    assert message.startswith('litellm.BadRequestError: ') and "model group 'or400' failed" in message


def test_nested_raw_reason_appended(gateway):
    message = message_of(gateway.request('POST', '/v1/chat/completions', {'model': 'or400nested', 'messages': M}))
    assert "Provider returned error: Invalid 'tools[0].name': string too long [redacted]" in message
    assert 'req_FAKE' not in message and 'WoowFakeProvider' not in message


@pytest.mark.parametrize('model,error_class', [
    ('compatctx', 'ContextWindowExceededError'),  # OpenAI-compatible mapping classifies on the scrubbed text
    ('or400ctx', 'BadRequestError'),  # the openrouter mapping never classified it (same on 0.1.8)
])
def test_context_window_error_still_classified(gateway, model, error_class):
    result = gateway.request('POST', '/v1/chat/completions', {'model': model, 'messages': M})
    assert result.status_code == 400 and 'maximum context length is 1000 tokens' in message_of(result)
    assert leaks(result) == []
    failures = [row['completion_response'] for row in gateway.spend if isinstance(row['completion_response'], Exception)]
    assert [type(f).__name__ for f in failures] == [error_class]


@pytest.mark.parametrize('scenario,expected', [
    ('or401', "The proxy's upstream credentials were rejected. Your API key is valid; contact the proxy administrator."),
    ('or402', 'The upstream provider declined the request for a billing reason. Contact the proxy administrator.'),
    ('or403key', 'The upstream provider refused the request. Contact the proxy administrator.'),
    ('or403mod', 'The input was flagged by upstream moderation (violence).'),
    ('or408', 'The upstream provider timed out.'),
    ('or504', 'The upstream provider timed out.'),
    ('or429', 'The upstream provider is rate limiting this model. Retry later.'),
    ('or404', 'No endpoints found matching your data policy. Configure: [redacted-url]'),
    ('or503', 'provider overloaded'),
])
def test_status_policy_messages(gateway, scenario, expected):
    result = gateway.request('POST', '/v1/chat/completions', {'model': scenario, 'messages': M})
    assert f'{{"error":{{"message":"{expected}","code":{ERRORS[scenario][0]}}}}}' in message_of(result)


def test_moderation_reasons_only(gateway):
    result = gateway.request('POST', '/v1/messages', {'model': 'or403mod', 'messages': M, 'max_tokens': 32})
    assert result.status_code == 403 and 'flagged by upstream moderation (violence)' in result.text
    assert FLAGGED not in result.text and 'flagged_input' not in result.text


def test_retry_after_kept_on_429(gateway):
    result = gateway.request('POST', '/v1/chat/completions', {'model': 'or429', 'messages': M})
    assert result.status_code == 429
    assert result.headers.get('retry-after') == '7' or result.headers.get('llm_provider-retry-after') == '7'


@pytest.mark.parametrize('route', ['chat-stream', 'messages-stream'])
def test_midstream_string_code_maps_to_500(gateway, route):
    path, body = ROUTES[route]
    result = gateway.request('POST', path, body('midstream-strcode'))
    assert 'invalid literal' not in result.text and 'Provider disconnected' in result.text
    errors = [e['error'] for e in sse_events(result) if isinstance(e.get('error'), dict)]
    assert errors, result.text[:600]
    if route == 'chat-stream':
        assert errors[-1].get('code') == '500'
    assert all(e.get('type') != 'None' and e.get('param') != 'None' for e in errors)  # null is fine, "None" is not


def test_midstream_int_code_keeps_status(gateway):
    result = gateway.request('POST', '/v1/chat/completions', {'model': 'midstream', 'messages': M, 'stream': True})
    errors = [e['error'] for e in sse_events(result) if isinstance(e.get('error'), dict)]
    assert errors and errors[-1].get('code') == '502' and leaks(result) == []


def test_spend_log_row_scrubbed_keeps_codes(gateway):
    result = gateway.request('POST', '/v1/chat/completions', {'model': 'or429byok', 'messages': M})
    assert result.status_code == 429
    failures = [row for row in gateway.spend if isinstance(row['completion_response'], Exception)]
    assert len(failures) == 1
    row = failures[0]
    stored = str(row['completion_response']) + json.dumps(row.get('kwargs', {}).get('standard_logging_object') or {},
                                                          default=str)
    assert 'user_FAKE' not in stored and 'org-FAKE' not in stored and 'is_byok' not in stored
    logged = row['kwargs']['standard_logging_object']
    info = logged['error_information']
    assert info['error_code'] == '429' and info['error_class'] == 'RateLimitError'
    assert info['llm_provider'] == 'openrouter'
    assert X_REQUEST_ID in json.dumps(logged, default=str), 'upstream x-request-id must stay for the admin'


def test_server_log_keeps_raw_for_admin(gateway, litellm_log):
    gateway.request('POST', '/v1/chat/completions', {'model': 'or400', 'messages': M})
    scrubbed = litellm_log.messages('Woow upstream error scrubbed')
    assert len(scrubbed) == 1 and USER_ID in scrubbed[0] and 'status=400' in scrubbed[0]


def test_non_openrouter_message_byte_identical(gateway, monkeypatch):
    from litellm.litellm_core_utils import woow_provider_errors
    guarded = gateway.request('POST', '/v1/chat/completions', {'model': 'compat503', 'messages': M})
    monkeypatch.setattr(woow_provider_errors, 'scrub_provider_error', lambda provider, text, status=None: text)
    unguarded = gateway.request('POST', '/v1/chat/completions', {'model': 'compat503', 'messages': M})
    assert guarded.status_code == unguarded.status_code == 503
    assert message_of(guarded) == message_of(unguarded) and 'provider overloaded' in message_of(guarded)


def test_openai_compatible_route_to_openrouter_is_scrubbed(gateway):
    result = gateway.request('POST', '/v1/chat/completions', {'model': 'compat400', 'messages': M})
    assert result.status_code == 400 and leaks(result) == []
    assert 'temperature: Invalid input' in message_of(result)


def test_success_headers_allowlist(gateway, monkeypatch):
    result = gateway.request('POST', '/v1/chat/completions', {'model': 'free', 'messages': M})
    provider = {k.lower() for k in result.headers if k.lower().startswith('llm_provider-')}
    assert result.status_code == 200 and provider
    assert all(k.startswith('llm_provider-x-ratelimit-') or k == 'llm_provider-retry-after' for k in provider), provider
    assert 'llm_provider-x-ratelimit-remaining-requests' in provider and 'llm_provider-retry-after' in provider
    monkeypatch.setenv('WOOW_PROVIDER_HEADERS', 'all')
    everything = gateway.request('POST', '/v1/chat/completions', {'model': 'free', 'messages': M})
    assert {'llm_provider-cf-ray', 'llm_provider-x-request-id', 'llm_provider-' + PROBE_HEADER} <= {
        k.lower() for k in everything.headers}


def test_full_sk_key_still_redacted(gateway):
    result = gateway.request('POST', '/v1/chat/completions', {'model': 'or400key', 'messages': M})
    assert result.status_code == 400 and FAKE_KEY not in result.text and '0123456789abcdef' * 4 not in result.text
    assert 'Invalid upstream key' in message_of(result)


# P1: output token limits --------------------------------------------------------------------
TOKEN_ROUTES = [
    ('/v1/chat/completions', 'max_tokens', {'model': 'free', 'messages': M}),
    ('/v1/chat/completions', 'max_completion_tokens', {'model': 'free', 'messages': M}),
    ('/v1/completions', 'max_tokens', {'model': 'free', 'prompt': 'offline'}),
    ('/v1/responses', 'max_output_tokens', {'model': 'free', 'input': 'offline'}),
]
INVALID_LIMITS = [
    (-1, "Invalid '{p}': integer below minimum value. Expected a value >= 1, but got -1 instead."),
    (0, "Invalid '{p}': integer below minimum value. Expected a value >= 1, but got 0 instead."),
    (1.5, "Invalid type for '{p}': expected an integer, but got a decimal number instead."),
    ('10', "Invalid type for '{p}': expected an integer, but got a string instead."),
    ('abc', "Invalid type for '{p}': expected an integer, but got a string instead."),
    (True, "Invalid type for '{p}': expected an integer, but got a boolean instead."),
    (False, "Invalid type for '{p}': expected an integer, but got a boolean instead."),
    ([5], "Invalid type for '{p}': expected an integer, but got an array instead."),
    ({}, "Invalid type for '{p}': expected an integer, but got an object instead."),
    (2**31, "Invalid '{p}': integer above maximum value. Expected a value <= 2147483647, but got 2147483648 instead."),
    (2**63, "Invalid '{p}': integer above maximum value. Expected a value <= 2147483647, "
            "but got 9223372036854775808 instead."),
]
LIMIT_IDS = [repr(v) for v, _ in INVALID_LIMITS]


@pytest.mark.parametrize('token', [MASTER, VIRTUAL], ids=['master', 'virtual'])
@pytest.mark.parametrize('path,param,body', TOKEN_ROUTES)
@pytest.mark.parametrize('value,message', INVALID_LIMITS, ids=LIMIT_IDS)
def test_token_limit_rejected(gateway, path, param, body, value, message, token):
    result = gateway.request('POST', path, {**body, param: value}, token=token)
    assert result.status_code == 400
    assert result.json() == {'error': {'message': message.format(p=param), 'type': 'invalid_request_error',
                                       'param': param, 'code': '400'}}
    assert gateway.upstream.calls == [] and gateway.spend == []


@pytest.mark.parametrize('path,param,body', TOKEN_ROUTES)
@pytest.mark.parametrize('value', [1, 16, 16.0, 50_000_000, 2**31 - 1, None, 'absent'])
def test_token_limit_valid_forwarded(gateway, path, param, body, value):
    request = dict(body) if value == 'absent' else {**body, param: value}
    result = gateway.request('POST', path, request)
    assert result.status_code == 200, result.text
    assert len(gateway.upstream.calls) == 1
    sent = gateway.upstream.calls[0]['body']
    if value in (None, 'absent'):
        assert sent.get(param) is None
    elif param == 'max_output_tokens' and value < 16:
        assert sent[param] == 16
    else:
        assert sent[param] == value


def test_responses_small_value_still_raised_to_16(gateway):
    result = gateway.request('POST', '/v1/responses', {'model': 'free', 'input': 'offline', 'max_output_tokens': 5})
    assert result.status_code == 200 and gateway.upstream.calls[0]['body']['max_output_tokens'] == 16


def test_both_chat_limits_are_checked(gateway):
    result = gateway.request('POST', '/v1/chat/completions',
                             {'model': 'free', 'messages': M, 'max_tokens': 16, 'max_completion_tokens': -1})
    assert result.status_code == 400 and result.json()['error']['param'] == 'max_completion_tokens'
    assert gateway.upstream.calls == []


@pytest.mark.parametrize('value,message', INVALID_LIMITS, ids=LIMIT_IDS)
def test_messages_error_shape_pinned(gateway, value, message):
    result = gateway.request('POST', '/v1/messages', {'model': 'free', 'messages': M, 'max_tokens': value})
    assert result.status_code == 400
    # /v1/messages keeps the Anthropic error envelope (no param/code fields there).
    assert result.json() == {'type': 'error', 'error': {'type': 'invalid_request_error',
                                                        'message': message.format(p='max_tokens')}}
    assert gateway.upstream.calls == [] and gateway.spend == []


@pytest.mark.parametrize('mode', ['warn', 'off'])
def test_token_limit_warn_and_off_modes(gateway, monkeypatch, litellm_log, mode):
    monkeypatch.setenv('WOOW_TOKEN_LIMIT_VALIDATION', mode)
    result = gateway.request('POST', '/v1/chat/completions', {'model': 'free', 'messages': M, 'max_tokens': -1})
    assert result.status_code == 200 and gateway.upstream.calls[0]['body']['max_tokens'] == -1
    warnings = litellm_log.messages('Woow invalid output token limit forwarded')
    assert len(warnings) == (1 if mode == 'warn' else 0)
    assert all('offline' not in w and 'param=max_tokens' in w for w in warnings)


# P1: budget admission ------------------------------------------------------------------------
CHAT = '/v1/chat/completions'
REASONING_USAGE = {'prompt_tokens': 204, 'completion_tokens': 377, 'total_tokens': 581,
                   'completion_tokens_details': {'reasoning_tokens': 367}}


def estimate(body, route=CHAT):
    from litellm.proxy.spend_tracking import budget_reservation as br

    async def run():
        counts = await br.count_request_input_tokens(body, route, server.llm_router)
        return br.estimate_request_max_cost(body, route, server.llm_router, counts), counts
    return asyncio.run(run())


def test_tiny_budget_reasoning_rejected_before_upstream(gateway):
    result = gateway.request('POST', CHAT, {'model': 'reasoning', 'messages': M, 'max_tokens': 16}, token=TINY)
    assert result.status_code == 422 and gateway.upstream.calls == []
    assert result.json()['error']['type'] == 'budget_exceeded' and result.json()['error']['code'] == '422'
    assert result.headers.get('x-should-retry') == 'false'
    assert not HEX64.search(result.text) and hash_token(TINY) not in result.text


@pytest.mark.parametrize('path,body', [
    (CHAT, {'model': 'reasoning', 'messages': M, 'max_tokens': 16}),
    ('/v1/messages', {'model': 'reasoning', 'messages': M, 'max_tokens': 16}),
])
def test_budget_status_429_when_configured(gateway, monkeypatch, path, body):
    monkeypatch.setattr(litellm, 'budget_exceeded_status_code', 429)
    result = gateway.request('POST', path, body, token=TINY)
    assert result.status_code == 429 and gateway.upstream.calls == []
    assert result.json()['error']['type'] == 'budget_exceeded' and result.json()['error']['code'] == '429'
    assert result.headers.get('x-should-retry') == 'false' and not HEX64.search(result.text)


def test_budget_message_has_no_entity_hash(gateway):
    message = message_of(gateway.request('POST', CHAT, {'model': 'reasoning', 'messages': M, 'max_tokens': 16},
                                         token=TINY))
    assert message.startswith('Budget has been exceeded! Key budget. Current cost: ')
    assert 'Max budget: 0.0001' in message and hash_token(TINY) not in message and 'key=' not in message


def test_concurrent_reasoning_spend_bounded(gateway):
    """10 concurrent reasoning requests (max_tokens=16, billed 204 + 377 tokens each) on small budgets."""
    one = 204 * PRICE['input_cost_per_token'] + 377 * PRICE['output_cost_per_token']  # billed per request
    gateway.upstream.delay, gateway.upstream.usage = 1.0, REASONING_USAGE
    body = {'model': 'reasoning', 'messages': M, 'max_tokens': 16}

    async def burst(budget):
        key = f'sk-upstream-guard-burst-{budget}-fixture'
        server.user_api_key_cache.set_cache(key=hash_token(key), model_type=UserAPIKeyAuth, value=UserAPIKeyAuth(
            api_key=hash_token(key), token=hash_token(key), user_role=LitellmUserRoles.INTERNAL_USER, models=[],
            user_id='offline-user', metadata={}, max_budget=budget, spend=0.0))
        results = await asyncio.gather(*(gateway.send('POST', CHAT, body, key, settle=0) for _ in range(10)))
        await asyncio.sleep(0.6)
        admitted = [r for r in results if r.status_code == 200]
        assert {r.status_code for r in results} <= {200, 422}
        return admitted, sum(float(r.headers.get('x-litellm-response-cost') or 0) for r in admitted)

    async def run():  # one event loop: the spend counters' locks are bound to it
        return await burst(0.0005), await burst(0.012)
    (tiny, tiny_actual), (small, small_actual) = asyncio.run(run())
    assert tiny_actual <= 0.0005 + one, (len(tiny), tiny_actual)
    # A budget that fits two worst-case reservations admits exactly two, and they stay within it.
    assert len(small) == 2 and small_actual <= 0.012


def test_reasoning_reserve_applied(gateway):
    cost, counts = estimate({'model': 'reasoning', 'messages': M, 'max_tokens': 16})
    assert cost == pytest.approx(256 * PRICE['input_cost_per_token'] + 2048 * PRICE['output_cost_per_token'])
    cost, _ = estimate({'model': 'reasoning512', 'messages': M, 'max_tokens': 16})
    assert cost == pytest.approx(256 * PRICE['input_cost_per_token'] + 512 * PRICE['output_cost_per_token'])


def test_reasoning_reserve_env_override(gateway, monkeypatch):
    monkeypatch.setenv('WOOW_BUDGET_REASONING_RESERVE_TOKENS', '4096')
    monkeypatch.setenv('WOOW_BUDGET_MIN_INPUT_TOKENS', '100')
    cost, _ = estimate({'model': 'reasoning', 'messages': M, 'max_tokens': 16})
    assert cost == pytest.approx(100 * PRICE['input_cost_per_token'] + 4096 * PRICE['output_cost_per_token'])


@pytest.mark.parametrize('disable', [{'reasoning': {'enabled': False}}, {'reasoning': {'effort': 'none'}},
                                     {'reasoning_effort': 'none'}])
def test_reasoning_disabled_body_skips_reserve(gateway, disable):
    cost, _ = estimate({'model': 'reasoning', 'messages': M, 'max_tokens': 16, **disable})
    assert cost == pytest.approx(256 * PRICE['input_cost_per_token'] + 16 * PRICE['output_cost_per_token'])


@pytest.mark.parametrize('value', [-1, 0, False])
def test_negative_max_tokens_reserves_default_not_negative(gateway, value):
    cost, counts = estimate({'model': 'plain', 'messages': M, 'max_tokens': value})
    counted = counts['plain']
    assert cost == pytest.approx(counted * PRICE['input_cost_per_token'] + 16384 * PRICE['output_cost_per_token'])


def test_non_reasoning_estimate_identical_to_pristine(gateway):
    cost, counts = estimate({'model': 'plain', 'messages': M, 'max_tokens': 16})
    assert cost == pytest.approx(counts['plain'] * PRICE['input_cost_per_token'] + 16 * PRICE['output_cost_per_token'])


def test_resize_mode_restores_upstream(gateway, monkeypatch):
    monkeypatch.setenv('WOOW_BUDGET_ADMISSION', 'resize')
    result = gateway.request('POST', CHAT, {'model': 'reasoning', 'messages': M, 'max_tokens': 16}, token=TINY)
    assert result.status_code == 200 and len(gateway.upstream.calls) == 1


def test_unpriced_allow_warns(gateway, litellm_log):
    result = gateway.request('POST', CHAT, {'model': 'unpriced', 'messages': M}, token=TINY)
    assert result.status_code == 200 and len(gateway.upstream.calls) == 1
    assert litellm_log.messages('Woow event=unpriced_budgeted_request model=unpriced')


def test_unpriced_block_budgeted_keys(gateway, monkeypatch):
    monkeypatch.setenv('WOOW_BUDGET_UNPRICED', 'block_budgeted_keys')
    result = gateway.request('POST', CHAT, {'model': 'unpriced', 'messages': M}, token=TINY)
    assert result.status_code == 400 and gateway.upstream.calls == [] and gateway.spend == []
    assert result.json()['error']['param'] == 'model' and result.json()['error']['type'] == 'invalid_request_error'
    assert "Model 'unpriced' has no price configured" in message_of(result)
    unbudgeted = gateway.request('POST', CHAT, {'model': 'unpriced', 'messages': M})
    assert unbudgeted.status_code == 200


def test_unbudgeted_key_unaffected(gateway, litellm_log):
    for model in ('reasoning', 'unpriced'):
        assert gateway.request('POST', CHAT, {'model': model, 'messages': M, 'max_tokens': 16}).status_code == 200
    assert len(gateway.upstream.calls) == 2
    assert not litellm_log.messages('Woow event=unpriced_budgeted_request')


# P2: reasoning routes ------------------------------------------------------------------------
def test_reasoning_effort_forwarded_for_model_not_in_cost_map(gateway):
    result = gateway.request('POST', CHAT, {'model': 'newmodel', 'messages': M, 'reasoning_effort': 'low'})
    assert result.status_code == 200 and gateway.upstream.calls[0]['body'].get('reasoning_effort') == 'low'


def test_route_default_reasoning_forwarded(gateway):
    assert gateway.request('POST', CHAT, {'model': 'routedefault', 'messages': M}).status_code == 200
    assert gateway.upstream.calls[0]['body'].get('reasoning') == {'enabled': False}


def test_client_reasoning_object_overrides_route_default(gateway):
    reasoning = {'enabled': True, 'max_tokens': 64}
    assert gateway.request('POST', CHAT, {'model': 'routedefault', 'messages': M, 'reasoning': reasoning}).status_code == 200
    assert gateway.upstream.calls[0]['body'].get('reasoning') == reasoning


def test_empty_content_length_is_passed_through(gateway):
    gateway.upstream.content, gateway.upstream.finish, gateway.upstream.usage = '', 'length', REASONING_USAGE
    result = gateway.request('POST', CHAT, {'model': 'newmodel', 'messages': M, 'max_tokens': 16})
    choice = result.json()['choices'][0]
    assert result.status_code == 200 and choice['finish_reason'] == 'length' and not choice['message']['content']
    assert result.json()['usage']['completion_tokens_details']['reasoning_tokens'] == 367
