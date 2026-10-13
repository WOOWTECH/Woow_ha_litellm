"""HA boot policy ownership and patch drift checks (no LiteLLM dependency)."""
import hashlib
import importlib.util
from pathlib import Path
import stat

import pytest
from conftest import DEFAULT_OPTIONS, REPO

SCRIPT = REPO/'litellm/patches/apply_subscription_policy.py'
spec = importlib.util.spec_from_file_location('subscription_patch', SCRIPT)
patcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patcher)


def test_boot_policy_toggle_keeps_disabled_env_clean(box):
    box.ok('perms')
    policy = box.run_dir/'woow/chatgpt-subscription'
    for enabled in (True, False, True):
        box.options(DEFAULT_OPTIONS, chatgpt_subscription=enabled)
        box.ok('env')
        assert policy.read_text() == ('enabled\n' if enabled else 'disabled\n')
        assert stat.S_IMODE(policy.stat().st_mode) == 0o644
        assert not stat.S_IMODE(policy.parent.stat().st_mode) & 0o022
        env = box.envdir('litellm')
        if not enabled:
            assert 'PYTHONPATH' not in env
            assert 'CHATGPT_TOKEN_DIR' not in env
            assert not any(key.startswith('WOOW_') for key in env)
        assert 'PYTHONPATH' not in box.envdir('litellm-migrate')


def test_patch_refuses_drift_before_writing(tmp_path):
    path = tmp_path/'router.py'
    path.write_text('changed upstream\n')
    with pytest.raises(SystemExit, match='changed upstream'):
        patcher.apply(tmp_path)
    assert path.read_text() == 'changed upstream\n'
    assert not (tmp_path/'woow_subscription_policy.py').exists()


@pytest.mark.parametrize('ambiguous', [False, True])
def test_patch_refuses_context_or_existing_helper_before_writing(tmp_path, monkeypatch, ambiguous):
    path = tmp_path/'router.py'
    source = 'before = 1\n' * (2 if ambiguous else 1)
    path.write_text(source)
    monkeypatch.setattr(patcher, 'ROUTER_SHA256', hashlib.sha256(source.encode()).hexdigest())
    monkeypatch.setattr(patcher, 'EDITS', [('before = 1\n', 'after = 1\n')])
    if not ambiguous:
        (tmp_path/'woow_subscription_policy.py').write_text('existing = True\n')
    with pytest.raises(SystemExit, match='context' if ambiguous else 'existing'):
        patcher.apply(tmp_path)
    assert path.read_text() == source
