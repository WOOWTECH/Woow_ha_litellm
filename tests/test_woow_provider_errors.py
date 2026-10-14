"""Lean tests for the stdlib-only provider error helper (no LiteLLM installed)."""
import importlib.util
import json
import logging
from pathlib import Path
import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('woow_provider_errors', REPO / 'litellm/patches/woow_provider_errors.py')
errors = importlib.util.module_from_spec(spec)
spec.loader.exec_module(errors)

USER = 'user_FAKEgatewayACCOUNT01'
PREFIX = 'litellm.BadRequestError: OpenrouterException - '
SUFFIX = "\n\nLiteLLM: model group 'or400' failed with the error above. No fallback was attempted."


def openrouter(status, message, metadata=None):
    body = {'error': {'message': message, 'code': status}, 'user_id': USER}
    if metadata is not None:
        body['error']['metadata'] = metadata
    return json.dumps(body)


def scrubbed_message(text):
    start = text.index('{')
    body, _ = json.JSONDecoder().raw_decode(text, start)
    assert set(body) == {'error'} and set(body['error']) == {'message', 'code'}
    return body['error']['message']


@pytest.mark.parametrize('status,message,expected', [
    (400, 'temperature: Invalid input', 'temperature: Invalid input'),
    (401, 'User not found.', errors.MSG_401),
    (402, 'can only afford 1234', errors.MSG_402),
    (403, 'Key limit exceeded (total limit)', errors.MSG_403),
    (404, 'No endpoints found', 'No endpoints found'),
    (408, 'Timed out', errors.MSG_TIMEOUT),
    (413, 'Request entity too large', 'Request entity too large'),
    (422, 'Unprocessable', 'Unprocessable'),
    (429, 'Provider returned error', errors.MSG_429),
    (500, 'Internal Server Error', 'Internal Server Error'),
    (502, 'Provider returned error', 'Provider returned error'),
    (503, 'provider overloaded', 'provider overloaded'),
    (504, 'Gateway timeout', errors.MSG_TIMEOUT),
])
def test_status_policy(status, message, expected):
    text = errors.scrub_provider_error('openrouter', openrouter(status, message, {'provider_name': 'P', 'is_byok': True}), status)
    assert scrubbed_message(text) == expected
    assert USER not in text and 'metadata' not in text and 'is_byok' not in text


def test_moderation_keeps_reasons_never_the_flagged_input():
    metadata = {'reasons': ['violence', 'x' * 100, 'a', 'b', 'c', 'dropped'], 'flagged_input': 'SENTINEL-INPUT'}
    text = errors.scrub_provider_error('openrouter', openrouter(403, 'flagged', metadata), 403)
    message = scrubbed_message(text)
    assert message == 'The input was flagged by upstream moderation (violence, ' + 'x' * 64 + ', a, b, c).'
    assert 'SENTINEL-INPUT' not in text and 'dropped' not in text
    assert errors.scrub_provider_error('openrouter', text, 403) == text


@pytest.mark.parametrize('status', [400, 403, 429, 502])
def test_idempotent_including_router_wrapper(status):
    raw = PREFIX + openrouter(status, 'Provider returned error', {
        'raw': json.dumps({'error': {'message': 'nested reason req_FAKEupstream0001'}}),
        'reasons': ['violence']}) + SUFFIX
    once = errors.scrub_provider_error('openrouter', raw, status)
    assert once.startswith(PREFIX) and once.endswith(SUFFIX)
    assert errors.scrub_provider_error('openrouter', once, status) == once
    assert errors.scrub_provider_error('openrouter', 'litellm.APIError: ' + once, None) == 'litellm.APIError: ' + once
    assert errors.client_safe_stream_message(once) == once


def test_non_json_is_returned_unchanged_without_identifiers():
    text = 'litellm.APIConnectionError: OpenrouterException - connection reset by peer'
    assert errors.scrub_provider_error('openrouter', text, None) == text


def test_nested_raw_reason_is_appended_and_truncated():
    nested = 'Invalid tools[0].name ' + 'y' * 400
    text = errors.scrub_provider_error('openrouter', openrouter(400, 'Provider returned error', {
        'raw': json.dumps({'error': {'message': nested}})}), 400)
    assert scrubbed_message(text) == 'Provider returned error: ' + nested[:300]
    plain = errors.scrub_provider_error('openrouter', openrouter(400, 'Provider returned error', {'raw': 'not json'}), 400)
    assert scrubbed_message(plain) == 'Provider returned error'
    unrelated = errors.scrub_provider_error('openrouter', openrouter(502, 'Provider returned error', {
        'raw': json.dumps({'error': {'message': 'html'}})}), 502)
    assert scrubbed_message(unrelated) == 'Provider returned error'


@pytest.mark.parametrize('identifier', [
    'user_ABCdef123', 'org-ABCdef123', 'req_ABCdef123', 'req-ABC-def-123', 'gen-1700000000-AbCdEf', 'msg_ABCdef123',
    'chatcmpl-ABCdef123', 'sk-or-v1-ABCDEFGH12', 'a' * 32, '0123456789abcdef' * 4,
    'org_2abcDEFghiJKL', '6e5c1d2a-1234-4abc-9def-0123456789ab', '6E5C1D2A-1234-4ABC-9DEF-0123456789AB',
])
def test_identifier_rules(identifier):
    text = errors.scrub_provider_error('openrouter', openrouter(400, f'bad {identifier} here'), 400)
    assert identifier not in text and scrubbed_message(text) == 'bad [redacted] here'


def test_project_numbers_are_redacted():
    assert errors.redact_ids('quota for projects/123456789012/locations') == 'quota for projects/[redacted]/locations'


def test_nested_raw_clerk_org_is_redacted():
    text = errors.scrub_provider_error('openrouter', openrouter(422, 'Provider returned error', {
        'raw': json.dumps({'message': 'org_2abcDEFghiJKL over quota'})}), 422)
    assert scrubbed_message(text) == 'Provider returned error: [redacted] over quota'


def test_openrouter_urls_are_redacted_without_breaking_json():
    text = errors.scrub_provider_error('openrouter', openrouter(404, 'Configure: https://openrouter.ai/settings/privacy'), 404)
    assert scrubbed_message(text) == 'Configure: [redacted-url]'


@pytest.mark.parametrize('provider', ['anthropic', 'openai', 'azure', None])
def test_other_providers_are_byte_identical(provider):
    text = json.dumps({'error': {'message': 'm user_ABCdef123', 'metadata': {'raw': 'x'}}})
    assert errors.scrub_provider_error(provider, text, 400) is text


def test_openai_route_pointed_at_openrouter_is_detected_by_user_id():
    text = 'OpenAIException - ' + openrouter(400, 'bad')
    assert USER not in errors.scrub_provider_error('openai', text, 400)


SDK_REPR = ("Error code: 403 - {'error': {'message': 'P requires moderation', 'code': 403, 'metadata': "
            "{'reasons': ['violence'], 'flagged_input': 'FLAGGED', 'provider_name': 'P', 'is_byok': False}}, "
            "'user_id': '" + USER + "'}")


def test_openai_sdk_python_repr_is_rebuilt():
    text = errors.scrub_provider_error('openai', SDK_REPR, 403)
    assert text == ('Error code: 403 - {"error":{"message":"The input was flagged by upstream moderation '
                    '(violence).","code":403}}')
    assert errors.scrub_provider_error('openai', text, 403) is text  # rebuilt body has no user_id: not touched again


class SdkError(Exception):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.message, self.status_code = message, status_code


@pytest.mark.parametrize('status,inner,expected', [
    (401, 'User not found.', errors.MSG_401),
    (402, 'can only afford 1234. To increase, visit https://openrouter.ai/settings/credits', errors.MSG_402),
    (403, 'P requires moderation', 'The input was flagged by upstream moderation (violence).'),
    (404, 'No endpoints found. Configure: https://openrouter.ai/settings/privacy', 'No endpoints found. Configure: [redacted-url]'),
    (400, 'temperature: Invalid input', 'temperature: Invalid input'),
])
def test_compatible_message_uses_the_exceptions_full_text(status, inner, expected):
    reasons = {'reasons': ['violence'], 'flagged_input': 'FLAGGED'} if status == 403 else {'provider_name': 'P'}
    full = 'Error code: %d - %r' % (status, {'error': {'message': inner, 'code': status, 'metadata': reasons},
                                             'user_id': USER})
    assert errors.scrub_compatible_message('openai', inner, SdkError(full, status)) == expected
    # the SDK's whole text as the message: rebuilt in place
    whole = errors.scrub_compatible_message('openai', full, SdkError(full, status))
    assert whole.startswith('Error code: %d - {"error":{"message":' % status) and USER not in whole and 'FLAGGED' not in whole


def test_compatible_message_leaves_other_providers_alone():
    full = 'Error code: 400 - %r' % {'error': {'message': 'bad user_ABCdef123', 'type': 'invalid_request_error'}}
    for message in ('bad user_ABCdef123', full):
        assert errors.scrub_compatible_message('openai', message, SdkError(full, 400)) is message
    assert errors.scrub_compatible_message('openai', None, SdkError(full, 400)) is None


def test_compatible_message_on_openrouter_provider_still_scrubs():
    raw = openrouter(429, 'Provider returned error')
    assert scrubbed_message(errors.scrub_compatible_message('openrouter', raw, SdkError(raw, 429))) == errors.MSG_429


def test_anthropic_error_about_metadata_user_id_is_byte_identical(caplog):
    caplog.set_level(logging.WARNING, logger='LiteLLM')
    text = 'AnthropicException - ' + json.dumps({'type': 'error', 'error': {
        'type': 'invalid_request_error', 'message': 'metadata.user_id: String should have at most 256 characters'}})
    assert errors.scrub_provider_error('anthropic', text, 400) is text
    assert errors.client_safe_stream_message(text) == text
    assert not caplog.records


def test_stream_message_keeps_other_providers_fields():
    vertex = json.dumps({'error': {'code': 429, 'message': 'Quota exceeded for aiplatform', 'status': 'RESOURCE_EXHAUSTED'}})
    assert errors.client_safe_stream_message(vertex) == vertex
    assert errors.client_safe_stream_message(vertex.replace('aiplatform', 'req_ABCdef123')) == vertex.replace(
        'aiplatform', '[redacted]')


@pytest.mark.parametrize('text', ["Error code: 400 - {'error': {'message': 'unterminated", 'x {not: a literal}',
                                  "Error code: 400 - {'error': __import__('os')}"])
def test_unparseable_bodies_are_left_alone(text):
    assert errors.scrub_provider_error('openai', text, 400) is text


def test_health_endpoint_for_non_admin():
    endpoint = {'model': 'openrouter/x', 'api_base': 'https://or403mod.upstream.invalid/api/v1', 'api_version': None,
                'raw_request_typed_dict': {'raw_request_api_base': 'https://or403mod.upstream.invalid/api/v1'},
                'error': ('litellm.APIConnectionError: cannot reach or403mod.upstream.invalid for ' + USER
                          + '\nstack trace: Traceback (most recent call last):\n  File "/usr/lib/x.py"')}
    out = errors.health_endpoint_for_non_admin(endpoint, ('api_base', 'api_version'))
    assert out == {'model': 'openrouter/x', 'error': 'litellm.APIConnectionError: cannot reach [redacted-url] for [redacted]'}
    assert errors.health_endpoint_for_non_admin({'model': 'm', 'error': 'x' * 900}, ())['error'] == 'x' * 500


def test_raw_text_goes_to_the_server_log_only_when_changed(caplog):
    caplog.set_level(logging.WARNING, logger='LiteLLM')
    original = openrouter(400, 'bad')
    once = errors.scrub_provider_error('openrouter', original, 400)
    errors.scrub_provider_error('openrouter', once, 400)
    records = [r.getMessage() for r in caplog.records if r.getMessage().startswith('Woow upstream error scrubbed')]
    assert len(records) == 1 and USER in records[0] and 'status=400' in records[0]


@pytest.mark.parametrize('code,status', [
    (502, 502), ('503', 503), ('server_error', 500), ('rate_limit_exceeded', 429), (None, 500), (200, 500),
    (True, 500), ('abc', 500), (999, 500),
])
def test_stream_error_parts_status(code, status):
    message, mapped = errors.stream_error_parts({'code': code, 'message': 'Provider disconnected user_ABCdef123',
                                                 'metadata': {'raw': 'x', 'headers': {'cf-ray': 'r'}}, 'user_id': USER})
    assert mapped == status
    body = json.loads(message)
    assert body == {'error': {'message': 'Provider disconnected [redacted]',
                              'code': code if isinstance(code, (int, str)) and not isinstance(code, bool) else None}}


def test_stream_error_parts_without_message():
    message, status = errors.stream_error_parts({})
    assert json.loads(message)['error']['message'] == errors.GENERIC_STREAM_MESSAGE and status == 500


@pytest.mark.parametrize('name,allowed', [
    ('x-ratelimit-remaining-requests', True), ('llm_provider-x-ratelimit-limit-tokens', True), ('Retry-After', True),
    ('llm_provider-retry-after', True), ('cf-ray', False), ('x-request-id', False), ('llm_provider-x-request-id', False),
    ('x-woowfake-probe', False), ('server', False),
])
def test_provider_header_allowlist(monkeypatch, name, allowed):
    monkeypatch.delenv('WOOW_PROVIDER_HEADERS', raising=False)
    assert errors.provider_header_allowed(name) is allowed
    monkeypatch.setenv('WOOW_PROVIDER_HEADERS', 'all')
    assert errors.provider_header_allowed(name) is True
