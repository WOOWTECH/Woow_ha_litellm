"""The shared Woow plugin (0.3.14) on top of the add-on's patched LiteLLM: nothing is applied twice and
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
ADDON_USAGE = '900'  # WOOW_USAGE_LIMIT_COOLDOWN_MAX_SECONDS as the add-on writes it
SWITCHES = (*ADDON_ENV, 'WOOW_BUDGET_UNPRICED', 'WOOW_BUDGET_ADMISSION', 'WOOW_REASONING_MODEL_PATTERNS')


def plugin_dir():
    """The plugin copy of the image under test (/usr/share/...); in a source checkout, the repo's copy."""
    candidates = [os.environ.get('WOOW_PLUGIN_DIR'), '/usr/share/woow-litellm/py']
    here = Path(__file__).resolve()
    if len(here.parents) > 2:  # the image run mounts this file at /contracts: no checkout around it
        candidates.append(str(here.parents[2] / 'litellm/rootfs/usr/share/woow-litellm/py'))
    for candidate in candidates:
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


# ── 0.3.14: an exhausted ChatGPT quota keeps its account out of the rotation (HA router defaults) ──────────
# The add-on's config sets no router_settings (tests/test_woow_plugin_ha_env.py pins that), so the proxy
# builds LiteLLM's Router with its own defaults: num_retries 2, no allowed_fails, cooldown_time 5 s. A 429
# cools the deployment down at once in a group with alternatives; the Retry-After header sets how long.
USAGE_ENV = 'WOOW_USAGE_LIMIT_COOLDOWN_MAX_SECONDS'
WEEKLY = json.dumps({'error': {'type': 'usage_limit_reached', 'message': 'The usage limit has been reached',
                               'plan_type': 'pro', 'resets_at': 1893456000, 'limit_window_minutes': 10080,
                               'resets_in_seconds': 259200}})


def _synthetic_jwt(core, label):
    import base64
    claims = {'exp': 4102444800, core.AUTH_CLAIM: {'chatgpt_account_id': label}}
    return 'synthetic.' + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=') + '.unsigned'


class ChatGPTUpstream:
    """The ChatGPT backend at the http boundary: account a is out of quota, account b is reached."""

    def __init__(self, monkeypatch, tmp_path, env=None, headers=None, text=WEEKLY, status=429):
        import types
        from litellm.llms.chatgpt.chat.transformation import ChatGPTConfig
        from litellm.llms.chatgpt.responses.transformation import ChatGPTResponsesAPIConfig
        from litellm.llms.custom_httpx import http_handler

        # The build's runtime policy (init-woow-env writes it): subscription deployments load only when enabled.
        from litellm import woow_subscription_policy
        policy = tmp_path / 'chatgpt-subscription'
        policy.write_bytes(b'enabled\n')
        monkeypatch.setattr(woow_subscription_policy, 'POLICY_PATH', policy)
        # LiteLLM's ChatGPT Authenticator creates its token directory when the provider config is built; the
        # image's HOME is read-only, so point it at a scratch directory (nothing is read or written there).
        monkeypatch.setenv('CHATGPT_TOKEN_DIR', str(tmp_path / 'chatgpt-tokens'))
        monkeypatch.syspath_prepend(plugin_dir())
        import woow_chatgpt as core
        for name in SWITCHES:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(USAGE_ENV, raising=False)
        for name, value in (env or {}).items():
            monkeypatch.setenv(name, value)
        # The provider seams exactly as the plugin's import hook installs them; restored after the test.
        for cls, names in ((ChatGPTConfig, ('_get_openai_compatible_provider_info', 'validate_environment', 'get_error_class')),
                           (ChatGPTResponsesAPIConfig, ('validate_environment', 'get_error_class', 'transform_responses_api_request'))):
            for name in names:
                monkeypatch.setattr(cls, name, getattr(cls, name))
            monkeypatch.setattr(cls, '_woow_patched', False, raising=False)
        core._patch_chat(types.SimpleNamespace(ChatGPTConfig=ChatGPTConfig))
        core._patch_responses(types.SimpleNamespace(ChatGPTResponsesAPIConfig=ChatGPTResponsesAPIConfig))
        monkeypatch.setattr(litellm, 'credential_list', [])
        monkeypatch.setattr(core, 'authenticator', lambda account: types.SimpleNamespace(
            get_access_token=lambda: _synthetic_jwt(core, 'acct-' + account), get_account_id=lambda: 'acct-' + account))

        self.seen = []
        self.sleeps = []
        self.reached = 'synthetic: the healthy account was reached'
        exhausted = ('a',)

        async def boundary(handler, url, *args, **kwargs):
            sent = {k.lower(): v for k, v in (kwargs.get('headers') or {}).items()}
            account = (sent.get('chatgpt-account-id') or '').replace('acct-', '')
            self.seen.append(account)
            if account in exhausted:
                request = httpx.Request('POST', url)
                raise httpx.HTTPStatusError('upstream error', request=request,
                                            response=httpx.Response(status, text=text, headers=headers or {}, request=request))
            raise RuntimeError(self.reached)

        monkeypatch.setattr(http_handler.AsyncHTTPHandler, 'post', boundary)
        real_sleep = asyncio.sleep

        async def fast_sleep(delay, *args, **kwargs):
            self.sleeps.append(delay)
            return await real_sleep(0)
        monkeypatch.setattr(asyncio, 'sleep', fast_sleep)
        # Every Router adds its cooldown/usage callbacks to LiteLLM's process-wide lists, which stop taking new
        # entries at 100 (MAX_CALLBACKS): after the hundreds of Routers other contract files build, this Router
        # would never be told about a failure. Start from empty lists (restored after the test).
        for name in ('_async_success_callback', 'success_callback', '_async_failure_callback', 'failure_callback'):
            monkeypatch.setattr(litellm, name, [])
        # No num_retries / allowed_fails / cooldown_time: what the proxy builds from the add-on's config.
        self.router = litellm.Router(model_list=[
            {'model_name': 'pair', 'litellm_params': {'model': 'chatgpt/gpt-5.5', 'api_key': 'woow-chatgpt:a'},
             'model_info': {'id': 'dep-a', 'mode': 'responses'}},
            {'model_name': 'pair', 'litellm_params': {'model': 'chatgpt/gpt-5.5', 'api_key': 'woow-chatgpt:b'},
             'model_info': {'id': 'dep-b', 'mode': 'responses'}},
            {'model_name': 'solo', 'litellm_params': {'model': 'chatgpt/gpt-5.5', 'api_key': 'woow-chatgpt:a'},
             'model_info': {'id': 'dep-solo', 'mode': 'responses'}},
        ])

    def call(self, model='pair'):
        async def run():
            try:
                await self.router.acompletion(model=model, messages=[{'role': 'user', 'content': 'synthetic'}])
            except Exception as exc:  # noqa: BLE001
                return exc
        return asyncio.run(run())

    def until_a_was_picked(self):
        """Call the pair (simple-shuffle) until a request reached the exhausted account (first pick or on a
        retry); returns where that request's upstream calls start in `seen`, and its final failure."""
        for _ in range(40):
            before = len(self.seen)
            failure = self.call()
            if 'a' in self.seen[before:]:
                return before, failure
        raise AssertionError(f'account a was never picked: seen={self.seen}')

    def exhausted_account_first(self, monkeypatch):
        """Let simple-shuffle pick account a whenever a is a candidate (only this test's Router is affected).
        A random pick can leave a for a request's last attempt ([b, b, a], about one run in seven): with no
        retry left that request ends on a's 429. Picked first, a request meets it with both retries left."""
        import random
        import types
        from litellm.router_strategy import simple_shuffle

        def choice(candidates):
            return next((d for d in candidates if d['model_info']['id'] == 'dep-a'), None) or random.choice(candidates)
        monkeypatch.setattr(simple_shuffle, 'random', types.SimpleNamespace(choice=choice, choices=random.choices))

    def cooldown(self, *ids):
        return self.router.cooldown_cache.get_min_cooldown(list(ids), None)

    def active(self, *ids):
        return asyncio.run(self.router.cooldown_cache.async_get_active_cooldowns(list(ids), None))


def test_ha_router_runs_on_litellm_defaults(monkeypatch, tmp_path):
    """The assumptions the mechanism rests on; a LiteLLM bump or an added router setting fails here."""
    from litellm.router_utils.cooldown_handlers import _is_allowed_fails_set_on_router
    up = ChatGPTUpstream(monkeypatch, tmp_path)
    router = up.router
    assert router.num_retries == 2 and router.cooldown_time == 5
    assert router.allowed_fails_policy is None and _is_allowed_fails_set_on_router(litellm_router_instance=router) is False
    assert not router.disable_cooldowns


def test_exhausted_account_is_cooled_for_900_s_and_the_same_request_moves_on(monkeypatch, tmp_path):
    """With the add-on's own environment (900) and the router defaults: the request that hit the exhausted
    account is retried on the other one inside the same call (no client-visible 429), and the exhausted
    account is not probed again while it cools."""
    up = ChatGPTUpstream(monkeypatch, tmp_path, env={USAGE_ENV: ADDON_USAGE})
    up.exhausted_account_first(monkeypatch)
    before, failure = up.until_a_was_picked()
    mine = up.seen[before:]
    assert mine[0] == 'a' and mine.count('a') == 1 and 'b' in mine  # a was tried once, the request ended on the other account
    assert up.reached in str(failure) and getattr(failure, 'status_code', None) != 429
    assert up.cooldown('dep-a') == 900 and 'dep-a' in [i for i, _ in up.active('dep-a')]
    after = len(up.seen)
    for _ in range(12):
        up.call()
    assert 'a' not in up.seen[after:]
    assert all(delay == 0 for delay in up.sleeps)        # a healthy alternative: no back-off sleep


def test_unset_environment_gives_the_same_900(monkeypatch, tmp_path):
    up = ChatGPTUpstream(monkeypatch, tmp_path)                      # the plugin's own default, nothing written
    up.until_a_was_picked()
    assert up.cooldown('dep-a') == 900


def test_zero_restores_litellms_5_second_cooldown(monkeypatch, tmp_path):
    up = ChatGPTUpstream(monkeypatch, tmp_path, env={USAGE_ENV: '0'})
    up.until_a_was_picked()
    assert up.cooldown('dep-a') == 5


@pytest.mark.parametrize('value,expected', [('120', 120), ('61', 61), ('86400', 86400)])
def test_env_vars_value_is_the_cap(monkeypatch, tmp_path, value, expected):
    up = ChatGPTUpstream(monkeypatch, tmp_path, env={USAGE_ENV: value})
    up.until_a_was_picked()
    assert up.cooldown('dep-a') == expected


def test_an_upstream_retry_after_and_ordinary_rate_limits_are_left_alone(monkeypatch, tmp_path):
    up = ChatGPTUpstream(monkeypatch, tmp_path, headers={'retry-after': '7'})
    up.until_a_was_picked()
    assert up.cooldown('dep-a') == 7
    ordinary = json.dumps({'error': {'type': 'rate_limit_exceeded', 'resets_in_seconds': 3}})
    up = ChatGPTUpstream(monkeypatch, tmp_path, text=ordinary)
    up.until_a_was_picked()
    assert up.cooldown('dep-a') == 5


def test_a_single_account_group_is_never_cooled_and_keeps_litellms_retries(monkeypatch, tmp_path):
    """LiteLLM's rule, unchanged: no cooldown for the only deployment of a group; the default 2 retries
    still reach the exhausted account (3 upstream calls) -- the hint changes nothing here."""
    up = ChatGPTUpstream(monkeypatch, tmp_path)
    failure = up.call('solo')
    assert getattr(failure, 'status_code', None) == 429
    assert up.active('dep-solo') == [] and up.seen == ['a', 'a', 'a']
