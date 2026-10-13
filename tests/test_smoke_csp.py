"""Accept enforcing CSP policy lists, never discard hardening to pass smoke."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('smoke_csp', Path(__file__).parent/'smoke/smoke_checks.py')
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


@pytest.mark.parametrize('values,expected', [
    (["frame-ancestors 'self', object-src 'none'; base-uri 'none'"], True),
    (["frame-ancestors 'self'", "object-src 'none'; base-uri 'none'"], True),
    (["object-src 'none'; base-uri 'none'; frame-ancestors 'self'"], True),
    (["SCRIPT-SRC 'nonce-fixture'; FRAME-ANCESTORS 'self'", "OBJECT-SRC 'none'; BASE-URI 'none'"], True),
    ([], False),
    (["frame-ancestors 'self'"], False),
    (["frame-ancestors 'none', object-src 'none'; base-uri 'none'"], False),
    (["frame-ancestors *; object-src 'none'; base-uri 'none'"], False),
    (["frame-ancestors 'self'; base-uri 'none'"], False),
    (["frame-ancestors 'self'; object-src 'none'"], False),
    (["frame-ancestors 'self'; object-src 'self'; base-uri 'none'"], False),
    (["frame-ancestors 'self'; object-src 'none'; base-uri *"], False),
    (["frame-ancestors 'self'; object-src 'none'; base-uri 'none'", "frame-ancestors 'none'"], False),
    (["frame-ancestors 'none'; frame-ancestors 'self'; object-src 'none'; base-uri 'none'"], False),
    (["frame-ancestors 'self' 'none'; object-src 'none'; base-uri 'none'"], False),
    (["frame-ancestors; object-src 'none'; base-uri 'none'"], False),
])
def test_ingress_csp_semantics(values, expected):
    assert smoke.ingress_csp_valid(values) is expected
