"""Build-guard regressions for apply_upstream_guard.py; runtime behaviour lives in api_compat."""
import hashlib
import importlib.util
from pathlib import Path
import re
import shutil
import pytest

REPO = Path(__file__).resolve().parents[1]
PATCHES = REPO / 'litellm/patches'
SHARED = 'proxy/common_request_processing.py'
# Pristine LiteLLM 1.104.0 (upstream.lock.json) bytes of the targets no earlier installer edits.
PRISTINE = {
    'litellm_core_utils/exception_mapping_utils.py': '7896a70c295d5730a975fc7edee30d91821bd9d6e09830bafd280f5a1c2da771',
    'llms/openrouter/chat/transformation.py': 'f6cf4733c03c0b993f009dadc3fc9636434ad47cee20a4cefde2c70078578bec',
    'llms/anthropic/experimental_pass_through/adapters/streaming_iterator.py':
        '928654271696a04b87d3adbba9313d23fbb66cd3250fba5e3281515162a3b69b',
    'litellm_core_utils/llm_response_utils/get_headers.py': 'fb2922ad9bccad07db19070eb08ecbe8db4aa6203bb2b36b35e09d5b02e2ce78',
    'litellm_core_utils/core_helpers.py': '045e8d1e77e7e152da8eb21b0bb6e5dcc3e88a685b8b984357e69638dbf03d8a',
    'proxy/spend_tracking/budget_reservation.py': '433bf0a8f2e4ed5d7301c49c4f7d7151da65ed509157429b85bb32f53869cca5',
    'proxy/health_endpoints/_health_endpoints.py': '8ea1e58c7ee57f77e21e7dd4e711f18ef135a485e38564cf0958ff36c0e92a03',
    'proxy/auth/auth_exception_handler.py': '9da190e8c1f5db0287f420c20e875069eae16b6af76b8188378600e7347998a2',
    'proxy/hooks/parallel_request_limiter_v3.py': '90c865d2b1912ef25c90f988ccd5b56b452edc5f0e57c7320b864c542dd9b8ec',
}
EARLIER = ('apply_responses_fix.py', 'apply_subscription_policy.py', 'apply_api_compat.py', 'apply_routes_auth.py',
           'apply_security_headers.py', 'apply_login_throttle.py', 'apply_api_errors.py')


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


module = load('upstream_guard_patch', PATCHES / 'apply_upstream_guard.py')
sha = lambda data: hashlib.sha256(data).hexdigest()
BEFORE, AFTER = 'value = 1\n', 'value = 2\n'
HELPER_SOURCE = {'one.py': 'helper = 1\n', 'two.py': 'helper = 2\n'}


def pin(before=BEFORE, after=AFTER, old='value = 1', new='value = 2', count=1):
    return (sha(before.encode()), sha(after.encode()), ((old, new, count),))


@pytest.fixture
def tree(tmp_path, monkeypatch):
    root, sources = tmp_path / 'litellm', tmp_path / 'patches'
    (root / 'proxy').mkdir(parents=True)
    sources.mkdir()
    for name in ('first.py', 'last.py'):
        (root / name).write_text(BEFORE)
    for name, text in HELPER_SOURCE.items():
        (sources / name).write_text(text)
    monkeypatch.setattr(module, '__file__', str(sources / 'apply_upstream_guard.py'))
    monkeypatch.setattr(module, 'PATCHES', {'first.py': pin(), 'last.py': pin()})
    monkeypatch.setattr(module, 'HELPERS', {
        'proxy/helper_one.py': ('one.py', sha(HELPER_SOURCE['one.py'].encode())),
        'helper_two.py': ('two.py', sha(HELPER_SOURCE['two.py'].encode())),
    })
    return root


def untouched(root):
    return (all((root / n).read_text() == BEFORE for n in ('first.py', 'last.py'))
            and not any((root / h).exists() for h in module.HELPERS))


@pytest.mark.parametrize('failure', ['input_hash', 'context', 'output_hash', 'syntax'])
def test_validate_everything_before_any_write(tree, monkeypatch, failure):
    bad = {
        'input_hash': ('0' * 64,) + pin()[1:],
        'context': pin(old='missing context'),
        'output_hash': (pin()[0], '0' * 64, pin()[2]),
        'syntax': pin(after='value = [\n', new='value = ['),
    }[failure]
    monkeypatch.setitem(module.PATCHES, 'last.py', bad)
    with pytest.raises((SystemExit, SyntaxError)):
        module.apply(tree)
    assert untouched(tree)


@pytest.mark.parametrize('helper', ['proxy/helper_one.py', 'helper_two.py'])
def test_tampered_or_symlinked_helper_is_refused_without_writes(tree, monkeypatch, helper):
    source_name = module.HELPERS[helper][0]
    monkeypatch.setitem(module.HELPERS, helper, (source_name, '0' * 64))
    with pytest.raises(SystemExit, match='helper source'):
        module.apply(tree)
    assert untouched(tree)
    monkeypatch.setitem(module.HELPERS, helper, (source_name, sha(HELPER_SOURCE[source_name].encode())))
    source = Path(module.__file__).with_name(source_name)
    (source.parent / 'elsewhere.py').write_text(HELPER_SOURCE[source_name])
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
    assert (tree / 'last.py').read_text() == BEFORE and not any((tree / h).exists() for h in module.HELPERS)


def test_success_then_reapplication_is_refused(tree):
    module.apply(tree)
    assert (tree / 'first.py').read_text() == AFTER and (tree / 'last.py').read_text() == AFTER
    helpers = {name: (tree / name).read_bytes() for name in module.HELPERS}
    assert {name: sha(data) for name, data in helpers.items()} == {n: h for n, (_, h) in module.HELPERS.items()}
    with pytest.raises(SystemExit, match='Refusing changed upstream source'):
        module.apply(tree)
    assert {name: (tree / name).read_bytes() for name in module.HELPERS} == helpers


@pytest.mark.parametrize('helper', ['proxy/helper_one.py', 'helper_two.py'])
def test_existing_helper_is_refused_even_with_pristine_targets(tree, helper):
    (tree / helper).write_text('planted = True\n')
    with pytest.raises(SystemExit, match='existing helper'):
        module.apply(tree)
    assert (tree / 'first.py').read_text() == BEFORE and (tree / helper).read_text() == 'planted = True\n'


def test_pinned_helpers_are_the_reviewed_files():
    assert set(module.HELPERS) == {'litellm_core_utils/woow_provider_errors.py', 'proxy/woow_request_guard.py',
                                   'proxy/auth/woow_auth_errors.py'}
    for name, (source, expected) in module.HELPERS.items():
        assert source == Path(name).name and sha((PATCHES / source).read_bytes()) == expected


def test_helpers_import_only_the_standard_library_at_module_level():
    for source, _ in module.HELPERS.values():
        top = [line for line in (PATCHES / source).read_text().splitlines() if line.startswith(('import ', 'from '))]
        assert top and not any('litellm' in line or 'fastapi' in line for line in top), source


def test_every_edit_has_exactly_one_occurrence_and_a_reviewed_hash():
    for name, (before, after, edits) in module.PATCHES.items():
        assert len(before) == len(after) == 64 and before != after, name
        assert edits and all(count == 1 and old != new for old, new, count in edits), name


def test_tampered_real_target_is_refused_without_writes(tmp_path):
    """Negative build check on the real PATCHES: one changed byte in any target and nothing is written."""
    root = tmp_path / 'litellm'
    for name in module.PATCHES:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text('# not the reviewed LiteLLM source\n')
    with pytest.raises(SystemExit, match='Refusing changed upstream source'):
        module.apply(root)
    assert all((root / n).read_text() == '# not the reviewed LiteLLM source\n' for n in module.PATCHES)
    assert not any((root / h).exists() for h in module.HELPERS)


def _paths_named_by(installer):
    """Every 'dir/file.py' string literal an earlier installer names (targets, helpers, dependencies)."""
    return set(re.findall(r"""['"]([a-z_]+(?:/[a-z_.]+)*\.py)['"]""", (PATCHES / installer).read_text()))


def test_chain_order_matches_both_images():
    errors = load('api_errors_for_guard', PATCHES / 'apply_api_errors.py')
    api = load('api_compat_for_guard', PATCHES / 'apply_api_compat.py')
    routes = load('routes_for_guard', PATCHES / 'apply_routes_auth.py')
    earlier = set().union(*(_paths_named_by(installer) for installer in EARLIER))
    assert 'router.py' in earlier and 'proxy/woow_api_errors.py' in earlier  # the scan sees them
    assert set(module.PATCHES) & earlier == {SHARED}
    assert module.PATCHES[SHARED][0] == errors.PATCHES[SHARED][1]
    assert {name: before for name, (before, _, _) in module.PATCHES.items() if name != SHARED} == PRISTINE
    assert not set(module.HELPERS) & (earlier | set(module.PATCHES))
    assert 'router.py' not in module.PATCHES and 'proxy/woow_api_errors.py' not in module.PATCHES
    harness = load('harness_for_guard', REPO / 'tests/api_contract_harness/run_image_contracts.py')
    final = harness.final_api_patches(api, routes, errors, module)
    assert all(final[name][1] == after for name, (_, after, _) in module.PATCHES.items())
    assert all(final[name][1] == helper_hash for name, (_, helper_hash) in module.HELPERS.items())
    assert final[SHARED][0] == api.PATCHES[SHARED][0]  # still verified from the pristine API-compat input
    with pytest.raises(RuntimeError, match='Upstream guard patch'):
        broken = type('Broken', (), {'PATCHES': {SHARED: ('0' * 64, '1' * 64, ())}, 'HELPERS': {}})
        harness.final_api_patches(api, routes, errors, broken)


def test_dockerfile_runs_it_last_and_ci_gate_ships_it():
    docker = (REPO / 'litellm/Dockerfile').read_text()
    runs = [line for line in docker.splitlines() if line.startswith('RUN python -I /usr/share/woow-litellm/patches/')]
    assert runs[-1] == 'RUN python -I /usr/share/woow-litellm/patches/apply_upstream_guard.py'
    assert runs[-2] == 'RUN python -I /usr/share/woow-litellm/patches/apply_api_errors.py'
    assert docker.index('COPY patches/ /usr/share/woow-litellm/patches/') < docker.index(runs[-1])
    assert 'litellm/patches/apply_upstream_guard.py' in (REPO / 'tools/ci-api-contracts.sh').read_text()
    assert "'apply_upstream_guard.py'" in (REPO / 'tests/api_contract_harness/run_image_contracts.py').read_text()


def test_installer_cli_refuses_extra_arguments(tmp_path):
    import subprocess
    import sys
    copy = tmp_path / 'apply_upstream_guard.py'
    shutil.copy(PATCHES / 'apply_upstream_guard.py', copy)
    result = subprocess.run([sys.executable, '-I', str(copy), 'a', 'b'], capture_output=True, text=True)
    assert result.returncode != 0 and 'Usage: apply_upstream_guard.py' in result.stderr


def test_d1_scrub_runs_after_the_server_log_and_before_callbacks():
    """The auth error is logged with the key hash first; only then are callbacks, the spend log and the
    client handed the scrubbed exception (the order the edits create in auth_exception_handler.py)."""
    edits = module.PATCHES['proxy/auth/auth_exception_handler.py'][2]
    logged = [new for old, new, _ in edits if 'extra=log_extra' in old]
    assert len(logged) == 1
    assert logged[0].index('extra=log_extra') < logged[0].index('scrub_auth_exception(e)')
    final = [new for old, new, _ in edits if 'final_exception' in old]
    assert len(final) == 1 and 'scrub_auth_exception(_as_proxy_exception(e))' in final[0]
