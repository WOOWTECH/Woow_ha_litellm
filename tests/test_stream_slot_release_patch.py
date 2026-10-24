"""Build-guard regressions for apply_stream_slot_release.py and lean tests of its stdlib-only helper (no
LiteLLM installed); runtime behaviour lives in api_compat/test_stream_slots.py."""
import asyncio
import contextlib
import hashlib
import importlib.util
import logging
from pathlib import Path
import shutil
import subprocess
import sys
import types
import pytest

REPO = Path(__file__).resolve().parents[1]
PATCHES = REPO / 'litellm/patches'
TARGET = 'proxy/common_request_processing.py'
HELPER_PATH = 'proxy/woow_stream_slots.py'
# What the helper calls into: the limiter and the budget reservation as the upstream guard leaves them,
# and ProxyLogging in pristine LiteLLM 1.104.0 (upstream.lock.json image), which no installer edits.
GUARD_DEPENDENCIES = ('proxy/hooks/parallel_request_limiter_v3.py', 'proxy/spend_tracking/budget_reservation.py')
PRISTINE_DEPENDENCIES = {'proxy/utils.py': '81e6fdba0c204a852883ac96a1b47ec2254c9fe97547f7e05bd0110e0d90933f'}
EARLIER = ('apply_responses_fix.py', 'apply_subscription_policy.py', 'apply_api_compat.py', 'apply_routes_auth.py',
           'apply_security_headers.py', 'apply_login_throttle.py', 'apply_api_errors.py',
           'apply_upstream_guard.py', 'apply_m4_batch1.py')


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


module = load('stream_slot_patch', PATCHES / 'apply_stream_slot_release.py')
slots = load('woow_stream_slots_lean', PATCHES / 'woow_stream_slots.py')
sha = lambda data: hashlib.sha256(data).hexdigest()
BEFORE, AFTER = 'value = 1\n', 'value = 2\n'
HELPER = 'helper = 1\n'
DEPENDENCY = 'limiter = 1\n'


def pin(before=BEFORE, after=AFTER, old='value = 1', new='value = 2', count=1):
    return (sha(before.encode()), sha(after.encode()), ((old, new, count),))


@pytest.fixture
def tree(tmp_path, monkeypatch):
    root, sources = tmp_path / 'litellm', tmp_path / 'patches'
    (root / 'proxy').mkdir(parents=True)
    sources.mkdir()
    for name in ('first.py', 'last.py'):
        (root / name).write_text(BEFORE)
    (root / 'proxy/limiter.py').write_text(DEPENDENCY)
    (sources / 'helper.py').write_text(HELPER)
    monkeypatch.setattr(module, '__file__', str(sources / 'apply_stream_slot_release.py'))
    monkeypatch.setattr(module, 'PATCHES', {'first.py': pin(), 'last.py': pin()})
    monkeypatch.setattr(module, 'HELPERS', {'proxy/helper.py': ('helper.py', sha(HELPER.encode()))})
    monkeypatch.setattr(module, 'DEPENDENCIES', {'proxy/limiter.py': sha(DEPENDENCY.encode())})
    return root


def untouched(root):
    return all((root / n).read_text() == BEFORE for n in ('first.py', 'last.py')) and not (root / 'proxy/helper.py').exists()


@pytest.mark.parametrize('failure', ['input_hash', 'context', 'count', 'output_hash', 'syntax'])
def test_validate_everything_before_any_write(tree, monkeypatch, failure):
    bad = {
        'input_hash': ('0' * 64,) + pin()[1:],
        'context': pin(old='missing context'),
        'count': pin(count=2),
        'output_hash': (pin()[0], '0' * 64, pin()[2]),
        'syntax': pin(after='value = [\n', new='value = ['),
    }[failure]
    monkeypatch.setitem(module.PATCHES, 'last.py', bad)
    with pytest.raises((SystemExit, SyntaxError)):
        module.apply(tree)
    assert untouched(tree)


@pytest.mark.parametrize('kind', ['tampered', 'missing', 'symlink'])
def test_dependencies_are_verified_first(tree, kind):
    dependency = tree / 'proxy/limiter.py'
    if kind == 'tampered':
        dependency.write_text('limiter = 2\n')
    elif kind == 'missing':
        dependency.unlink()
    else:
        (tree / 'proxy/elsewhere.py').write_text(DEPENDENCY)
        dependency.unlink()
        dependency.symlink_to(tree / 'proxy/elsewhere.py')
    with pytest.raises(SystemExit, match='dependency'):
        module.apply(tree)
    assert untouched(tree)


def test_tampered_or_symlinked_helper_is_refused_without_writes(tree, monkeypatch):
    monkeypatch.setitem(module.HELPERS, 'proxy/helper.py', ('helper.py', '0' * 64))
    with pytest.raises(SystemExit, match='helper source'):
        module.apply(tree)
    assert untouched(tree)
    monkeypatch.setitem(module.HELPERS, 'proxy/helper.py', ('helper.py', sha(HELPER.encode())))
    source = Path(module.__file__).with_name('helper.py')
    (source.parent / 'elsewhere.py').write_text(HELPER)
    source.unlink()
    source.symlink_to(source.parent / 'elsewhere.py')
    with pytest.raises(SystemExit, match='symlink helper source'):
        module.apply(tree)
    assert untouched(tree)


@pytest.mark.parametrize('kind', ['symlink', 'missing'])
def test_symlink_or_missing_target_is_refused(tree, kind):
    (tree / 'first.py').unlink()
    if kind == 'symlink':
        (tree / 'elsewhere.py').write_text(BEFORE)
        (tree / 'first.py').symlink_to(tree / 'elsewhere.py')
    with pytest.raises(SystemExit, match='missing or symlink target'):
        module.apply(tree)
    assert (tree / 'last.py').read_text() == BEFORE and not (tree / 'proxy/helper.py').exists()


def test_success_then_reapplication_is_refused(tree):
    module.apply(tree)
    assert (tree / 'first.py').read_text() == AFTER and (tree / 'last.py').read_text() == AFTER
    assert (tree / 'proxy/helper.py').read_text() == HELPER
    with pytest.raises(SystemExit, match='Refusing changed upstream source'):
        module.apply(tree)
    assert (tree / 'proxy/helper.py').read_text() == HELPER


def test_existing_helper_is_refused_even_with_pristine_targets(tree):
    (tree / 'proxy/helper.py').write_text('planted = True\n')
    with pytest.raises(SystemExit, match='existing helper'):
        module.apply(tree)
    assert (tree / 'first.py').read_text() == BEFORE and (tree / 'proxy/helper.py').read_text() == 'planted = True\n'


def test_pinned_helper_and_dependencies_are_the_reviewed_files():
    assert module.HELPERS == {HELPER_PATH: ('woow_stream_slots.py', sha((PATCHES / 'woow_stream_slots.py').read_bytes()))}
    guard = load('guard_for_stream_slots', PATCHES / 'apply_upstream_guard.py')
    assert module.DEPENDENCIES == {**{name: guard.PATCHES[name][1] for name in GUARD_DEPENDENCIES}, **PRISTINE_DEPENDENCIES}


def test_helper_imports_only_the_standard_library_at_module_level():
    top = [line for line in (PATCHES / 'woow_stream_slots.py').read_text().splitlines()
           if line.startswith(('import ', 'from '))]
    assert top and not any(name in line for line in top for name in ('litellm', 'anyio', 'fastapi', 'starlette'))


def test_edits_fork_only_after_the_stash_exists_and_wrap_the_request_once():
    (_, _, edits), = module.PATCHES.values()
    assert [count for _, _, count in edits] == [1, 1, 1]
    s1 = edits[0][1]
    # Without a keepalive interval the request still runs inline, untouched; with one, the stash comes first.
    assert s1.index('        return await produce_response\n') < s1.index('    ensure_request_stash()\n') < s1.index(
        '    produce_task: Final = asyncio.ensure_future(produce_response)\n')
    opening, closing = edits[1][1], edits[2][1]
    assert opening.endswith('            release_slot_if_cancelled(\n                proxy_logging_obj,\n'
                            '                user_api_key_dict,\n                self._process_llm_request(\n')
    assert closing.startswith('                skip_pre_call_logic=skip_pre_call_logic,\n                ),\n            ),\n')


def test_tampered_real_target_is_refused_without_writes(tmp_path, monkeypatch):
    root = tmp_path / 'litellm'
    for name in (*module.PATCHES, *module.DEPENDENCIES):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text('# not the reviewed LiteLLM source\n')
    with pytest.raises(SystemExit, match='Refusing unexpected dependency'):
        module.apply(root)
    monkeypatch.setattr(module, 'DEPENDENCIES', {})
    with pytest.raises(SystemExit, match='Refusing changed upstream source'):
        module.apply(root)
    assert all((root / n).read_text() == '# not the reviewed LiteLLM source\n' for n in module.PATCHES)
    assert not any((root / h).exists() for h in module.HELPERS)


def test_chain_order_gives_both_images_the_same_inputs():
    """HA (subscription policy, restore fix) and PaaS chains hand the target over with one hash."""
    assert set(module.PATCHES) == {TARGET}
    batch1 = load('batch1_for_stream_slots', PATCHES / 'apply_m4_batch1.py')
    assert module.PATCHES[TARGET][0] == batch1.PATCHES[TARGET][1]
    earlier = '\n'.join((PATCHES / installer).read_text() for installer in EARLIER)
    assert 'router.py' in earlier and TARGET in earlier  # the scan sees them
    assert not any(name in earlier for name in PRISTINE_DEPENDENCIES)  # no installer edits ProxyLogging
    assert not any(name in earlier for name in module.HELPERS)
    api = load('api_compat_for_stream_slots', PATCHES / 'apply_api_compat.py')
    routes = load('routes_for_stream_slots', PATCHES / 'apply_routes_auth.py')
    errors = load('api_errors_for_stream_slots', PATCHES / 'apply_api_errors.py')
    guard = load('guard_for_stream_slots_chain', PATCHES / 'apply_upstream_guard.py')
    harness = load('harness_for_stream_slots', REPO / 'tests/api_contract_harness/run_image_contracts.py')
    final = harness.final_api_patches(api, routes, errors, guard, batch1, module)
    assert final[TARGET][1] == module.PATCHES[TARGET][1]
    assert final[TARGET][0] == api.PATCHES[TARGET][0]  # still verified from the pristine API-compat input
    assert final[HELPER_PATH][1] == module.HELPERS[HELPER_PATH][1]
    assert all(final[name][1] == expected for name, expected in module.DEPENDENCIES.items())  # the image is checked too
    before_it = harness.final_api_patches(api, routes, errors, guard, batch1)
    assert set(final) - set(before_it) == {HELPER_PATH, *PRISTINE_DEPENDENCIES}
    assert all(final[name] == entry for name, entry in before_it.items() if name != TARGET)
    for broken in ({'PATCHES': {TARGET: ('0' * 64, '1' * 64, ())}, 'HELPERS': {}, 'DEPENDENCIES': {}},
                   {'PATCHES': {}, 'HELPERS': {}, 'DEPENDENCIES': {GUARD_DEPENDENCIES[0]: '0' * 64}}):
        with pytest.raises(RuntimeError, match='Stream slot patch'):
            harness.final_api_patches(api, routes, errors, guard, batch1, type('Broken', (), broken))


def test_dockerfile_runs_it_last_and_ci_gate_ships_it():
    docker = (REPO / 'litellm/Dockerfile').read_text()
    runs = [line for line in docker.splitlines() if line.startswith('RUN python -I /usr/share/woow-litellm/patches/')]
    assert runs[-1] == 'RUN python -I /usr/share/woow-litellm/patches/apply_stream_slot_release.py'
    assert runs[-2] == 'RUN python -I /usr/share/woow-litellm/patches/apply_m4_batch1.py'
    assert docker.index('COPY patches/ /usr/share/woow-litellm/patches/') < docker.index(runs[-1])
    assert 'litellm/patches/apply_stream_slot_release.py' in (REPO / 'tools/ci-api-contracts.sh').read_text()
    assert "'apply_stream_slot_release.py'" in (REPO / 'tests/api_contract_harness/run_image_contracts.py').read_text()


def test_installer_cli_refuses_extra_arguments(tmp_path):
    copy = tmp_path / 'apply_stream_slot_release.py'
    shutil.copy(PATCHES / 'apply_stream_slot_release.py', copy)
    result = subprocess.run([sys.executable, '-I', str(copy), 'a', 'b'], capture_output=True, text=True)
    assert result.returncode != 0 and 'Usage: apply_stream_slot_release.py' in result.stderr


# --- the helper, with its LiteLLM and anyio imports registered as fakes ----------------------------


class Proxy:
    def __init__(self, fail=False):
        self.released, self.fail = [], fail

    async def _arelease_max_parallel_requests_on_disconnect(self, user_api_key_dict):
        self.released.append(user_api_key_dict)
        if self.fail:
            raise RuntimeError('limiter cache unavailable')


@pytest.fixture
def installed(monkeypatch):
    calls = types.SimpleNamespace(stash=0, reservations=[], shields=[], budget_fail=False)
    limiter = types.ModuleType('litellm.proxy.hooks.parallel_request_limiter_v3')

    def get_or_create_request_stash():
        calls.stash += 1
    limiter.get_or_create_request_stash = get_or_create_request_stash
    budget = types.ModuleType('litellm.proxy.spend_tracking.budget_reservation')

    async def release_budget_reservation_on_cancel(reservation):
        calls.reservations.append(reservation)
        if calls.budget_fail:
            raise RuntimeError('spend counter unavailable')
    budget.release_budget_reservation_on_cancel = release_budget_reservation_on_cancel
    anyio = types.ModuleType('anyio')

    @contextlib.contextmanager
    def cancel_scope(shield=False):
        calls.shields.append(shield)
        yield
    anyio.CancelScope = cancel_scope
    for fake in (limiter, budget, anyio):
        monkeypatch.setitem(sys.modules, fake.__name__, fake)
    return calls


KEY = types.SimpleNamespace(budget_reservation={'reservation_id': 'fixture'})


def run_cancelled(proxy, key=KEY):
    async def main():
        started = asyncio.Event()

        async def pending_upstream():
            started.set()
            await asyncio.Event().wait()  # the upstream answer that never comes
        task = asyncio.ensure_future(slots.release_slot_if_cancelled(proxy, key, pending_upstream()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return task.cancelled()
    return asyncio.run(main())


def test_ensure_request_stash_creates_it_through_the_limiter(installed):
    slots.ensure_request_stash()
    assert installed.stash == 1


def test_a_completed_or_failed_request_passes_through_and_releases_nothing(installed):
    proxy = Proxy()

    async def answer():
        return 'response'

    async def provider_error():
        raise ValueError('upstream 500')
    assert asyncio.run(slots.release_slot_if_cancelled(proxy, KEY, answer())) == 'response'
    with pytest.raises(ValueError, match='upstream 500'):
        asyncio.run(slots.release_slot_if_cancelled(proxy, KEY, provider_error()))
    assert proxy.released == [] and installed.reservations == [] and installed.shields == []


def test_a_cancelled_request_releases_its_slot_and_reservation_shielded_then_stays_cancelled(installed):
    proxy = Proxy()
    assert run_cancelled(proxy) is True
    assert proxy.released == [KEY] and installed.reservations == [KEY.budget_reservation]
    assert installed.shields == [True]


def test_a_key_without_a_reservation_still_has_its_slot_released(installed):
    proxy, key = Proxy(), types.SimpleNamespace()
    assert run_cancelled(proxy, key) is True
    assert proxy.released == [key] and installed.reservations == [None]


def test_release_failures_are_logged_and_never_replace_the_cancellation(installed, caplog):
    caplog.set_level(logging.WARNING, logger='LiteLLM Proxy')
    installed.budget_fail = True
    proxy = Proxy(fail=True)
    assert run_cancelled(proxy) is True
    assert proxy.released == [KEY] and installed.reservations == [KEY.budget_reservation]  # the second still ran
    messages = [record.getMessage() for record in caplog.records if record.name == 'LiteLLM Proxy']
    assert messages == ['Woow API-09: max_parallel_requests slot of a cancelled request not released: limiter cache unavailable',
                        'Woow API-09: budget reservation of a cancelled request not reconciled: spend counter unavailable']
