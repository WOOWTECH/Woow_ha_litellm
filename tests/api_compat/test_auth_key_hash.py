"""D1 contracts (apply_upstream_guard.py): auth and key errors never show the virtual key's hash.

LiteLLM 1.104.0 answers a deleted, revoked or unknown virtual key with ``Received API Key =
sk-...last4, Key Hash (Token) =<sha256 of the key>``; clients forward that verbatim and the spend
log stores it in ``error_information``. Same real ASGI app, auth cache, recording spend writer and
offline provider as test_upstream_guard.py; the database holds no verification-token row (an
unknown key, or a key whose row was deleted). Every key below is fake.
"""
import datetime
import json
import logging
import pytest
from fastapi import HTTPException
import litellm
from litellm.caching.dual_cache import DualCache
from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy import proxy_server as server
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.hooks.parallel_request_limiter_v3 import _PROXY_MaxParallelRequestsHandler_v3
from litellm.proxy.utils import InternalUsageCache, hash_token
from .test_upstream_guard import HEX64, M, P3_HEADERS, TINY, gateway  # noqa: F401  (gateway is a fixture)

UNKNOWN = 'sk-d1-unknown-virtual-key-fixture-ppyA'
REVOKED = 'sk-d1-revoked-virtual-key-fixture-q7Rz'
EXPIRED = 'sk-d1-expired-virtual-key-fixture-x1Ex'
BLOCKED = 'sk-d1-blocked-virtual-key-fixture-b2Bl'
LIMITED = 'sk-d1-rpm-limited-virtual-key-fixture-r3Rl'
MODEL_LIMITED = 'sk-d1-model-rpm-limited-key-fixture-m4Ml'
SPENT = 'sk-d1-over-budget-virtual-key-fixture-o5Ob'
# A client that pastes the key id (hash) itself as the bearer, and a key without the sk- prefix.
PASTED_HASH = hash_token(UNKNOWN)
NO_PREFIX = 'woow-' + hash_token(REVOKED)
UNKNOWN_MESSAGE = ('Authentication Error, Invalid proxy server token passed. Received API Key = sk-...{}. '
                   'Unable to find token in cache or `LiteLLM_VerificationTokenTable`')


class NoTokenRows:
    """The verification-token table without a row for the presented key (never created, or deleted)."""

    async def get_data(self, **kwargs):
        return None


def _seed(key, **extra):
    server.user_api_key_cache.set_cache(key=hash_token(key), model_type=UserAPIKeyAuth, value=UserAPIKeyAuth(
        api_key=hash_token(key), token=hash_token(key), user_role=LitellmUserRoles.INTERNAL_USER, models=[],
        user_id='offline-user', metadata=extra.pop('metadata', {}), key_alias='d1-' + key[-4:], **extra))


@pytest.fixture
def keys(gateway, monkeypatch):
    monkeypatch.setattr(server, 'prisma_client', NoTokenRows())
    _seed(REVOKED, spend=0.0)
    _seed(EXPIRED, spend=0.0, expires=datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc))
    _seed(BLOCKED, spend=0.0, blocked=True)
    _seed(SPENT, spend=5.0, max_budget=0.01)
    _seed(LIMITED, spend=0.0, rpm_limit=1)
    _seed(MODEL_LIMITED, spend=0.0, metadata={'model_rpm_limit': {'free': 1}})
    # The default per-key rate limiter (the fixture's callback list holds only the spend writer).
    limiter = _PROXY_MaxParallelRequestsHandler_v3(InternalUsageCache(dual_cache=DualCache()))
    monkeypatch.setattr(litellm, 'callbacks', [*litellm.callbacks, limiter])
    return gateway


def revoke(gateway):
    """The key worked; then its row was deleted (key/delete) and its cache entry invalidated."""
    assert gateway.request('POST', '/v1/chat/completions', {'model': 'free', 'messages': M}, token=REVOKED).status_code == 200
    server.user_api_key_cache.delete_cache(key=hash_token(REVOKED))
    gateway.spend.clear()


ROUTES = {
    'chat': ('POST', '/v1/chat/completions', {'model': 'free', 'messages': M}),
    'chat-stream': ('POST', '/v1/chat/completions', {'model': 'free', 'messages': M, 'stream': True}),
    'messages': ('POST', '/v1/messages', {'model': 'free', 'messages': M, 'max_tokens': 32}),
    'messages-stream': ('POST', '/v1/messages', {'model': 'free', 'messages': M, 'max_tokens': 32, 'stream': True}),
    'models': ('GET', '/v1/models', None),
    'key-info': ('GET', '/key/info', None),
}
TOKENS = {'unknown': UNKNOWN, 'revoked': REVOKED, 'expired': EXPIRED, 'blocked': BLOCKED,
          'pasted-hash': PASTED_HASH, 'no-prefix': NO_PREFIX}
# (status, error.type, error.code, error.param) on bf0d29b (HA 0.1.9) for the same requests; D1 changes
# messages only. The expired key's param is upstream's masked key (sk-...last4).
BASELINE = {
    'unknown': (401, 'token_not_found_in_db', '401', 'key'),
    'revoked': (401, 'token_not_found_in_db', '401', 'key'),
    'expired': (401, 'expired_key', '401', 'sk-...' + EXPIRED[-4:]),
    'blocked': (401, 'auth_error', '401', 'None'),
    'pasted-hash': (401, 'auth_error', '401', 'None'),
    'no-prefix': (401, 'auth_error', '401', 'None'),
}


def hashes_in(result):
    headers = '\n'.join(f'{k}: {v}' for k, v in result.headers.items() if k.lower() not in P3_HEADERS)
    return HEX64.findall(result.text.lower()) + HEX64.findall(headers.lower())


def envelope(result):
    error = result.json()['error']
    return result.status_code, error.get('type'), error.get('code'), error.get('param')


@pytest.mark.parametrize('route', ROUTES)
@pytest.mark.parametrize('who', TOKENS)
def test_auth_errors_carry_no_key_hash(keys, who, route):
    if who == 'revoked':
        revoke(keys)
    method, path, body = ROUTES[route]
    result = keys.request(method, path, body, token=TOKENS[who])
    assert envelope(result) == BASELINE[who]
    assert hashes_in(result) == [], result.text[:400]
    for secret in (hash_token(TOKENS[who]), PASTED_HASH, hash_token(REVOKED)):
        assert secret not in result.text and secret not in str(result.headers)


@pytest.mark.parametrize('route', ROUTES)
@pytest.mark.parametrize('who', ['unknown', 'revoked'])
def test_unknown_key_message_keeps_masked_key(keys, who, route):
    """OpenAI-style: the caller still sees which key was refused (last 4 characters), nothing more."""
    if who == 'revoked':
        revoke(keys)
    method, path, body = ROUTES[route]
    result = keys.request(method, path, body, token=TOKENS[who])
    assert result.json()['error']['message'] == UNKNOWN_MESSAGE.format(TOKENS[who][-4:])


def failure_info(gateway):
    from litellm.proxy.spend_tracking.spend_tracking_utils import get_logging_payload
    rows = []
    for row in gateway.spend:
        payload = get_logging_payload(kwargs=row['kwargs'], response_obj=row['completion_response'],
                                      start_time=row['start_time'], end_time=row['end_time'], llm_router=None)
        metadata = payload['metadata'] if isinstance(payload['metadata'], dict) else json.loads(payload['metadata'])
        if metadata.get('error_information'):
            rows.append((payload, metadata['error_information']))
    return rows


@pytest.mark.parametrize('route', ['chat', 'messages', 'models', 'key-info'])
@pytest.mark.parametrize('who', TOKENS)
def test_spend_log_error_message_has_no_key_hash(keys, who, route):
    """Internal users read error_information; its message, traceback and class carry no key hash."""
    if who == 'revoked':
        revoke(keys)
    method, path, body = ROUTES[route]
    keys.request(method, path, body, token=TOKENS[who])
    rows = failure_info(keys)
    assert rows, 'the auth failure must still be logged'
    for payload, info in rows:
        assert info['error_code'] == ('' if who == 'blocked' else '401')  # a plain Exception has no code
        text = json.dumps({k: info.get(k) for k in ('error_message', 'traceback', 'normalized_error', 'error_class')})
        assert HEX64.findall(text.lower()) == [], text[:400]
    # what failure callbacks receive as the response object: the (scrubbed) exception itself
    assert HEX64.findall(' '.join(str(row['completion_response']) for row in keys.spend).lower()) == []


@pytest.mark.parametrize('who', ['unknown', 'revoked'])
def test_spend_log_keeps_class_and_key_column_for_admins(keys, who):
    if who == 'revoked':
        revoke(keys)
    keys.request('POST', '/v1/chat/completions', {'model': 'free', 'messages': M}, token=TOKENS[who])
    [(payload, info)] = failure_info(keys)
    assert info['error_class'] == 'KeyNotFoundError' and info['normalized_error'] == '401_AUTHENTICATION_FAILED'
    assert info['error_message'] == UNKNOWN_MESSAGE.format(TOKENS[who][-4:])
    assert payload['api_key'] == hash_token(TOKENS[who])  # the row's own key column is unchanged (admin view)


class EmitTimeRecords(logging.Handler):
    """Formats each record when it is emitted, as a stream or file handler would."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append((record.levelno, record.getMessage()))


@pytest.fixture
def proxy_log():
    logger = logging.getLogger('LiteLLM Proxy')
    handler = EmitTimeRecords()
    logger.addHandler(handler)
    yield handler
    logger.removeHandler(handler)


@pytest.mark.parametrize('who', ['unknown', 'revoked'])
def test_server_log_keeps_the_hash_for_admins(keys, proxy_log, who):
    if who == 'revoked':
        revoke(keys)
        proxy_log.lines.clear()
    keys.request('POST', '/v1/chat/completions', {'model': 'free', 'messages': M}, token=TOKENS[who])
    auth = [(level, line) for level, line in proxy_log.lines if 'user_api_key_auth(): Exception occured' in line]
    assert len(auth) == 1 and auth[0][0] == logging.ERROR
    assert hash_token(TOKENS[who]) in auth[0][1]


def test_callback_replacement_error_is_scrubbed(keys, monkeypatch):
    """A failure callback may hand back its own HTTPException; the client still gets no key hash."""

    class Replace(CustomLogger):
        async def async_post_call_failure_hook(self, request_data, original_exception, user_api_key_dict,
                                               traceback_str=None):
            return HTTPException(status_code=401, detail={'error': 'rejected key ' + hash_token(UNKNOWN)})

    monkeypatch.setattr(litellm, 'callbacks', [*litellm.callbacks, Replace()])
    result = keys.request('POST', '/v1/chat/completions', {'model': 'free', 'messages': M}, token=UNKNOWN)
    assert result.status_code == 401 and 'rejected key [redacted]' in result.text
    assert hashes_in(result) == []


# Key-scoped rate limits: the limiter names the key by its hash (api_key, model_per_key).
def _twice(gateway, token, route):
    method, path, body = ROUTES[route]
    first = gateway.request(method, path, body, token=token)
    assert first.status_code == 200
    return gateway.request(method, path, body, token=token)


@pytest.mark.parametrize('route', ['chat', 'chat-stream', 'messages'])
def test_key_rate_limit_message_has_no_key_hash(keys, route):
    result = _twice(keys, LIMITED, route)
    assert result.status_code == 429
    assert hashes_in(result) == [] and hash_token(LIMITED) not in result.text
    assert 'Rate limit exceeded for api_key: [redacted]. Limit type: requests.' in result.text
    for _, info in failure_info(keys):
        assert HEX64.findall(json.dumps(info).lower()) == []


def test_key_rate_limit_envelope_unchanged(keys):
    result = _twice(keys, LIMITED, 'chat')
    assert envelope(result) == (429, 'throttling_error', '429', None)


def test_model_per_key_rate_limit_keeps_the_model(keys):
    result = _twice(keys, MODEL_LIMITED, 'chat')
    assert result.status_code == 429 and hashes_in(result) == []
    assert 'Rate limit exceeded for model_per_key: [redacted]:free.' in result.text


@pytest.mark.parametrize('route', ['chat', 'messages', 'models'])
def test_budget_errors_name_the_key_by_alias(keys, route):
    """Key budget refusals already use the alias; the spend log keeps the budget entity id for admins."""
    method, path, body = ROUTES[route]
    result = keys.request(method, path, body, token=SPENT)
    assert result.status_code == 422 and result.headers.get('x-should-retry') == 'false'
    assert hashes_in(result) == [] and 'Key=d1-' + SPENT[-4:] in result.text
    [(_, info)] = failure_info(keys)
    assert info['error_budget_entity_id'] == hash_token(SPENT)
    assert HEX64.findall(json.dumps({k: info[k] for k in ('error_message', 'traceback')}).lower()) == []


def test_budget_admission_refusal_has_no_key_hash(keys):
    result = keys.request('POST', '/v1/chat/completions', {'model': 'reasoning', 'messages': M}, token=TINY)
    assert result.status_code == 422 and hashes_in(result) == []


def test_working_key_still_works(keys):
    """The scrub runs only on failures: a valid key gets its normal answer."""
    result = keys.request('POST', '/v1/chat/completions', {'model': 'free', 'messages': M}, token=REVOKED)
    assert result.status_code == 200 and result.json()['choices'][0]['message']['content'] == 'OK'
