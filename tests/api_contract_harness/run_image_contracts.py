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


def final_api_patches(api_patch, routes_patch, errors_patch, guard_patch, batch1_patch=None, stream_patch=None):
    patches = dict(api_patch.PATCHES)
    before, after, edits = patches[routes_patch.TARGET]
    if after != routes_patch.EXPECTED:
        raise RuntimeError('Routes auth patch must follow the exact API patch output')
    patches[routes_patch.TARGET] = (before, routes_patch.RESULT, edits)
    # The API errors patch runs last; on shared files it must start from the exact output above.
    for name, (errors_before, errors_after, errors_edits) in errors_patch.PATCHES.items():
        if name in patches and patches[name][1] != errors_before:
            raise RuntimeError('API errors patch must follow the exact API patch output: ' + name)
        patches[name] = (patches[name][0] if name in patches else errors_before, errors_after, errors_edits)
    patches[errors_patch.HELPER] = (None, errors_patch.HELPER_SHA256, ())
    # The upstream guard runs after it; a file it shares must start from the exact output above.
    for name, (guard_before, guard_after, guard_edits) in guard_patch.PATCHES.items():
        if name in patches and patches[name][1] != guard_before:
            raise RuntimeError('Upstream guard patch must follow the exact API errors output: ' + name)
        patches[name] = (patches[name][0] if name in patches else guard_before, guard_after, guard_edits)
    for name, (_, helper_hash) in guard_patch.HELPERS.items():
        patches[name] = (None, helper_hash, ())
    if batch1_patch is None:
        return patches
    # M4 batch 1 runs last; a file it shares with the chain above must start from its exact output,
    # and the upstream guard helper it builds on must be the one installed above.
    for name, expected in batch1_patch.DEPENDENCIES.items():
        if name not in patches or patches[name][1] != expected:
            raise RuntimeError('M4 batch 1 patch must follow the exact upstream guard output: ' + name)
    for name, (batch_before, batch_after, batch_edits) in batch1_patch.PATCHES.items():
        if name in patches and patches[name][1] != batch_before:
            raise RuntimeError('M4 batch 1 patch must follow the exact upstream guard output: ' + name)
        patches[name] = (patches[name][0] if name in patches else batch_before, batch_after, batch_edits)
    for name, (_, helper_hash) in batch1_patch.HELPERS.items():
        patches[name] = (None, helper_hash, ())
    if stream_patch is None:
        return patches
    # The stream slot release runs after it; its target must start from the exact batch 1 output. Of
    # the files its helper calls into, those an installer above edits must be that installer's output;
    # the pristine one (no installer edits it) is verified in the image as it is.
    for name, expected in stream_patch.DEPENDENCIES.items():
        if name in patches and patches[name][1] != expected:
            raise RuntimeError('Stream slot patch must follow the exact M4 batch 1 output: ' + name)
        patches.setdefault(name, (expected, expected, ()))
    for name, (stream_before, stream_after, stream_edits) in stream_patch.PATCHES.items():
        if name in patches and patches[name][1] != stream_before:
            raise RuntimeError('Stream slot patch must follow the exact M4 batch 1 output: ' + name)
        patches[name] = (patches[name][0] if name in patches else stream_before, stream_after, stream_edits)
    for name, (_, helper_hash) in stream_patch.HELPERS.items():
        patches[name] = (None, helper_hash, ())
    return patches


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
        routes_spec = importlib.util.spec_from_file_location('routes_patch', HERE / 'apply_routes_auth.py')
        routes_patch = importlib.util.module_from_spec(routes_spec)
        routes_spec.loader.exec_module(routes_patch)
        errors_spec = importlib.util.spec_from_file_location('errors_patch', HERE / 'apply_api_errors.py')
        errors_patch = importlib.util.module_from_spec(errors_spec)
        errors_spec.loader.exec_module(errors_patch)
        guard_spec = importlib.util.spec_from_file_location('guard_patch', HERE / 'apply_upstream_guard.py')
        guard_patch = importlib.util.module_from_spec(guard_spec)
        guard_spec.loader.exec_module(guard_patch)
        batch1_spec = importlib.util.spec_from_file_location('batch1_patch', HERE / 'apply_m4_batch1.py')
        batch1_patch = importlib.util.module_from_spec(batch1_spec)
        batch1_spec.loader.exec_module(batch1_patch)
        stream_spec = importlib.util.spec_from_file_location('stream_patch', HERE / 'apply_stream_slot_release.py')
        stream_patch = importlib.util.module_from_spec(stream_spec)
        stream_spec.loader.exec_module(stream_patch)
        final_patches = final_api_patches(patch, routes_patch, errors_patch, guard_patch, batch1_patch, stream_patch)
        verify_patched_source(root, final_patches)
        print(json.dumps({'litellm_version': importlib.metadata.version('litellm'),
                          'source': str(root), 'verified_patch_files': len(final_patches),
                          'harness': {r['name']: r['version'] for r in lock}}, sort_keys=True), flush=True)
        sys.addaudithook(offline_only)
        import pytest
        return int(pytest.main(['-q', '-p', 'no:cacheprovider', '--confcutdir=' + str(CONTRACTS),
                               '--rootdir=' + str(CONTRACTS), '--basetemp=' + str(overlay / 'test-tmp'), str(CONTRACTS)],
                              plugins=[RequireExecutedTests()]))


if __name__ == '__main__':
    raise SystemExit(main())
