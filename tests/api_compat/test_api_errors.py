"""M4 API error contracts (apply_api_errors.py) on the real proxy app; nothing leaves the process.

The provider boundary is httpx's transport, answering like OpenRouter (including its usage
chunk after the finish chunk). The spend-log boundary is the real _ProxyDBLogger writing to a
recording DB writer. Auth uses the real cache-backed path; no database or network I/O.
"""
import asyncio
import json
import httpx
import pytest
import litellm
from litellm.proxy import proxy_server as server
from litellm.proxy._types import LiteLLM_UserTable, LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.common_utils.user_api_key_cache import UserApiKeyCache
from litellm.proxy.hooks.proxy_track_cost_callback import _ProxyDBLogger
from litellm.proxy.utils import hash_token

MASTER = 'sk-api-errors-master-fixture'
VIRTUAL = 'sk-api-errors-virtual-fixture'
MOCK_KEY = 'sk-api-errors-mock-allowed-fixture'
CANARY = 'api-errors-provider-secret-sentinel'
M = [{'role': 'user', 'content': 'offline'}]
MOCK_DISABLED = 'Mock testing request params are disabled on this proxy'
GENERIC_500 = {'message': 'Internal server error', 'type': 'internal_server_error', 'param': None, 'code': '500'}


def _sse(chunks):
    return ''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + 'data: [DONE]\n\n'


def _openrouter_stream():
    base = {'id': 'gen-offline', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'vendor/model:free'}
    def chunk(delta, finish=None, **extra):
        return dict(base, choices=[{'index': 0, 'delta': delta, 'finish_reason': finish, 'native_finish_reason': finish}], **extra)
    return [chunk({'role': 'assistant', 'content': ''})] + [chunk({'role': 'assistant', 'content': c}) for c in '一二三'] + [
        chunk({'role': 'assistant', 'content': ''}, 'stop'),
        chunk({'role': 'assistant', 'content': ''}, usage={'prompt_tokens': 5, 'completion_tokens': 3, 'total_tokens': 8})]


class Upstream:
    """OpenAI-compatible provider at the transport boundary; https://broken.invalid fails."""
    def __init__(self):
        self.calls = []

    async def handle(self, transport, request):
        body = json.loads(request.content or b'{}')
        self.calls.append(body)
        if request.url.host == 'broken.invalid':
            return httpx.Response(503, json={'error': {'message': 'provider overloaded', 'code': 503}}, request=request)
        if body.get('stream'):
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                  content=_sse(_openrouter_stream()).encode(), request=request)
        return httpx.Response(200, request=request, json={
            'id': 'gen-offline', 'object': 'chat.completion', 'created': 1, 'model': 'vendor/model:free',
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'OK'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}})


class Gateway:
    def __init__(self, app, upstream, spend):
        self.app, self.upstream, self.spend = app, upstream, spend

    def request(self, method, path, body=None, raw=None, token=VIRTUAL):
        async def run():
            transport = httpx.ASGITransport(app=self.app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url='http://offline.invalid') as client:
                headers = {'Authorization': 'Bearer ' + token, 'content-type': 'application/json'}
                content = raw if raw is not None else (json.dumps(body) if body is not None else None)
                result = await client.request(method, path, headers=headers, content=content)
                await asyncio.sleep(0.3)  # let detached success/failure logging tasks finish
                return result
        result = asyncio.run(run())
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
    for key, metadata in ((VIRTUAL, {}), (MOCK_KEY, {'allow_client_mock_response': True})):
        cache.set_cache(key=hash_token(key), model_type=UserAPIKeyAuth, value=UserAPIKeyAuth(
            api_key=hash_token(key), token=hash_token(key), user_role=LitellmUserRoles.INTERNAL_USER,
            models=[], user_id='offline-user', metadata=metadata))
    monkeypatch.setattr(server, 'user_api_key_cache', cache)
    # Every row is already in the real memory cache; any database read would fail loudly.
    monkeypatch.setattr(server, 'prisma_client', object())
    router = litellm.Router(model_list=[
        {'model_name': name, 'litellm_params': {'model': 'openrouter/vendor/model:free', 'api_key': CANARY,
                                                'api_base': f'https://{host}/api/v1'}}
        for name, host in (('free', 'upstream.invalid'), ('broken', 'broken.invalid'))], num_retries=0)
    monkeypatch.setattr(server, 'llm_router', router)
    monkeypatch.setattr(server, 'llm_model_list', router.model_list)
    upstream = Upstream()
    monkeypatch.setattr(litellm, 'disable_aiohttp_transport', True)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request',
                        lambda transport, request: upstream.handle(transport, request))
    spend = []
    async def update_database(**kwargs):
        spend.append(kwargs)
    monkeypatch.setattr(server.proxy_logging_obj.db_spend_update_writer, 'update_database', update_database)
    monkeypatch.setattr(litellm, 'callbacks', [_ProxyDBLogger()])
    return Gateway(server.app, upstream, spend)


def statuses(rows):
    return ['failure' if isinstance(row['completion_response'], Exception) else 'success' for row in rows]


def assert_openai_error(result, status, **expected):
    body = result.json()
    assert result.status_code == status
    assert set(body) == {'error'} and isinstance(body['error']['message'], str)
    assert body['error']['code'] == str(status)
    for key, value in expected.items():
        assert body['error'][key] == value


# API-04.S05 --------------------------------------------------------------------------------
@pytest.mark.parametrize('field,value', [('mock_response', 'woow-mock'), ('mock_tool_calls', [])])
def test_client_mock_field_is_refused_not_sent_upstream(gateway, field, value):
    result = gateway.request('POST', '/v1/chat/completions', {'model': 'free', 'messages': M, field: value})
    assert_openai_error(result, 400, type='invalid_request_error', param=field)
    assert MOCK_DISABLED in result.json()['error']['message']
    assert gateway.upstream.calls == [] and gateway.spend == []


def test_key_allowed_mock_response_is_still_served_without_upstream(gateway):
    result = gateway.request('POST', '/v1/chat/completions',
                             {'model': 'free', 'messages': M, 'mock_response': 'woow-mock'}, token=MOCK_KEY)
    assert result.status_code == 200
    assert result.json()['choices'][0]['message']['content'] == 'woow-mock'
    assert gateway.upstream.calls == []


def test_mock_testing_setting_semantics_are_unchanged(gateway, monkeypatch):
    monkeypatch.setattr(server, 'general_settings', {'dangerously_allow_mock_testing_request_params': True})
    allowed = gateway.request('POST', '/v1/chat/completions',
                              {'model': 'free', 'messages': M, 'mock_testing_rate_limit_error': True})
    assert allowed.status_code == 429 and MOCK_DISABLED not in allowed.text
    # The setting never covered mock_response; only key/team metadata does.
    refused = gateway.request('POST', '/v1/chat/completions', {'model': 'free', 'messages': M, 'mock_response': 'x'})
    assert refused.status_code == 400 and MOCK_DISABLED in refused.text
    assert gateway.upstream.calls == []


# API-04.S06 --------------------------------------------------------------------------------
REJECTED = [
    ('POST', '/v1/chat/completions', {'model': 'free'}, None),
    ('POST', '/v1/chat/completions', None, '{"model":"free","messages":[{"ro'),
    ('POST', '/v1/responses', {'model': 'free'}, None),
    ('POST', '/v1/embeddings', {'model': 'free'}, None),
    ('POST', '/v1/chat/completions', {'model': 'free', 'messages': M, 'mock_response': 'x'}, None),
    ('POST', '/v1/chat/completions', {'model': 'free', 'messages': M, 'mock_testing_fallbacks': True}, None),
    ('POST', '/v1/completions', {'model': 'free'}, None),
    ('POST', '/v1/chat/completions', {'model': 'free', 'messages': M + [{'role': 'tool', 'tool_call_id': 'x', 'content': 'r'}]}, None),
]


@pytest.mark.parametrize('method,path,body,raw', REJECTED)
def test_validation_rejection_writes_no_spend_log_and_calls_no_provider(gateway, method, path, body, raw):
    result = gateway.request(method, path, body, raw)
    assert_openai_error(result, 400)
    assert gateway.upstream.calls == []
    assert gateway.spend == []


def test_provider_failure_and_success_still_write_spend_logs(gateway):
    failed = gateway.request('POST', '/v1/chat/completions', {'model': 'broken', 'messages': M})
    assert failed.status_code == 503 and len(gateway.upstream.calls) == 1
    assert statuses(gateway.spend) == ['failure']
    ok = gateway.request('POST', '/v1/chat/completions', {'model': 'free', 'messages': M})
    assert ok.status_code == 200 and statuses(gateway.spend) == ['failure', 'success']


# API-07.S07 --------------------------------------------------------------------------------
def stream(gateway, include_usage):
    body = {'model': 'free', 'messages': M, 'stream': True}
    if include_usage is not None:
        body['stream_options'] = {'include_usage': include_usage}
    result = gateway.request('POST', '/v1/chat/completions', body)
    assert result.status_code == 200
    data = [line[6:] for line in result.text.splitlines() if line.startswith('data: ')]
    assert data[-1] == '[DONE]' and data.count('[DONE]') == 1
    return [json.loads(item) for item in data[:-1]]


@pytest.mark.parametrize('include_usage', [True, False, None])
def test_finish_reason_is_on_the_last_chunk_with_choices(gateway, include_usage):
    packets = stream(gateway, include_usage)
    with_choices = [p for p in packets if p['choices']]
    assert [p['choices'][0].get('finish_reason') for p in with_choices][-1] == 'stop'
    assert sum(bool(c.get('finish_reason')) for p in with_choices for c in p['choices']) == 1
    assert ''.join(p['choices'][0]['delta'].get('content') or '' for p in with_choices) == '一二三'
    usage = [p for p in packets if p.get('usage')]
    if include_usage:
        assert packets[-1] is usage[0] and usage[0]['choices'] == []
    else:
        assert usage == []


def test_post_finish_usage_still_reaches_client_and_cost_tracking(gateway):
    packets = stream(gateway, True)
    assert packets[-1]['usage']['total_tokens'] == 8 and packets[-1]['usage']['completion_tokens'] == 3
    assert len(gateway.spend) == 1
    assert gateway.spend[0]['completion_response'].usage.total_tokens == 8


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('include_usage', [False, True])
def test_wrapper_drops_post_finish_choice_in_sync_and_async_paths(asynchronous, include_usage):
    """Same OpenRouter chunks through the real parser and CustomStreamWrapper (SDK paths too)."""
    import datetime
    from unittest.mock import AsyncMock, Mock
    from litellm.litellm_core_utils.litellm_logging import Logging
    from litellm.litellm_core_utils.streaming_handler import CustomStreamWrapper
    from litellm.llms.openrouter.chat.transformation import OpenRouterChatCompletionStreamingHandler
    parser = OpenRouterChatCompletionStreamingHandler(streaming_response=iter(()), sync_stream=True)
    chunks = [parser.chunk_parser(c) for c in _openrouter_stream()]
    async def upstream():
        for chunk in chunks:
            yield chunk
    log = Logging(model='vendor/model:free', messages=M, stream=True, call_type='acompletion' if asynchronous else 'completion',
                  start_time=datetime.datetime.now(), litellm_call_id='offline-call', function_id='offline-function')
    log.success_handler = Mock()
    log.dispatch_success_handlers = AsyncMock()
    wrapper = CustomStreamWrapper(completion_stream=upstream() if asynchronous else iter(chunks), model='vendor/model:free',
                                  logging_obj=log, custom_llm_provider='openrouter', stream_options={'include_usage': include_usage})
    wrapper.async_cache_streaming_response = AsyncMock()
    wrapper.run_success_logging_and_cache_storage = Mock()
    async def consume():
        return [c async for c in wrapper]
    out = asyncio.run(consume()) if asynchronous else list(wrapper)
    with_choices = [c for c in out if c.choices]
    assert with_choices[-1].choices[0].finish_reason == 'stop'
    assert sum(bool(ch.finish_reason) for c in with_choices for ch in c.choices) == 1
    usage = [c for c in out if getattr(c, 'usage', None) is not None]
    assert len(usage) == int(include_usage)
    if include_usage:
        assert out[-1] is usage[0] and out[-1].choices == [] and out[-1].usage.total_tokens == 8


# API-11.S02 --------------------------------------------------------------------------------
def assistant_call(*ids):
    return {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': i, 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{}'}} for i in ids]}


def tool(call_id):
    message = {'role': 'tool', 'content': 'result'}
    if call_id is not None:
        message['tool_call_id'] = call_id
    return message


@pytest.mark.parametrize('messages,index', [
    (M + [assistant_call('call_a'), tool('call_mismatch')], 2),
    (M + [assistant_call('call_a'), tool(None)], 2),
    (M + [tool('call_a'), assistant_call('call_a')], 1),
    (M + [assistant_call('call_a'), tool('call_a'), tool('call_a2')], 3),
])
def test_tool_call_id_mismatch_is_a_400_before_provider(gateway, messages, index):
    result = gateway.request('POST', '/v1/chat/completions', {'model': 'free', 'messages': messages})
    assert_openai_error(result, 400, type='invalid_request_error', param=f'messages.[{index}].tool_call_id')
    assert gateway.upstream.calls == [] and gateway.spend == []


@pytest.mark.parametrize('messages', [
    M + [assistant_call('call_a'), tool('call_a')],
    M + [assistant_call('call_a', 'call_b'), tool('call_b'), tool('call_a')],
    M + [assistant_call('call_a'), tool('call_a'), {'role': 'user', 'content': 'again'}, assistant_call('call_c'), tool('call_a')],
])
def test_matching_tool_call_ids_are_forwarded(gateway, messages):
    result = gateway.request('POST', '/v1/chat/completions', {'model': 'free', 'messages': messages})
    assert result.status_code == 200
    assert gateway.upstream.calls[-1]['messages'][-1]['tool_call_id'] == messages[-1]['tool_call_id']


# API-22.S01 --------------------------------------------------------------------------------
@pytest.mark.parametrize('method,path,raw,status', [
    ('GET', '/v1/models/woow-nonexistent', None, 404),
    ('GET', '/v1/chat/completions', None, 405),
    ('GET', '/chat/completions', None, 405),
    ('GET', '/v1/responses', None, 405),
    ('GET', '/v1/no-such-endpoint', None, 404),
    ('GET', '/v1/models?return_wildcard_routes=not-a-bool', None, 422),
    ('POST', '/v1/chat/completions', '[1,2]', 500),
    ('POST', '/v1/responses', '[]', 500),
])
def test_openai_routes_use_the_openai_error_envelope(gateway, method, path, raw, status):
    result = gateway.request(method, path, raw=raw, token=MASTER)
    assert_openai_error(result, status)
    if status == 500:
        assert result.json()['error']['message'] == 'Internal server error'


@pytest.mark.parametrize('method,path,body,status,expected', [
    ('GET', '/v1/messages', None, 405, {'detail': 'Method Not Allowed'}),
    ('GET', '/no-such-root', None, 404, {'detail': 'Not Found'}),
    ('GET', '/model/info?litellm_model_id=missing', None, 400, {'detail': {'error': 'Model id = missing not found on litellm proxy'}}),
])
def test_other_routes_keep_their_error_formats(gateway, method, path, body, status, expected):
    result = gateway.request(method, path, body, token=MASTER)
    assert result.status_code == status and result.json() == expected


def test_management_validation_error_format_is_unchanged(gateway):
    result = gateway.request('POST', '/key/generate', {'duration': {}}, token=MASTER)
    assert result.status_code == 422 and set(result.json()) == {'detail'}
    assert result.json()['detail'][0]['loc'] == ['body', 'duration']


# API-22.S02 --------------------------------------------------------------------------------
def test_completions_without_prompt_is_a_400(gateway):
    result = gateway.request('POST', '/v1/completions', {'model': 'free'})
    assert_openai_error(result, 400, type='invalid_request_error', param='prompt')
    assert "'prompt'" in result.json()['error']['message'] and 'KeyError' not in result.text


@pytest.mark.parametrize('path', ['/v1/chat/completions', '/v1/completions'])
def test_unhandled_exception_is_the_generic_500(gateway, monkeypatch, path):
    import litellm.proxy.common_request_processing as processing
    async def broken_route(**kwargs):
        raise KeyError('woow-internal-detail')
    monkeypatch.setattr(processing, 'route_request', broken_route)
    result = gateway.request('POST', path, {'model': 'free', 'messages': M, 'prompt': 'x'})
    assert result.status_code == 500 and result.json() == {'error': GENERIC_500}
    assert 'woow-internal-detail' not in result.text and 'Traceback' not in result.text


def test_provider_errors_keep_their_status_and_message(gateway):
    result = gateway.request('POST', '/v1/chat/completions', {'model': 'broken', 'messages': M})
    assert_openai_error(result, 503)
    assert result.json()['error']['message'] != 'Internal server error'
    assert 'provider overloaded' in result.json()['error']['message']
