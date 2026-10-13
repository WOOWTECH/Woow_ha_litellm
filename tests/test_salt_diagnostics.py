"""Actual refusal branches must point to a consistent data+secrets backup."""
import os
import subprocess

import pytest
from conftest import ROOTFS


@pytest.mark.parametrize('command', ['secrets', 'meta-sql', 'platform-sql'])
@pytest.mark.parametrize('salt_state', ['missing', 'malformed'])
def test_salt_file_refusal_names_both_backup_paths(box, command, salt_state):
    for step in ('perms', 'env', 'secrets'):
        box.ok(step)
    (box.data/'postgres/PG_VERSION').write_text('18\n')
    salt = box.data/'secrets/salt_key'
    malformed = 'synthetic invalid salt value'
    if salt_state == 'missing':
        salt.unlink()
    else:
        salt.write_text(malformed)
    result = box.run(command)
    assert result.returncode == 1
    assert '/data/postgres' in result.stderr
    assert '/data/secrets' in result.stderr
    assert 'backup' in result.stderr.lower()
    assert malformed not in result.stderr
    # Error reporting must neither replace the salt nor emit SQL containing it.
    assert result.stdout == ''
    if salt_state == 'missing':
        assert not salt.exists()
    else:
        assert salt.read_text() == malformed


@pytest.mark.parametrize('meta,platform', [
    ('different', 'same'), ('unexpected', 'same'),
    ('same', 'different'), ('same', 'empty'), ('same', 'unexpected'),
    ('same', 'same'),
])
def test_database_salt_refusal_names_both_backup_paths(tmp_path, meta, platform):
    # Execute the product shell branch; substitute only PostgreSQL/woow-init
    # boundaries and the common.sh absolute path. No database or HA effects.
    common = tmp_path/'common.sh'
    common.write_text('''log() { echo "$*" >&2; }
fatal() { echo "$*" >&2; exit 1; }
woow_init() { echo "$1"; }
psql_su() {
  input="$(cat)"
  case "$input" in
    meta-sql) echo "$META_RESULT" ;;
    platform-sql) echo "$PLATFORM_RESULT" ;;
  esac
}
''')
    source = (ROOTFS/'usr/local/lib/woow-litellm/init-db').read_text()
    script = tmp_path/'init-db'
    script.write_text(source.replace('/usr/local/lib/woow-litellm/common.sh', str(common)))
    result = subprocess.run(['bash', str(script)], capture_output=True, text=True,
                            env={**os.environ, 'META_RESULT': meta, 'PLATFORM_RESULT': platform}, timeout=10)
    if meta == platform == 'same':
        assert result.returncode == 0
        assert 'database ready' in result.stderr
    else:
        assert result.returncode == 1
        assert '/data/postgres' in result.stderr
        assert '/data/secrets' in result.stderr
        assert 'backup' in result.stderr.lower()
        assert 'database ready' not in result.stderr
