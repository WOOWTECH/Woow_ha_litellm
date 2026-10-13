"""Offline contracts against the actual pinned module, not a throttle reimplementation.

LITELLM_LOGIN_SECURITY_MODULE is a local RED/GREEN seam only. The image runner
always selects and hash-checks the installed module before loading this suite.
Redis cases exercise real Python fallback/TTL handling, not Redis Lua execution.
"""
import asyncio
import contextlib
import dataclasses
import importlib.util
import io
import logging
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
os.environ['LITELLM_TELEMETRY'] = 'False'
os.environ['DO_NOT_TRACK'] = '1'
sys.dont_write_bytecode = True


def offline(event, args):
    if event in {'socket.connect', 'socket.connect_ex', 'socket.getaddrinfo',
                 'socket.bind', 'socket.sendto', 'subprocess.Popen', 'os.system',
                 'os.posix_spawn', 'os.fork', 'os.forkpty', 'os.exec'}:
        raise PermissionError('Login security contracts forbid network and child processes')


sys.addaudithook(offline)
from litellm.proxy.auth import login_throttle as installed_throttle
from litellm.proxy.auth import login_utils
from litellm.proxy._types import ProxyException
from litellm.caching.in_memory_cache import InMemoryCache

MODULE_PATH = Path(os.environ.get('LITELLM_LOGIN_SECURITY_MODULE', installed_throttle.__file__))
_spec = importlib.util.spec_from_file_location('woow_test_actual_login_throttle', MODULE_PATH)
lt = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = lt
_spec.loader.exec_module(lt)


class LoginSecurityContract(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = patch('time.time', return_value=1000.0).start()
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'UI_USERNAME': 'woow-test-admin', 'UI_PASSWORD': 'synthetic-correct'}).start()
        self.stderr = io.StringIO()
        self.records = []
        logger = getattr(lt, '_security_logger', None)
        if logger:
            for handler in logger.handlers:
                if isinstance(handler, logging.StreamHandler):
                    patch.object(handler, 'stream', self.stderr).start()
            records = self.records
            class Capture(logging.Handler):
                def emit(self, record):
                    records.append(record)
            capture = Capture()
            logger.addHandler(capture)
            self.addCleanup(logger.removeHandler, capture)
        self.t = self.throttle()

    def throttle(self, **kwargs):
        values = dict(client_ip='192.0.2.45', source_limit=10, user_limit=5,
                      window_seconds=60, block_seconds=300,
                      counters=InMemoryCache(), blocks=InMemoryCache())
        values.update(kwargs)
        return lt.LoginThrottle(**values)

    async def signin(self, user='synthetic-user', password='synthetic-wrong', throttle=None):
        try:
            await login_utils.authenticate_user(user, password, 'synthetic-master', None, throttle or self.t)
        except ProxyException as exc:
            return int(exc.code), exc.headers
        return 200, {}

    async def fill_pair(self):
        for _ in range(5):
            self.assertEqual((await self.signin())[0], 401)

    async def test_pair_sixth_failure_is_429_with_retry_after(self):
        await self.fill_pair()
        code, headers = await self.signin()
        self.assertEqual(code, 429)
        self.assertEqual(headers['Retry-After'], '300')

    async def test_source_eleventh_failure_is_429_with_retry_after(self):
        for i in range(10):
            self.assertEqual((await self.signin(user=f'synthetic-{i}'))[0], 401)
        code, headers = await self.signin(user='synthetic-10')
        self.assertEqual(code, 429)
        self.assertEqual(headers['Retry-After'], '300')

    async def test_blocked_correct_password_never_reaches_password_check(self):
        await self.fill_pair()
        await self.signin()
        with patch.object(login_utils, '_admin_credentials_match', side_effect=AssertionError('must not check password')):
            self.assertEqual((await self.signin(password='synthetic-correct'))[0], 429)

    async def test_refused_attempt_does_not_extend_block(self):
        await self.fill_pair()
        await self.signin()
        self.clock.return_value = 1017.0
        self.assertEqual((await self.signin())[1]['Retry-After'], '283')
        self.clock.return_value = 1025.0
        self.assertEqual((await self.signin())[1]['Retry-After'], '275')

    async def test_expired_block_allows_new_attempt(self):
        await self.fill_pair()
        await self.signin()
        self.clock.return_value = 1301.0
        self.assertEqual((await self.signin())[0], 401)

    async def test_casefold_shares_pair_budget(self):
        for i in range(5):
            self.assertEqual((await self.signin(user='Synthetic-User' if i % 2 else 'synthetic-user'))[0], 401)
        self.assertEqual((await self.signin(user='SYNTHETIC-USER'))[0], 429)

    async def test_different_source_has_independent_pair_budget(self):
        await self.fill_pair()
        await self.signin()
        other = dataclasses.replace(self.t, client_ip='192.0.2.46')
        self.assertEqual((await self.signin(throttle=other))[0], 401)

    async def test_blocked_pair_does_not_keep_spending_source_budget(self):
        await self.fill_pair()
        await self.signin()
        key = self.t._keys('synthetic-user').source_counter
        before = self.t.counters.get_cache(key)
        for _ in range(3):
            self.assertEqual((await self.signin())[0], 429)
        self.assertEqual(self.t.counters.get_cache(key), before)
        self.assertEqual((await self.signin(user='different-user'))[0], 401)

    async def test_success_clears_pair_counter_only(self):
        for _ in range(4):
            await self.signin()
        attempt = await self.t.attempt('synthetic-user')
        await attempt.succeeded()
        keys = self.t._keys('synthetic-user')
        self.assertIsNone(self.t.counters.get_cache(keys.pair_counter))
        self.assertEqual(self.t.counters.get_cache(keys.source_counter), 4)
        self.assertEqual((await self.signin())[0], 401)

    async def test_disabled_throttle_does_not_count_or_block(self):
        disabled = dataclasses.replace(self.t, enabled=False)
        for _ in range(12):
            self.assertEqual((await self.signin(throttle=disabled))[0], 401)
        self.assertIsNone(self.t.counters.get_cache(self.t._keys('synthetic-user').pair_counter))

    async def test_missing_proxy_topology_keeps_source_scope_off(self):
        from starlette.requests import Request
        request = Request({'type': 'http', 'client': ('192.0.2.45', 1234), 'headers': []})
        throttle = lt.LoginThrottle.from_request(request, {}, None)
        self.assertIsNone(throttle.source_limit)
        self.assertEqual(throttle.user_limit, 5)

    async def test_custom_limit_and_block_ttl_are_preserved(self):
        self.t = self.throttle(source_limit=4, user_limit=2, block_seconds=77)
        self.assertEqual((await self.signin())[0], 401)
        self.assertEqual((await self.signin())[0], 401)
        self.assertEqual(await self.signin(), (429, {'Retry-After': '77'}))

    async def test_safe_warning_survives_error_handler_without_global_changes(self):
        logger = lt.verbose_proxy_logger
        sink = io.StringIO()
        handler = logging.StreamHandler(sink)
        handler.setLevel(logging.ERROR)
        with patch.object(logger, 'handlers', [handler]), patch.object(logger, 'level', logging.ERROR):
            root = logging.getLogger()
            before = (root.level, tuple(root.handlers), logger.level, handler.level)
            await self.fill_pair()
            await self.signin()
            await self.signin()
            self.assertEqual((root.level, tuple(root.handlers), logger.level, handler.level), before)
            self.assertEqual(sink.getvalue(), '')
        output = self.stderr.getvalue()
        self.assertIn('WARNING woow.security.login_throttle: Admin UI sign-in blocked', output)
        self.assertIn('Admin UI sign-in refused', output)
        self.assertEqual(len(self.records), 2)
        self.assertFalse(lt._security_logger.propagate)
        for record in self.records:
            self.assertEqual(record.levelno, logging.WARNING)
            self.assertIn('username=[redacted] source=[redacted]', record.getMessage())
            self.assertNotIn('synthetic-user', repr(record.__dict__))
            self.assertNotIn('192.0.2.45', repr(record.__dict__))
            self.assertIsNone(record.exc_info)

    async def test_warning_contains_no_attacker_controlled_username(self):
        for _ in range(7):
            await self.signin(user='injected\nERROR forged-event')
        output = self.stderr.getvalue()
        self.assertIn('source=[redacted]', output)
        self.assertNotIn('injected', output)
        self.assertNotIn('forged-event', output)
        self.assertEqual(len(output.splitlines()), 2)

    async def test_redis_error_not_promoted_to_security_logger(self):
        # Only the Python fallback contract is exercised; no fake Lua model.
        class BrokenRedis:
            def async_register_script(self, script):
                async def fail(*args):
                    raise OSError('synthetic-private-redis-location')
                return fail
        self.t = dataclasses.replace(self.t, redis_cache=BrokenRedis())
        with patch.object(lt.verbose_proxy_logger, 'level', logging.ERROR):
            await self.fill_pair()
            self.assertEqual((await self.signin())[0], 429)
        self.assertNotIn('synthetic-private-redis-location', self.stderr.getvalue())
        self.assertEqual(len(self.records), 1)

    async def test_shared_store_ttl_result_raises_without_local_recount(self):
        # Stub only external Redis I/O; actual LoginAttempt.failed consumes TTLs.
        for ttls in [(300, 0), (0, 300)]:
            with self.subTest(ttls=ttls), patch.object(lt.LoginThrottle, 'record_failure', new=AsyncMock(return_value=ttls)):
                with self.assertRaises(ProxyException) as caught:
                    await lt.LoginAttempt(self.t, 'synthetic-user').failed()
                self.assertEqual(int(caught.exception.code), 429)
                self.assertEqual(caught.exception.headers['Retry-After'], '300')
        self.assertIsNone(self.t.counters.get_cache(self.t._keys('synthetic-user').pair_counter))


if __name__ == '__main__':
    unittest.main(verbosity=2)
