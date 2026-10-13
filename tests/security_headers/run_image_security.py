"""Read-only image gate for reviewed security commit 73e198e; no source overlay."""
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest

COMMIT = '73e198e81754126465a92bace206c56833f45664'
TARGET = 'proxy/middleware/security_headers_middleware.py'
PAYLOAD_SHA = '7eae4c58ca47b08600fef20ba0d72241a48bfa67571731ed611574b2072897c8'
TEST_SHA = '8cf4ca6d59486c96d8c876ac7c0c85c463d10cba018a7004dedf8426656b0c89'
TEST = Path('/security-contracts/test_headers.py')


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def offline(event, args):
    if event in {'socket.connect', 'socket.connect_ex', 'socket.getaddrinfo',
                 'socket.bind', 'socket.sendto', 'subprocess.Popen', 'os.system',
                 'os.posix_spawn', 'os.fork', 'os.forkpty', 'os.exec'}:
        raise PermissionError('Security image gate forbids network and child processes')


def main():
    sys.dont_write_bytecode = True
    spec = importlib.util.find_spec('litellm')
    require(spec is not None and spec.submodule_search_locations, 'LiteLLM not installed')
    root = Path(next(iter(spec.submodule_search_locations))).resolve()
    require(not root.is_relative_to(TEST.parent), 'Runtime source must not be in test mount')
    require(importlib.metadata.version('litellm') == '1.104.0', 'Wrong runtime version')
    target = root / TARGET
    require(hashlib.sha256(target.read_bytes()).hexdigest() == PAYLOAD_SHA, 'Installed middleware hash mismatch')
    require(hashlib.sha256(TEST.read_bytes()).hexdigest() == TEST_SHA, 'Reviewed contract hash mismatch')
    os.environ['LITELLM_SECURITY_MODULE'] = str(target)
    sys.addaudithook(offline)
    test_spec = importlib.util.spec_from_file_location('reviewed_security_contract', TEST)
    module = importlib.util.module_from_spec(test_spec)
    test_spec.loader.exec_module(module)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(module.HeadersContract)
    require(suite.countTestCases() == 15, 'Unexpected contract count')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    ok = result.wasSuccessful() and result.testsRun == 15 and not result.skipped
    print(json.dumps({'security_commit': COMMIT, 'installed_hash_matches': True,
                      'test_hash_matches': True, 'tests_run': result.testsRun,
                      'skips': len(result.skipped), 'passed': ok, 'uid': os.geteuid()}))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
