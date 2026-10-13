"""Refuse drift before any upstream source file is modified."""
import hashlib
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'litellm/patches/apply_responses_fix.py'
spec = importlib.util.spec_from_file_location('protocol_patch', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_patch_checks_every_source_before_writing(tmp_path, monkeypatch):
    first = 'before = 1\n'
    (tmp_path / 'first.py').write_text(first)
    (tmp_path / 'second.py').write_text('unexpected source\n')
    monkeypatch.setattr(module, 'PATCHES', {
        'first.py': (hashlib.sha256(first.encode()).hexdigest(), first, 'after = 2\n', 1),
        'second.py': ('0' * 64, 'before', 'after', 1),
    })
    with pytest.raises(SystemExit, match='Refusing changed upstream source: second.py'):
        module.apply(tmp_path)
    assert (tmp_path / 'first.py').read_text() == first
    assert (tmp_path / 'second.py').read_text() == 'unexpected source\n'


def test_patch_refuses_ambiguous_context(tmp_path, monkeypatch):
    original = 'before = 1\nbefore = 1\n'
    (tmp_path / 'first.py').write_text(original)
    monkeypatch.setattr(module, 'PATCHES', {
        'first.py': (hashlib.sha256(original.encode()).hexdigest(), 'before = 1', 'after = 2', 1),
    })
    with pytest.raises(SystemExit, match='Refusing changed patch context'):
        module.apply(tmp_path)
    assert (tmp_path / 'first.py').read_text() == original


def test_patch_refuses_existing_helper_before_writes(tmp_path, monkeypatch):
    original = 'before = 1\n'
    (tmp_path / 'first.py').write_text(original)
    (tmp_path / 'responses').mkdir()
    helper = tmp_path / 'responses/woow_output_recovery.py'
    helper.write_text('existing = True\n')
    monkeypatch.setattr(module, 'PATCHES', {
        'first.py': (hashlib.sha256(original.encode()).hexdigest(), original, 'after = 2\n', 1),
    })
    monkeypatch.setattr(module, 'SOURCE_DEPENDENCIES', {})
    with pytest.raises(SystemExit, match='Refusing existing recovery helper'):
        module.apply(tmp_path)
    assert (tmp_path / 'first.py').read_text() == original
    assert helper.read_text() == 'existing = True\n'


def test_patch_refuses_changed_helper_dependency_before_writes(tmp_path, monkeypatch):
    original = 'before = 1\n'
    (tmp_path / 'first.py').write_text(original)
    (tmp_path / 'dependency.py').write_text('changed = True\n')
    monkeypatch.setattr(module, 'PATCHES', {
        'first.py': (hashlib.sha256(original.encode()).hexdigest(), original, 'after = 2\n', 1),
    })
    monkeypatch.setattr(module, 'SOURCE_DEPENDENCIES', {'dependency.py': '0' * 64})
    with pytest.raises(SystemExit, match='Refusing changed recovery dependency'):
        module.apply(tmp_path)
    assert (tmp_path / 'first.py').read_text() == original
