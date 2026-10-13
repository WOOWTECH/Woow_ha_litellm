"""Exercise the smoke marker probe with the upstream venv's shadowed test command."""
import os
import shlex
import subprocess

import pytest

from conftest import REPO


@pytest.mark.parametrize('marker_present', [False, True])
def test_startup_marker_probe_ignores_external_test(tmp_path, marker_present):
    # Upstream's /app/.venv/bin/test is a Python entrypoint with missing imports.
    # Docker exec searches PATH directly; only an explicit shell gets its builtin.
    shadow = tmp_path / 'test'
    shadow.write_text('#!/bin/sh\necho "external test entrypoint invoked" >&2\nexit 97\n')
    shadow.chmod(0o755)
    marker = tmp_path / 'woow-m4-step'
    if marker_present:
        marker.touch()
    source = (REPO / 'tests/smoke/run-smoke.sh').read_text()
    probe = next(line.strip() for line in source.splitlines()
                 if line.strip().startswith('if in_addon ') and '/tmp/woow-m4-step' in line)
    # Execute the exact docker-exec argv, substituting only the marker's pathname.
    command = shlex.split(probe.removeprefix('if in_addon ').split('; then', 1)[0])
    command = [arg.replace('/tmp/woow-m4-step', str(marker)) for arg in command]
    result = subprocess.run(command, capture_output=True, text=True, timeout=5,
                            env={**os.environ, 'PATH': f'{tmp_path}:{os.environ["PATH"]}'})
    assert result.returncode == (0 if marker_present else 1), result.stderr
    assert 'external test entrypoint invoked' not in result.stderr
