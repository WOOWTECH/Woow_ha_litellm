"""Lean unit tests for litellm/patches/woow_auth_errors.py (D1); loaded by path, no LiteLLM needed."""
import importlib.util
from pathlib import Path
import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('woow_auth_errors', REPO / 'litellm/patches/woow_auth_errors.py')
errors = importlib.util.module_from_spec(spec)
spec.loader.exec_module(errors)

HASH = '0123456789abcdef' * 4  # fake key hash
UPSTREAM = ('Authentication Error, Invalid proxy server token passed. Received API Key = sk-...ppyA, '
            f'Key Hash (Token) ={HASH}. Unable to find token in cache or `LiteLLM_VerificationTokenTable`')


@pytest.mark.parametrize('text,expected', [
    (UPSTREAM, 'Authentication Error, Invalid proxy server token passed. Received API Key = sk-...ppyA. '
               'Unable to find token in cache or `LiteLLM_VerificationTokenTable`'),
    (f'Received API Key = sk-...ppyA, Key Hash (Token) = {HASH.upper()}.', 'Received API Key = sk-...ppyA.'),
    (f'Authentication Error, Invalid proxy server token passed. key={HASH}, not found in db.',
     'Authentication Error, Invalid proxy server token passed. key=[redacted], not found in db.'),
    (f"Key doesn't exist in cache + check_cache_only=True. key={HASH}.",
     "Key doesn't exist in cache + check_cache_only=True. key=[redacted]."),
    (f'Rate limit exceeded for api_key: {HASH}. Limit type: requests.',
     'Rate limit exceeded for api_key: [redacted]. Limit type: requests.'),
    (f'{HASH}:woowfake/model', '[redacted]:woowfake/model'),
    (f'{HASH}\n{HASH}', '[redacted]\n[redacted]'),
])
def test_key_hashes_are_removed(text, expected):
    assert errors.scrub_key_hash(text) == expected


@pytest.mark.parametrize('text', [
    'Authentication Error - Expired Key. Key Expiry time 2020-01-01 00:00:00+00:00',
    "Key is blocked. Update via `/key/unblock` if you're an admin.",
    "LiteLLM Virtual Key expected. Received=0123****cdef, expected to start with 'sk-'.",
    'Budget has been exceeded! Key=team-bot (sk-...ppyA) Current cost: 5.0, Max budget: 0.01',
    HASH[:63],  # 63 hex digits: not a sha256
    HASH + 'a',  # 65 hex digits: part of something longer
    'x' + HASH[:63] + 'g',
    'request 3f2a9c1e-77aa-4d1b-9a0e-0123456789ab failed',  # UUIDs keep their hyphens
    '',
])
def test_other_text_is_unchanged(text):
    assert errors.scrub_key_hash(text) == text


@pytest.mark.parametrize('value', [None, 401, b'0123', ['x'], {'error': HASH}])
def test_non_strings_pass_through(value):
    assert errors.scrub_key_hash(value) is value


class ProxyLikeError(Exception):
    """Mirrors ProxyException: .message plus args, extra attributes that must survive."""

    def __init__(self, message):
        self.message = message
        super().__init__(message)
        self.type, self.param, self.code, self.headers = 'token_not_found_in_db', 'key', '401', {'x-a': 'b'}


class HTTPLikeError(Exception):
    def __init__(self, status_code, detail):
        super().__init__(status_code, detail)
        self.status_code, self.detail = status_code, detail


class BudgetLikeError(Exception):
    def __init__(self, message, entity_id):
        self.message, self.entity_id = message, entity_id
        super().__init__(message)


def test_proxy_exception_message_and_args_scrubbed_in_place():
    exc = ProxyLikeError(UPSTREAM)
    assert errors.scrub_auth_exception(exc) is exc
    assert HASH not in exc.message and HASH not in str(exc) and 'sk-...ppyA' in exc.message
    assert (exc.type, exc.param, exc.code, exc.headers) == ('token_not_found_in_db', 'key', '401', {'x-a': 'b'})


@pytest.mark.parametrize('detail,expected', [
    (f'rejected {HASH}', 'rejected [redacted]'),
    ({'error': f'Key not found: {HASH}', 'n': 1}, {'error': 'Key not found: [redacted]', 'n': 1}),
    ({'error': {'message': [f'k={HASH}']}}, {'error': {'message': ['k=[redacted]']}}),
])
def test_http_exception_detail_scrubbed(detail, expected):
    exc = HTTPLikeError(401, detail)
    errors.scrub_auth_exception(exc)
    assert exc.detail == expected and exc.status_code == 401 and HASH not in str(exc)


def test_plain_exception_args_scrubbed():
    exc = Exception(f"Key doesn't exist in cache + check_cache_only=True. key={HASH}.")
    errors.scrub_auth_exception(exc)
    assert str(exc) == "Key doesn't exist in cache + check_cache_only=True. key=[redacted]."


def test_structured_ids_are_kept_for_admins():
    exc = BudgetLikeError(f'Budget has been exceeded! Key={HASH}', HASH)
    errors.scrub_auth_exception(exc)
    assert exc.message == 'Budget has been exceeded! Key=[redacted]' and exc.entity_id == HASH


def test_clean_exception_is_left_untouched():
    exc = ProxyLikeError('Authentication Error, Key is blocked.')
    before = (exc.message, exc.args)
    errors.scrub_auth_exception(exc)
    assert (exc.message, exc.args) == before


def test_read_only_or_raising_attributes_do_not_break_the_handler():
    class ReadOnly(Exception):
        @property
        def message(self):
            return f'key={HASH}'

        @property
        def detail(self):
            raise RuntimeError('lazy attribute')

    exc = ReadOnly(f'key={HASH}')
    assert errors.scrub_auth_exception(exc) is exc
    assert str(exc) == 'key=[redacted]'  # args still scrubbed


@pytest.mark.parametrize('value', [None, 'text', 401, HTTPLikeError])
def test_non_exceptions_pass_through(value):
    assert errors.scrub_auth_exception(value) is value
