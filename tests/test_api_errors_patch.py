"""Build-guard regressions for apply_api_errors.py; runtime behaviour lives in api_compat."""
import hashlib
import importlib.util
from pathlib import Path
import pytest

REPO = Path(__file__).resolve().parents[1]
PATCHES = REPO / 'litellm/patches'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


module = load('api_errors_patch', PATCHES / 'apply_api_errors.py')
sha = lambda data: hashlib.sha256(data).hexdigest()


def pin(before, after, old='value = 1', new='value = 2', count=1):
    return (sha(before.encode()), sha(after.encode()), ((old, new, count),))


@pytest.fixture
def tree(tmp_path, monkeypatch):
    before, after = 'value = 1\n', 'value = 2\n'
    (tmp_path / 'proxy').mkdir()
    for name in ('first.py', 'last.py'):
        (tmp_path / name).write_text(before)
    monkeypatch.setattr(module, 'PATCHES', {'first.py': pin(before, after), 'last.py': pin(before, after)})
    return tmp_path


def untouched(root):
    return all((root / n).read_text() == 'value = 1\n' for n in ('first.py', 'last.py')) and not (root / module.HELPER).exists()


@pytest.mark.parametrize('failure', ['input_hash', 'context', 'output_hash', 'syntax'])
def test_validate_everything_before_any_write(tree, monkeypatch, failure):
    before, after = 'value = 1\n', 'value = 2\n'
    bad = {
        'input_hash': ('0' * 64,) + pin(before, after)[1:],
        'context': pin(before, after, old='missing context'),
        'output_hash': (pin(before, after)[0], '0' * 64, pin(before, after)[2]),
        'syntax': pin(before, 'value = [\n', new='value = ['),
    }[failure]
    monkeypatch.setitem(module.PATCHES, 'last.py', bad)
    with pytest.raises((SystemExit, SyntaxError)):
        module.apply(tree)
    assert untouched(tree)


def test_helper_hash_and_symlink_are_refused_without_writes(tree, monkeypatch):
    monkeypatch.setattr(module, 'HELPER_SHA256', '0' * 64)
    with pytest.raises(SystemExit, match='helper source'):
        module.apply(tree)
    assert untouched(tree)
    monkeypatch.undo()
    (tree / 'elsewhere.py').write_text('value = 1\n')
    (tree / 'first.py').unlink()
    (tree / 'first.py').symlink_to(tree / 'elsewhere.py')
    with pytest.raises(SystemExit, match='symlink'):
        module.apply(tree)


def test_success_then_reapplication_is_refused(tree):
    module.apply(tree)
    assert (tree / 'first.py').read_text() == 'value = 2\n'
    helper = (tree / module.HELPER).read_bytes()
    assert sha(helper) == module.HELPER_SHA256
    with pytest.raises(SystemExit, match='Refusing changed upstream source'):
        module.apply(tree)
    assert (tree / module.HELPER).read_bytes() == helper


def test_existing_helper_is_refused_even_with_pristine_targets(tree):
    (tree / module.HELPER).write_text('planted = True\n')
    with pytest.raises(SystemExit, match='existing helper'):
        module.apply(tree)
    assert (tree / 'first.py').read_text() == 'value = 1\n'
    assert (tree / module.HELPER).read_text() == 'planted = True\n'


def test_pinned_helper_is_the_reviewed_file():
    assert sha((PATCHES / 'woow_api_errors.py').read_bytes()) == module.HELPER_SHA256


def test_every_edit_has_exactly_one_occurrence_and_a_reviewed_hash():
    for name, (before, after, edits) in module.PATCHES.items():
        assert len(before) == len(after) == 64 and before != after, name
        assert edits and all(count == 1 and old != new for old, new, count in edits), name


def test_chain_order_matches_both_images():
    api = load('api_compat_for_errors', PATCHES / 'apply_api_compat.py')
    subscription = (PATCHES / 'apply_subscription_policy.py').read_text()
    for name, (before, _, _) in module.PATCHES.items():
        # The HA-only subscription policy edits router.py; both chains must hand over the same input.
        assert name != 'router.py' and "root/'" + name + "'" not in subscription
        if name in api.PATCHES:
            assert before == api.PATCHES[name][1], name
    harness = load('harness_for_errors', REPO / 'tests/api_contract_harness/run_image_contracts.py')
    routes = load('routes_for_errors', PATCHES / 'apply_routes_auth.py')
    final = harness.final_api_patches(api, routes, module)
    assert final[module.HELPER][1] == module.HELPER_SHA256
    assert all(final[name][1] == after for name, (_, after, _) in module.PATCHES.items())


def test_dockerfile_runs_it_last_and_ci_gate_ships_it():
    docker = (REPO / 'litellm/Dockerfile').read_text()
    runs = [line for line in docker.splitlines() if line.startswith('RUN python -I /usr/share/woow-litellm/patches/')]
    assert runs[-1] == 'RUN python -I /usr/share/woow-litellm/patches/apply_api_errors.py'
    assert runs[-2] == 'RUN python -I /usr/share/woow-litellm/patches/apply_login_throttle.py'
    assert docker.index('COPY patches/ /usr/share/woow-litellm/patches/') < docker.index(runs[-1])
    assert 'litellm/patches/apply_api_errors.py' in (REPO / 'tools/ci-api-contracts.sh').read_text()
