"""Lean tests for the stdlib-only M4 batch 1 helper (no LiteLLM installed).

The helper builds on the upstream guard's woow_provider_errors.redact_secrets; here that module is
loaded from the reviewed source and registered under its installed name, as in the image.
"""
import importlib.util
import json
from pathlib import Path
import sys
import types
import pytest

REPO = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


provider_errors = load('woow_provider_errors_for_client_guard', REPO / 'litellm/patches/woow_provider_errors.py')
guard = load('woow_client_guard', REPO / 'litellm/patches/woow_client_guard.py')

PROVIDER_KEY = 'sk-fx-good-echoauth-7f3a91'
RAW_KEY = 'woowRAWsecretVALUE12345'  # no recognisable shape: only its value can mask it
JWT = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ3b293LWZha2UifQ.c2lnbmF0dXJlLWZha2U'
TOKEN = 'opaqueCHATGPTaccess-token-0123456789'


@pytest.fixture(autouse=True)
def installed(monkeypatch):
    monkeypatch.setitem(sys.modules, guard._GUARD_MODULE, provider_errors)
    for name in (guard._PROXY_SERVER_MODULE, guard._LITELLM_REDACTION_MODULE, 'litellm', guard._CHATGPT_UTILS_MODULE):
        monkeypatch.delitem(sys.modules, name, raising=False)
    guard._REMEMBERED.clear()
    yield
    guard._REMEMBERED.clear()


def with_router(monkeypatch, model_list, credentials=()):
    proxy = types.ModuleType(guard._PROXY_SERVER_MODULE)
    proxy.llm_router = types.SimpleNamespace(model_list=model_list)
    monkeypatch.setitem(sys.modules, guard._PROXY_SERVER_MODULE, proxy)
    litellm = types.ModuleType('litellm')
    litellm.credential_list = [types.SimpleNamespace(credential_values=values) for values in credentials]
    monkeypatch.setitem(sys.modules, 'litellm', litellm)


# R03 ------------------------------------------------------------------------------------------
@pytest.mark.parametrize('text', [
    f'OpenAIException - {{"error": {{"message": "received Authorization: Bearer {PROVIDER_KEY}"}}}}',
    f"Error code: 401 - {{'error': {{'message': 'received Authorization: Bearer {PROVIDER_KEY}'}}}}",
    f'upstream echoed {PROVIDER_KEY} without a prefix',
    f'received Authorization: Bearer {JWT}',
    f'token {JWT} expired',
])
def test_shaped_credentials_are_masked_by_the_guard_rules(text):
    masked = guard.client_error_message(text)
    assert PROVIDER_KEY not in masked and JWT not in masked
    assert guard.client_error_message(masked) == masked  # idempotent across exits


def test_deployment_and_credential_values_are_masked_by_value(monkeypatch, tmp_path):
    monkeypatch.setenv('WOOW_FAKE_ENV_KEY', 'envSECRETvalue-9876543')
    with_router(monkeypatch, [
        {'model_name': 'raw', 'litellm_params': {'model': 'openai/x', 'api_key': RAW_KEY, 'max_tokens': 10,
                                                 'litellm_credential_name': 'shared-cred'}},
        {'model_name': 'env', 'litellm_params': {'model': 'openai/y', 'api_key': 'os.environ/WOOW_FAKE_ENV_KEY'}},
        {'model_name': 'short', 'litellm_params': {'model': 'openai/z', 'api_key': 'abc'}},
    ], credentials=[{'api_key': 'credSECRETvalue-112233', 'api_base': 'https://api.example.com'}])
    text = (f'key {RAW_KEY} rejected; env envSECRETvalue-9876543; cred credSECRETvalue-112233; '
            'credential shared-cred; abc is fine; base https://api.example.com')
    masked = guard.client_error_message(text)
    for secret in (RAW_KEY, 'envSECRETvalue-9876543', 'credSECRETvalue-112233'):
        assert secret not in masked
    # Names, short values and public hosts are not secrets.
    assert 'shared-cred' in masked and 'abc is fine' in masked and 'https://api.example.com' in masked


def test_remembered_access_tokens_are_masked_and_bounded():
    guard.remember_secret(TOKEN)
    guard.remember_secret('short')
    assert TOKEN not in guard.client_error_message(f'bad token {TOKEN}.')
    assert 'short' in guard.client_error_message('a short message')
    for index in range(guard._REMEMBER_LIMIT + 10):
        guard.remember_secret(f'token-value-{index:06d}')
    assert len(guard._REMEMBERED) == guard._REMEMBER_LIMIT and TOKEN not in guard._REMEMBERED


@pytest.mark.parametrize('text', [
    "OpenAIException - Cannot connect to host 127.0.0.1:9 ssl:<ssl.SSLContext object at 0x78b519699370> "
    "[Connect call failed ('127.0.0.1', 9)]",
    'OpenAIException - [Errno 111] Connection refused',
    'httpx.ConnectError: All connection attempts failed',
    'OpenAIException - Cannot connect to host mock-upstream:18080 ssl:default [Name or service not known]',
])
def test_connection_failures_read_connection_error_without_address(text):
    masked = guard.client_error_message(text + "\n\nLiteLLM: model group 'dead' failed with the error above.")
    assert 'Connection error.' in masked and masked.count('Connection error.') == 1
    for detail in ('127.0.0.1', 'SSLContext', '0x78b5', 'mock-upstream', 'Connect call failed'):
        assert detail not in masked
    assert "model group 'dead'" in masked


@pytest.mark.parametrize('internal', [
    'http://mock-upstream:18080/v1', 'http://10.0.0.5:8000/v1', 'https://litellm.ns.svc.cluster.local/v1',
    'http://homeassistant.local:8123', 'http://[::1]:4000', 'http://100.101.102.103/v1', 'http://169.254.10.1',
    'http://pi1.tail1234.ts.net:4000',
])
def test_internal_hosts_are_masked(internal):
    masked = guard.client_error_message(f'upstream {internal} answered 500')
    assert masked.startswith('upstream ') and masked.endswith(' answered 500')
    host = internal.split('://', 1)[1].split('/')[0].rsplit(':', 1)[0].strip('[]')
    assert host not in masked and guard.REDACTED_HOST in masked


@pytest.mark.parametrize('public', ['https://api.openai.com/v1', 'https://openrouter.ai/api/v1', 'http://8.8.8.8/x'])
def test_public_hosts_stay(public):
    assert guard.client_error_message(f'see {public} now') == f'see {public} now'


def test_traceback_is_cut_and_non_strings_pass():
    assert guard.client_error_message('boom\nTraceback (most recent call last):\n  File "/app/x.py"') == 'boom'
    assert guard.client_error_message(None) is None and guard.client_error_message(3) == 3


def test_error_dict_and_proxy_exception_are_masked():
    error = {'message': f'Bearer {PROVIDER_KEY}', 'type': 't', 'code': '401'}
    masked = guard.client_error_dict(error)
    assert PROVIDER_KEY not in masked['message'] and masked['code'] == '401' and error['message'].endswith(PROVIDER_KEY)
    exc = Exception(f'Bearer {PROVIDER_KEY}')
    exc.message = f'Bearer {PROVIDER_KEY}'
    guard.scrub_proxy_exception(exc)
    assert PROVIDER_KEY not in exc.message and PROVIDER_KEY not in str(exc)


# R15 ------------------------------------------------------------------------------------------
def test_health_error_keeps_the_first_line_only():
    error = (f'litellm.AuthenticationError: OpenAIException - received Authorization: Bearer {PROVIDER_KEY}\n'
             'stack trace: Traceback (most recent call last):\n  File "/app/.venv/lib/x.py", line 1')
    shown = guard.health_error_text(error)
    assert shown.startswith('litellm.AuthenticationError') and '\n' not in shown
    assert PROVIDER_KEY not in shown and 'stack trace' not in shown and 'Traceback' not in shown
    assert guard.health_error_text(None) is None and len(guard.health_error_text('x' * 5000)) == 500


def test_health_endpoint_display_masks_admin_entries_and_fills_chatgpt_api_base(monkeypatch):
    monkeypatch.delenv('CHATGPT_API_BASE', raising=False)
    monkeypatch.delenv('OPENAI_CHATGPT_API_BASE', raising=False)
    data = {'model': 'openai/x', 'api_key': RAW_KEY, 'api_base': 'https://api.example.com/v1'}
    cleaned = guard.health_endpoint_display(
        {'model': 'openai/x', 'api_base': 'https://api.example.com/v1', 'error': f'key {RAW_KEY} bad\nstack trace: t',
         'raw_request_typed_dict': {'raw_request_headers': {'Authorization': f'Bearer {RAW_KEY}'}}}, data, True)
    assert RAW_KEY not in json.dumps(cleaned) and cleaned['api_base'] == 'https://api.example.com/v1'
    chatgpt = guard.health_endpoint_display({'model': 'chatgpt/gpt-5.5', 'error': 'x'}, {'model': 'chatgpt/gpt-5.5'}, True)
    assert chatgpt['api_base'] == guard.CHATGPT_API_BASE_DEFAULT
    monkeypatch.setenv('CHATGPT_API_BASE', 'https://chatgpt.example/backend')
    assert guard.health_endpoint_display({}, {'custom_llm_provider': 'chatgpt'}, True)['api_base'] == 'https://chatgpt.example/backend'
    assert 'api_base' not in guard.health_endpoint_display({}, {'model': 'chatgpt/x'}, False)  # details=False
    assert 'api_base' not in guard.health_endpoint_display({}, {'model': 'openai/x'}, True)


# R11 ------------------------------------------------------------------------------------------
def test_responses_events_are_restamped_on_a_copy():
    event = {'type': 'response.created', 'response': {'id': 'resp_1', 'model': 'gpt-5.5'}}
    restamped = guard.restamp_responses_event_model(event, 'chatgpt/gpt-5.5')
    assert restamped['response']['model'] == 'chatgpt/gpt-5.5' and event['response']['model'] == 'gpt-5.5'
    delta = {'type': 'response.output_text.delta', 'delta': 'x'}
    assert guard.restamp_responses_event_model(delta, 'm') is delta
    chat = {'object': 'chat.completion.chunk', 'model': 'x'}
    assert guard.restamp_responses_event_model(chat, 'm') is chat
    assert guard.restamp_responses_event_model(event, '') is event


# R01 ------------------------------------------------------------------------------------------
@pytest.mark.parametrize('value,chars,existing,expected', [
    ('x%2Fy', guard.PATH_UNSAFE, None, True), ('x/y', guard.PATH_UNSAFE, None, True),
    ('100%', guard.NAME_UNSAFE, None, True), ('chatgpt/gpt-5.5', guard.NAME_UNSAFE, None, False),
    ('我的 OpenAI 金鑰', guard.PATH_UNSAFE, None, False), ('x y', guard.PATH_UNSAFE, None, False),
    ('x%2Fy', guard.PATH_UNSAFE, 'x%2Fy', False),  # keeping a stored name is never refused
    (None, guard.PATH_UNSAFE, None, False),
])
def test_unsafe_name_rule(value, chars, existing, expected):
    assert guard.unsafe_name(value, chars, existing) is expected


def test_unsafe_name_message_names_the_allowed_characters():
    message = guard.unsafe_name_message('credential_name', 'a%b', guard.PATH_UNSAFE)
    assert "'%' or '/'" in message and 'Home Assistant' in message and '中文' in message
