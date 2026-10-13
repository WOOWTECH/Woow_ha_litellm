"""Run contracts against installed image code, with only a hash-pinned pytest overlay."""
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import zipfile

HERE = Path(__file__).resolve().parent
CONTRACTS = Path('/contracts')


def unpack_wheels(wheels, lock, destination):
    expected = {row['filename'] for row in lock}
    if {p.name for p in wheels.glob('*.whl')} != expected:
        raise RuntimeError('Pytest wheel set differs from lock')
    for row in lock:
        wheel = wheels / row['filename']
        if hashlib.sha256(wheel.read_bytes()).hexdigest() != row['sha256']:
            raise RuntimeError('Pytest wheel hash mismatch: ' + row['name'])
    for row in lock:
        with zipfile.ZipFile(wheels / row['filename']) as archive:
            for member in archive.namelist():
                if not (destination / member).resolve().is_relative_to(destination.resolve()):
                    raise RuntimeError('Unsafe pytest wheel member')
            archive.extractall(destination)


def verify_patched_source(root, patches):
    for name, (_, expected, _) in patches.items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError('Built image API patch mismatch: ' + name)


def offline_only(event, args):
    if event in {'socket.connect', 'socket.connect_ex', 'socket.getaddrinfo', 'socket.bind',
                 'socket.sendto', 'subprocess.Popen', 'os.system', 'os.posix_spawn',
                 'os.fork', 'os.forkpty', 'os.exec'}:
        raise PermissionError('Built-image API contracts forbid network and child processes')


class RequireExecutedTests:
    def __init__(self):
        self.skipped = 0

    def pytest_collectreport(self, report):
        self.skipped += int(report.skipped)

    def pytest_runtest_logreport(self, report):
        self.skipped += int(report.skipped)

    def pytest_sessionfinish(self, session, exitstatus):
        if self.skipped or session.testscollected == 0:
            session.exitstatus = 1


def main():
    os.environ.update(LITELLM_LOCAL_MODEL_COST_MAP='True', LITELLM_TELEMETRY='False',
                      DO_NOT_TRACK='1', CHECKPOINT_DISABLE='1', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    sys.dont_write_bytecode = True
    lock = json.loads((HERE / 'wheels.lock.json').read_text())
    with tempfile.TemporaryDirectory(prefix='api-contract-pytest-') as directory:
        overlay = Path(directory)
        unpack_wheels(HERE / 'wheels', lock, overlay)
        sys.path.insert(0, str(overlay))
        for row in lock:
            if importlib.metadata.version(row['name']) != row['version']:
                raise RuntimeError('Unexpected pytest harness dependency: ' + row['name'])
        spec = importlib.util.find_spec('litellm')
        if spec is None or not spec.submodule_search_locations:
            raise RuntimeError('Built image does not contain LiteLLM')
        root = Path(next(iter(spec.submodule_search_locations))).resolve()
        if any(root.is_relative_to(p) for p in (HERE, overlay, CONTRACTS)):
            raise RuntimeError('LiteLLM must resolve from the built image, not mounted test code')
        patch_spec = importlib.util.spec_from_file_location('api_patch', HERE / 'apply_api_compat.py')
        patch = importlib.util.module_from_spec(patch_spec)
        patch_spec.loader.exec_module(patch)
        verify_patched_source(root, patch.PATCHES)
        print(json.dumps({'litellm_version': importlib.metadata.version('litellm'),
                          'source': str(root), 'verified_patch_files': len(patch.PATCHES),
                          'harness': {r['name']: r['version'] for r in lock}}, sort_keys=True), flush=True)
        sys.addaudithook(offline_only)
        import pytest
        return int(pytest.main(['-q', '-p', 'no:cacheprovider', '--confcutdir=' + str(CONTRACTS),
                               '--rootdir=' + str(CONTRACTS), '--basetemp=' + str(overlay / 'test-tmp'), str(CONTRACTS)],
                              plugins=[RequireExecutedTests()]))


if __name__ == '__main__':
    raise SystemExit(main())
