"""The shared Woow plugin (0.3.13) on top of the add-on's patched LiteLLM: nothing is applied twice and
nothing is lost. The plugin hooks wrap the same seams the build patches change (ProxyLogging hooks, the
request-processing error handler); these contracts run the REAL chain -- API errors, upstream guard and
M4 batch 1 installed in the image, plus the synced plugin files wrapped around them -- on the proxy app,
with the provider boundary at httpx's transport. The environment is what the add-on writes
(woow_init.litellm_env). Hooks are installed per test and removed again.
"""
import asyncio
import json
import os
from pathlib import Path

import httpx
import litellm
import pytest
from litellm.proxy import common_request_processing as processing
from litellm.proxy import proxy_server as server
from litellm.proxy import utils as proxy_utils
from litellm.proxy._types import LiteLLM_UserTable, LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.common_utils.user_api_key_cache import UserApiKeyCache
from litellm.proxy.hooks.proxy_track_cost_callback import _ProxyDBLogger
from litellm.proxy.utils import hash_token

MASTER = 'sk-plugin-composition-master-fixture'
PLAIN = 'sk-plugin-composition-plain-fixture'
BUDGET = 'sk-plugin-composition-budget-fixture'
SECRET = 'plugin-composition-provider-secret-sentinel'
M = [{'role': 'user', 'content': 'offline'}]
PRICE = {'input_cost_per_token': 1e-6, 'output_cost_per_token': 2e-6}
# What woow_init.litellm_env writes for the plugin (tests/test_woow_plugin_ha_env.py keeps it honest).
ADDON_ENV = {
    'WOOW_REASONING_MIN_OUTPUT_MODE': 'raise', 'WOOW_REASONING_MIN_OUTPUT_TOKENS': '512',
    'WOOW_UNPRICED_MODEL_MODE': 'warn', 'WOOW_FORWARD_RETRY_AFTER': 'true', 'WOOW_UPSTREAM_TIMEOUT_STATUS': '504',
}
SWITCHES = (*ADDON_ENV, 'WOOW_BUDGET_UNPRICED', 'WOOW_BUDGET_ADMISSION', 'WOOW_REASONING_MODEL_PATTERNS')


def plugin_dir():
    for candidate in (os.environ.get('WOOW_PLUGIN_DIR'), '/usr/share/woow-litellm/py',
                      str(Path(__file__).resolve().parents[2] / 'litellm/rootfs/usr/share/woow-litellm/py')):
        if candidate and (Path(candidate) / 'woow_chatgpt.py').is_file():
            return candidate
    raise RuntimeError('plugin copy not found')


def _deployment(name, host, model='openai/woowfake/model', **info):
    return {'model_name': name, 'model_info': {'id': 'dep-' + name, **info},
            'litellm_params': {'model': model, 'api_key': SECRET, 'api_base': f'https://{host}.upstream.invalid/v1'}}


MODEL_LIST = [
    _deployment('rate-limited', 'e429', model='openai/woowfake/rl', **PRICE),
    _deployment('overloaded', 'e503', model='openai/woowfake/busy', **PRICE),
    _deployment('hung', 'etimeout', model='openai/woowfake/hung', **PRICE),
    _deployment('reasoner', 'ok', model='openai/woowfake/reasoner', supports_reasoning=True, **PRICE),
    _deployment('plain', 'ok', model='openai/woowfake/plain', **PRICE),
    _deployment('unpriced', 'ok', model='openai/woowfake/unpriced'),
]


def _completion(content='OK'):
    return {'id': 'cmpl-1', 'object': 'chat.completion', 'created': 1, 'model': 'woowfake',
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}


class Gateway:
    def __init__(self):
        self.upstream_bodies = []

    def handle(self, request):
        self.upstream_bodies.append(json.loads(request.content or b'{}'))
        host = request.url.host
        if host.startswith('e429'):
            return httpx.Response(429, request=request, headers={'retry-after': '60', 'x-should-retry': 'true', 'cf-ray': 'FAKE'},
                                  json={'error': {'message': 'slow down', 'type': 'rate_limit_error'}})
        if host.startswith('e503'):
            return httpx.Response(503, request=request, headers={'retry-after': 'Wed, 21 Oct 2026 07:28:00 GMT'},
                                  json={'error': {'message': 'busy'}})
        if host.startswith('etimeout'):
            raise httpx.ReadTimeout('timed out', request=request)
        return httpx.Response(200, request=request, json=_completion())

    def call(self, model, token=PLAIN, **extra):
        async def send():
            transport = httpx.ASGITransport(app=server.app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url='http://offline.invalid', timeout=60) as client:
                result = await client.post('/v1/chat/completions', headers={'Authorization': 'Bearer ' + token},
                                           json={'model': model, 'messages': M, **extra})
                await asyncio.sleep(0.3)
                return result
        result = asyncio.run(send())
        assert SECRET not in result.text and MASTER not in result.text
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
    for key, budget in ((PLAIN, None), (BUDGET, 5.0)):
        cache.set_cache(key=hash_token(key), model_type=UserAPIKeyAuth, value=UserAPIKeyAuth(
            api_key=hash_token(key), token=hash_token(key), user_role=LitellmUserRoles.INTERNAL_USER,
            models=[], user_id='offline-user', metadata={}, max_budget=budget, spend=0.0))
    monkeypatch.setattr(server, 'user_api_key_cache', cache)
    monkeypatch.setattr(server, 'prisma_client', object())

    async def floor(counter_key, **kwargs):
        return 0.0
    monkeypatch.setattr(server, '_authoritative_floor_spend', floor)
    server.spend_counter_cache.in_memory_cache.flush_cache()
    router = litellm.Router(model_list=[json.loads(json.dumps(d)) for d in MODEL_LIST], num_retries=0, timeout=5)
    monkeypatch.setattr(server, 'llm_router', router)
    monkeypatch.setattr(server, 'llm_model_list', router.model_list)
    gw = Gateway()
    monkeypatch.setattr(litellm, 'disable_aiohttp_transport', True)
    monkeypatch.setattr(litellm, 'drop_params', True)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', lambda transport, request: _async(gw.handle, request))
    monkeypatch.setattr(server.proxy_logging_obj.db_spend_update_writer, 'update_database', _noop)
    monkeypatch.setattr(litellm, 'callbacks', [_ProxyDBLogger()])
    for name in SWITCHES:
        monkeypatch.delenv(name, raising=False)
    for name, value in ADDON_ENV.items():
        monkeypatch.setenv(name, value)
    # The plugin's seams, installed exactly as its import hook does and restored after the test.
    monkeypatch.syspath_prepend(plugin_dir())
    for cls, names in ((proxy_utils.ProxyLogging, ('pre_call_hook', 'post_call_success_hook')),
                       (processing.ProxyBaseLLMRequestProcessing, ('_handle_llm_api_exception',))):
        for name in names:
            monkeypatch.setattr(cls, name, getattr(cls, name))
    import woow_chatgpt
    woow_chatgpt._patch_proxy_logging(proxy_utils)
    woow_chatgpt._patch_request_processing(processing)
    yield gw
    server.spend_counter_cache.in_memory_cache.flush_cache()


async def _async(fn, request):
    return fn(request)


async def _noop(**kwargs):
    return None


def plugin_headers(result):
    return {k: v for k, v in result.headers.items() if k.lower().startswith(('x-woow', 'retry-after', 'x-should-retry'))}


def test_upstream_retry_hints_reach_the_caller_and_the_error_stays_masked(gateway):
    result = gateway.call('rate-limited')
    assert result.status_code == 429
    assert plugin_headers(result) == {'retry-after': '60', 'x-should-retry': 'true'}
    error = result.json()['error']
    assert error['code'] == '429' and error['type'] == 'throttling_error'
    # The build patches' own rules still hold: only rate-limit/retry-after provider headers are forwarded.
    assert result.headers['llm_provider-retry-after'] == '60'
    assert 'llm_provider-cf-ray' not in result.headers


def test_http_date_retry_after_is_forwarded(gateway):
    result = gateway.call('overloaded')
    assert result.status_code == 503
    assert plugin_headers(result) == {'retry-after': 'Wed, 21 Oct 2026 07:28:00 GMT'}


def test_upstream_timeout_is_a_504_envelope_and_408_restores(gateway, monkeypatch):
    result = gateway.call('hung')
    error = result.json()['error']
    assert (result.status_code, error['type'], error['code']) == (504, 'timeout', '504')
    monkeypatch.setenv('WOOW_UPSTREAM_TIMEOUT_STATUS', '408')
    result = gateway.call('hung')
    error = result.json()['error']
    assert (result.status_code, error['type'], error['code']) == (408, 'invalid_request_error', '408')


def test_forwarding_switch_off_leaves_the_builds_answer(gateway, monkeypatch):
    monkeypatch.setenv('WOOW_FORWARD_RETRY_AFTER', 'false')
    result = gateway.call('rate-limited')
    assert result.status_code == 429 and plugin_headers(result) == {}


def test_reasoning_route_gets_its_output_limit_raised(gateway):
    result = gateway.call('reasoner', max_tokens=16)
    assert result.status_code == 200
    assert result.headers['x-woow-max-tokens-raised'] == '16->512'
    assert any(512 in (b.get('max_completion_tokens'), b.get('max_tokens')) for b in gateway.upstream_bodies)
    gateway.upstream_bodies.clear()
    result = gateway.call('reasoner', max_tokens=16, reasoning_effort='none')
    assert 'x-woow-max-tokens-raised' not in result.headers
    assert not any(512 in (b.get('max_completion_tokens'), b.get('max_tokens')) for b in gateway.upstream_bodies)
    result = gateway.call('plain', max_tokens=16)
    assert 'x-woow-max-tokens-raised' not in result.headers


def test_reasoning_floor_off_switch(gateway, monkeypatch):
    monkeypatch.setenv('WOOW_REASONING_MIN_OUTPUT_MODE', 'off')
    result = gateway.call('reasoner', max_tokens=16)
    assert result.status_code == 200 and plugin_headers(result) == {}


def test_budgeted_key_on_an_unpriced_model_is_warned_not_stopped(gateway):
    assert 'x-woow-warning' not in gateway.call('unpriced').headers  # no budget: nothing to bypass
    assert 'x-woow-warning' not in gateway.call('plain', token=BUDGET).headers  # priced
    result = gateway.call('unpriced', token=BUDGET)
    assert result.status_code == 200
    assert result.headers['x-woow-warning'] == 'model_unpriced_budget_not_enforced'


def test_the_builds_unpriced_block_comes_first(gateway, monkeypatch):
    monkeypatch.setenv('WOOW_BUDGET_UNPRICED', 'block_budgeted_keys')
    result = gateway.call('unpriced', token=BUDGET)
    assert result.status_code == 400
    assert "Model 'unpriced' has no price configured" in result.json()['error']['message']
    assert 'x-woow-warning' not in result.headers


def test_unpriced_reject_mode_of_the_plugin(gateway, monkeypatch):
    monkeypatch.setenv('WOOW_UNPRICED_MODEL_MODE', 'reject')
    result = gateway.call('unpriced', token=BUDGET)
    assert result.status_code == 400 and result.json()['error']['param'] == 'model'
    assert gateway.call('unpriced').status_code == 200
