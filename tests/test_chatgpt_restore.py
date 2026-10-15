"""Restore reports DB persistence failures without sacrificing valid local sign-ins."""
import importlib.util
import json
from pathlib import Path

import pytest

from conftest import REPO, SHARE


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def core(tmp_path, monkeypatch):
    # Materialize exactly the build-time patch on a temporary copy, then execute
    # the actual standalone module. No LiteLLM import or real token directory.
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "woow_chatgpt.py").write_bytes((SHARE / "py/woow_chatgpt.py").read_bytes())
    # 0.1.11: the canonical plugin (charts 0.3.5, R14) carries the restore error contract itself;
    # the addon-local apply_restore_fix.py patch is gone.
    module = load_module("restore_under_test", runtime / "woow_chatgpt.py")
    monkeypatch.setattr(module, "TOKEN_DIR", str(tmp_path / "tokens"))
    monkeypatch.setattr(module, "AUTH_FILE", str(tmp_path / "tokens/auth.json"))
    monkeypatch.setattr(module, "ACCOUNTS_DIR", str(tmp_path / "tokens/accounts"))
    monkeypatch.setattr(module, "db_accounts", lambda: {})
    monkeypatch.setattr(module, "db_available", lambda: False)
    return module


def seed(core, account="a1", access="synthetic-access-new", refresh="synthetic-refresh-new"):
    record = {"access_token": access, "refresh_token": refresh}
    core._write_json(core.auth_path(account), record)
    return Path(core.auth_path(account)).read_bytes()


def test_restore_db_write_failure_is_not_success_and_preserves_local_tokens(core, monkeypatch):
    original = seed(core)

    def unavailable(account, record):
        raise OSError("synthetic database unavailable")

    monkeypatch.setattr(core, "db_save", unavailable)
    result = core.restore()

    assert result["ok"] is False
    assert result["state"] == "error"
    assert result["error"] == "db_save_failed"
    assert result["kept"] == ["a1"]
    assert Path(core.auth_path("a1")).read_bytes() == original


def test_restore_write_failure_preserves_newest_file_and_restores_other_accounts(core, monkeypatch):
    original_a1 = seed(core, "a1")
    original_a2 = seed(core, "a2", "synthetic-second-access", "synthetic-second-refresh")
    database = {
        "a1": {"access_token": "synthetic-old-access", "refresh_token": "synthetic-old-refresh"},
        "a3": {"access_token": "synthetic-third-access", "refresh_token": "synthetic-third-refresh"},
    }

    def save(account, record):
        if account == "a1":
            raise OSError("synthetic write failure")
        database[account] = dict(record)

    monkeypatch.setattr(core, "db_save", save)
    monkeypatch.setattr(core, "db_accounts", lambda: database.copy())
    result = core.restore()

    assert result == {"ok": False, "state": "error", "error": "db_save_failed",
                      "connected": True, "kept": ["a1", "a2"], "restored": ["a3"],
                      "failed": [{"account": "a1", "error": "db_save_failed"}]}
    assert Path(core.auth_path("a1")).read_bytes() == original_a1
    assert Path(core.auth_path("a2")).read_bytes() == original_a2
    assert core.account_record("a2") == database["a2"]
    assert core.account_record("a3") == database["a3"]


def test_restore_cli_failure_is_nonzero_and_does_not_leak_db_error_or_tokens(core, monkeypatch, capsys):
    import os

    original = seed(core, access="SYNTHETIC_ACCESS_CANARY", refresh="SYNTHETIC_REFRESH_CANARY")

    def unavailable(account, record):
        raise RuntimeError("SYNTHETIC_PRIVATE_DB_DETAIL SYNTHETIC_ACCESS_CANARY SYNTHETIC_REFRESH_CANARY")

    monkeypatch.setattr(core, "db_save", unavailable)
    old_umask = os.umask(0o077)
    try:
        exit_code = core.main(["restore"])
    finally:
        os.umask(old_umask)
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 1
    assert result["ok"] is False
    assert result["state"] == "error"
    assert result["error"] == "db_save_failed"
    assert "db_save_failed" in captured.err
    for canary in ("SYNTHETIC_PRIVATE_DB_DETAIL", "SYNTHETIC_ACCESS_CANARY", "SYNTHETIC_REFRESH_CANARY"):
        assert canary not in captured.out + captured.err
    assert "Traceback" not in captured.out + captured.err
    assert Path(core.auth_path("a1")).read_bytes() == original


@pytest.mark.parametrize("state", ["none", "kept", "restored"])
def test_restore_success_contract_is_unchanged(core, monkeypatch, state):
    if state == "kept":
        seed(core)
    elif state == "restored":
        monkeypatch.setattr(core, "db_accounts", lambda: {
            "a1": {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh"},
        })

    assert core.restore() == {
        "ok": True, "state": state, "connected": state != "none",
        "kept": ["a1"] if state == "kept" else [],
        "restored": ["a1"] if state == "restored" else [],
    }


def test_restore_cli_success_still_exits_zero(core, capsys):
    import os

    seed(core)
    old_umask = os.umask(0o077)
    try:
        assert core.main(["restore"]) == 0
    finally:
        os.umask(old_umask)
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["state"] == "kept"


def test_restore_db_read_failure_contract_and_local_tokens_unchanged(core, monkeypatch):
    original = seed(core)

    def unavailable():
        raise OSError("SYNTHETIC_PRIVATE_DB_DETAIL")

    monkeypatch.setattr(core, "db_accounts", unavailable)
    # Canonical 0.3.2 (charts 427e063) reads the database before touching any account, so a read
    # failure returns before anything is classified as kept; the local file stays untouched.
    assert core.restore() == {
        "ok": False, "state": "error", "error": "db_load_failed", "detail": "OSError", "kept": [],
    }
    assert Path(core.auth_path("a1")).read_bytes() == original


def test_restore_simultaneous_write_and_read_failures_remain_errors(core, monkeypatch, capsys):
    original = seed(core)

    def unavailable(*args):
        raise OSError("SYNTHETIC_PRIVATE_DB_DETAIL")

    monkeypatch.setattr(core, "db_save", unavailable)
    monkeypatch.setattr(core, "db_accounts", unavailable)
    result = core.restore()
    assert result["ok"] is False
    assert result["state"] == "error"
    assert result["error"] == "db_load_failed"
    assert Path(core.auth_path("a1")).read_bytes() == original
    assert "SYNTHETIC_PRIVATE_DB_DETAIL" not in json.dumps(result) + capsys.readouterr().err
