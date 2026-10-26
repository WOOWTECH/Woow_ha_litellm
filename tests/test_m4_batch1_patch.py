"""Build-guard regressions for apply_m4_batch1.py; runtime behaviour lives in api_compat/test_m4_batch1.py."""
import hashlib
import importlib.util
from pathlib import Path
import re
import shutil
import subprocess
import sys
import pytest

REPO = Path(__file__).resolve().parents[1]
PATCHES = REPO / 'litellm/patches'
# Pristine LiteLLM 1.104.0 (upstream.lock.json image) bytes of the targets no earlier installer edits.
PRISTINE = {
    'proxy/auth/route_checks.py': 'e159ec69303ba8654062e368ee79376cc5427d9b7999d13942ab8eacae703422',
    'proxy/common_utils/responses_stream_errors.py': '9a029357d771bb45d7e139de7e4864579c33bdf3ada622861dd257616622b12f',
    'proxy/response_api_endpoints/endpoints.py': 'a47693f778012379f7792d1dddfd19aef45c8306ea97e2221bcb6dc4405bae65',
    'anthropic_interface/exceptions/exception_mapping_utils.py':
        'c8254454f2fe5c9992007aae3acebfc989796010c5ca9e6bc4dd0e4e8479e47e',
    'llms/chatgpt/common_utils.py': '90e7ecbc8802ad1ec1d3b287145857699ccd2e7671f97f10c264f617b558f4aa',
    'proxy/health_check.py': '2e3160d3332332f60c4ddcb9500740a4b1bdd923d299e445c72808578397873d',
    'proxy/credential_endpoints/endpoints.py': 'bff9241d37e9715cfbd64891dc198a30ecedc13a94f2335e9bdc9f4cf1d52b68',
    'proxy/management_endpoints/model_management_endpoints.py':
        'e9262533fe0fc73d676bdfd9e41f7c03d0478727ebf23298c878ee101e8a5e5e',
}
# Targets an earlier installer already edits: the input is that installer's reviewed output.
SHARED = {
    'proxy/proxy_server.py': 'apply_api_errors.py',
    'proxy/common_request_processing.py': 'apply_upstream_guard.py',
    'proxy/health_endpoints/_health_endpoints.py': 'apply_upstream_guard.py',
    'llms/anthropic/experimental_pass_through/adapters/streaming_iterator.py': 'apply_upstream_guard.py',
}
EARLIER = ('apply_responses_fix.py', 'apply_subscription_policy.py', 'apply_api_compat.py', 'apply_routes_auth.py',
           'apply_security_headers.py', 'apply_login_throttle.py', 'apply_api_errors.py',
           'apply_upstream_guard.py')


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


module = load('m4_batch1_patch', PATCHES / 'apply_m4_batch1.py')
sha = lambda data: hashlib.sha256(data).hexdigest()
BEFORE, AFTER = 'value = 1\n', 'value = 2\n'
HELPER = 'helper = 1\n'
DEPENDENCY = 'guard = 1\n'


def pin(before=BEFORE, after=AFTER, old='value = 1', new='value = 2', count=1):
    return (sha(before.encode()), sha(after.encode()), ((old, new, count),))


@pytest.fixture
def tree(tmp_path, monkeypatch):
    root, sources = tmp_path / 'litellm', tmp_path / 'patches'
    (root / 'core').mkdir(parents=True)
    sources.mkdir()
    for name in ('first.py', 'last.py'):
        (root / name).write_text(BEFORE)
    (root / 'core/guard.py').write_text(DEPENDENCY)
    (sources / 'helper.py').write_text(HELPER)
    monkeypatch.setattr(module, '__file__', str(sources / 'apply_m4_batch1.py'))
    monkeypatch.setattr(module, 'PATCHES', {'first.py': pin(), 'last.py': pin()})
    monkeypatch.setattr(module, 'HELPERS', {'core/helper.py': ('helper.py', sha(HELPER.encode()))})
    monkeypatch.setattr(module, 'DEPENDENCIES', {'core/guard.py': sha(DEPENDENCY.encode())})
    return root


def untouched(root):
    return all((root / n).read_text() == BEFORE for n in ('first.py', 'last.py')) and not (root / 'core/helper.py').exists()


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
def test_upstream_guard_dependency_is_verified_first(tree, kind):
    dependency = tree / 'core/guard.py'
    if kind == 'tampered':
        dependency.write_text('guard = 2\n')
    elif kind == 'missing':
        dependency.unlink()
    else:
        (tree / 'core/elsewhere.py').write_text(DEPENDENCY)
        dependency.unlink()
        dependency.symlink_to(tree / 'core/elsewhere.py')
    with pytest.raises(SystemExit, match='dependency'):
        module.apply(tree)
    assert untouched(tree)


def test_tampered_or_symlinked_helper_is_refused_without_writes(tree, monkeypatch):
    monkeypatch.setitem(module.HELPERS, 'core/helper.py', ('helper.py', '0' * 64))
    with pytest.raises(SystemExit, match='helper source'):
        module.apply(tree)
    assert untouched(tree)
    monkeypatch.setitem(module.HELPERS, 'core/helper.py', ('helper.py', sha(HELPER.encode())))
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
    assert (tree / 'last.py').read_text() == BEFORE and not (tree / 'core/helper.py').exists()


def test_success_then_reapplication_is_refused(tree):
    module.apply(tree)
    assert (tree / 'first.py').read_text() == AFTER and (tree / 'last.py').read_text() == AFTER
    assert (tree / 'core/helper.py').read_text() == HELPER
    with pytest.raises(SystemExit, match='Refusing changed upstream source'):
        module.apply(tree)
    assert (tree / 'core/helper.py').read_text() == HELPER


def test_existing_helper_is_refused_even_with_pristine_targets(tree):
    (tree / 'core/helper.py').write_text('planted = True\n')
    with pytest.raises(SystemExit, match='existing helper'):
        module.apply(tree)
    assert (tree / 'first.py').read_text() == BEFORE and (tree / 'core/helper.py').read_text() == 'planted = True\n'


def test_pinned_helper_and_dependency_are_the_reviewed_files():
    assert module.HELPERS == {'litellm_core_utils/woow_client_guard.py': (
        'woow_client_guard.py', sha((PATCHES / 'woow_client_guard.py').read_bytes()))}
    guard = load('guard_for_batch1', PATCHES / 'apply_upstream_guard.py')
    assert module.DEPENDENCIES == {name: guard.HELPERS[name][1] for name in ('litellm_core_utils/woow_provider_errors.py',)}
    assert sha((PATCHES / 'woow_provider_errors.py').read_bytes()) == module.DEPENDENCIES[
        'litellm_core_utils/woow_provider_errors.py']


def test_helper_imports_only_the_standard_library_at_module_level():
    top = [line for line in (PATCHES / 'woow_client_guard.py').read_text().splitlines()
           if line.startswith(('import ', 'from '))]
    assert top and not any('litellm' in line or 'fastapi' in line for line in top)


def test_every_edit_has_a_reviewed_count_and_hash():
    for name, (before, after, edits) in module.PATCHES.items():
        assert len(before) == len(after) == 64 and before != after, name
        assert edits and all(count >= 1 and old != new for old, new, count in edits), name
    # Only the two identical SSE error frames of proxy_server.async_data_generator are replaced twice.
    counts = {(name, count) for name, (_, _, edits) in module.PATCHES.items() for _, _, count in edits if count != 1}
    assert counts == {('proxy/proxy_server.py', 2)}


def test_tampered_real_target_is_refused_without_writes(tmp_path):
    root = tmp_path / 'litellm'
    for name in (*module.PATCHES, *module.DEPENDENCIES):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text('# not the reviewed LiteLLM source\n')
    (root / 'litellm_core_utils/woow_provider_errors.py').write_bytes((PATCHES / 'woow_provider_errors.py').read_bytes())
    with pytest.raises(SystemExit, match='Refusing changed upstream source'):
        module.apply(root)
    assert all((root / n).read_text() == '# not the reviewed LiteLLM source\n' for n in module.PATCHES)
    assert not any((root / h).exists() for h in module.HELPERS)


def _paths_named_by(installer):
    """Every 'dir/file.py' string literal an earlier installer names (targets, helpers, dependencies)."""
    return set(re.findall(r"""['"]([a-z_]+(?:/[a-z_.]+)*\.py)['"]""", (PATCHES / installer).read_text()))


def test_chain_order_gives_both_images_the_same_inputs():
    """HA (subscription policy, restore fix) and PaaS chains hand every target over with one hash."""
    earlier = set().union(*(_paths_named_by(installer) for installer in EARLIER))
    assert 'router.py' in earlier and 'proxy/woow_api_errors.py' in earlier  # the scan sees them
    assert set(module.PATCHES) & earlier == set(SHARED)
    assert 'router.py' not in module.PATCHES and 'woow_chatgpt.py' not in module.PATCHES
    for name, installer in SHARED.items():
        assert module.PATCHES[name][0] == load('earlier_' + installer[:-3], PATCHES / installer).PATCHES[name][1], name
    assert {name: before for name, (before, _, _) in module.PATCHES.items() if name not in SHARED} == PRISTINE
    assert not set(module.HELPERS) & (earlier | set(module.PATCHES))
    api = load('api_compat_for_batch1', PATCHES / 'apply_api_compat.py')
    routes = load('routes_for_batch1', PATCHES / 'apply_routes_auth.py')
    errors = load('api_errors_for_batch1', PATCHES / 'apply_api_errors.py')
    guard = load('guard_for_batch1_chain', PATCHES / 'apply_upstream_guard.py')
    harness = load('harness_for_batch1', REPO / 'tests/api_contract_harness/run_image_contracts.py')
    final = harness.final_api_patches(api, routes, errors, guard, module)
    assert all(final[name][1] == after for name, (_, after, _) in module.PATCHES.items())
    assert final['litellm_core_utils/woow_client_guard.py'][1] == module.HELPERS['litellm_core_utils/woow_client_guard.py'][1]
    assert final['proxy/proxy_server.py'][0] == api.PATCHES['proxy/proxy_server.py'][0]  # still from the pristine input
    for broken in ({'PATCHES': {'proxy/proxy_server.py': ('0' * 64, '1' * 64, ())}, 'HELPERS': {}, 'DEPENDENCIES': {}},
                   {'PATCHES': {}, 'HELPERS': {}, 'DEPENDENCIES': {'litellm_core_utils/woow_provider_errors.py': '0' * 64}}):
        with pytest.raises(RuntimeError, match='M4 batch 1 patch'):
            harness.final_api_patches(api, routes, errors, guard, type('Broken', (), broken))


def test_dockerfile_runs_it_last_and_ci_gate_ships_it():
    docker = (REPO / 'litellm/Dockerfile').read_text()
    runs = [line for line in docker.splitlines() if line.startswith('RUN python -I /usr/share/woow-litellm/patches/')]
    # The stream slot release (apply_stream_slot_release.py) builds on batch 1 and precedes the final ChatGPT role normalization.
    assert runs[-2] == 'RUN python -I /usr/share/woow-litellm/patches/apply_stream_slot_release.py'
    assert runs[-3] == 'RUN python -I /usr/share/woow-litellm/patches/apply_m4_batch1.py'
    assert runs[-4] == 'RUN python -I /usr/share/woow-litellm/patches/apply_upstream_guard.py'
    assert docker.index('COPY patches/ /usr/share/woow-litellm/patches/') < docker.index(runs[-3])
    assert 'litellm/patches/apply_m4_batch1.py' in (REPO / 'tools/ci-api-contracts.sh').read_text()
    assert "'apply_m4_batch1.py'" in (REPO / 'tests/api_contract_harness/run_image_contracts.py').read_text()


def test_installer_cli_refuses_extra_arguments(tmp_path):
    copy = tmp_path / 'apply_m4_batch1.py'
    shutil.copy(PATCHES / 'apply_m4_batch1.py', copy)
    result = subprocess.run([sys.executable, '-I', str(copy), 'a', 'b'], capture_output=True, text=True)
    assert result.returncode != 0 and 'Usage: apply_m4_batch1.py' in result.stderr
