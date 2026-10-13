"""Run offline login contracts against the installed, hash-verified 1.104.0 module.

Usage: python3 -I -B /path/to/tests/login_security/run_image_login_security.py
No arguments, source overlays or host test dependencies are required.
"""
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest

TARGET = 'proxy/auth/login_throttle.py'
EXPECTED = '87f280351406b2b308c42ef28fe95a8b7b09b849c2e2e4f45f30c56bd3b6decb'
TEST_SHA = 'd904cd72c907841af50ff5031081fca13c84b14f3becd0232490ff6ec021da7e'
TEST_COUNT = 16


def main():
    sys.dont_write_bytecode = True
    if len(sys.argv) != 1:
        raise SystemExit('No source override is accepted by the installed-image gate')
    test = Path(__file__).resolve().with_name('test_contract.py')
    spec = importlib.util.find_spec('litellm')
    if spec is None or not spec.submodule_search_locations:
        raise SystemExit('LiteLLM is not installed')
    root = Path(next(iter(spec.submodule_search_locations))).resolve()
    if root.is_relative_to(test.parent):
        raise SystemExit('Refusing test-directory runtime source')
    if importlib.metadata.version('litellm') != '1.104.0':
        raise SystemExit('Wrong installed LiteLLM version')
    target = root / TARGET
    if hashlib.sha256(target.read_bytes()).hexdigest() != EXPECTED:
        raise SystemExit('Installed login throttle hash mismatch; unpatched or unknown source refused')
    if hashlib.sha256(test.read_bytes()).hexdigest() != TEST_SHA:
        raise SystemExit('Login contract hash mismatch')
    # Ignore a host-provided development seam; always exercise installed bytes.
    os.environ['LITELLM_LOGIN_SECURITY_MODULE'] = str(target)
    spec = importlib.util.spec_from_file_location('woow_login_security_contract', test)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(module.LoginSecurityContract)
    if suite.countTestCases() != TEST_COUNT:
        raise SystemExit('Unexpected login contract count')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    ok = result.wasSuccessful() and result.testsRun == TEST_COUNT and not result.skipped
    print(json.dumps({'installed_hash_matches': True, 'test_hash_matches': True,
                      'tests_run': result.testsRun, 'skips': len(result.skipped),
                      'passed': ok, 'uid': os.geteuid(), 'redis_lua_runtime_tested': False}))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
