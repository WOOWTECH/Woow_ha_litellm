"""Executable regressions for verified M4 HA failures; no HA runtime access."""
import os
import signal
import subprocess
import time

import pytest

from conftest import DEFAULT_OPTIONS, ROOTFS

LIB = ROOTFS / 'usr/local/lib/woow-litellm'


@pytest.mark.parametrize('key', ['REDIS_URL', 'ALERT_WEBHOOK_URL', 'SENTRY_DSN', 'PROVIDER_AUTH', 'CUSTOM_VALUE'])
def test_env_values_never_in_startup_log(box, key):
    value = 'https://user:fixture-password@example.invalid/fixture-token'
    box.options(DEFAULT_OPTIONS, env_vars=[{'key': key, 'value': value}])
    res = box.ok('env')
    assert value not in res.stderr
    assert key in res.stderr
    assert box.envdir('litellm')[key] == value


@pytest.mark.parametrize('key', ['CONFIG_FILE_PATH', 'WORKER_CONFIG', 'SSL_KEYFILE_PATH', 'SSL_CERTFILE_PATH',
    'LITELLM_DISABLE_LOGIN_RATE_LIMIT', 'MAX_REQUESTS_BEFORE_RESTART', 'MAX_REQUESTS_BEFORE_RESTART_JITTER',
    'PROMETHEUS_METRICS_PORT', 'LITELLM_CONFIG_BUCKET_NAME', 'GRANIAN_WORKERS'])
def test_runtime_contract_overrides_refused(box, key):
    box.options(DEFAULT_OPTIONS, env_vars=[{'key': key, 'value': 'fixture'}])
    res = box.run('env')
    assert res.returncode == 1
    assert key in res.stderr and 'cannot be overridden' in res.stderr


@pytest.mark.parametrize('value', ['', 'ends in space ', 'ends in tab\t', 'x' * 4096, '界' * 1366])
def test_lossy_envdir_values_refused(box, value):
    box.options(DEFAULT_OPTIONS, env_vars=[{'key': 'CUSTOM_VALUE', 'value': value}])
    res = box.run('env')
    assert res.returncode == 1
    assert 'CUSTOM_VALUE' in res.stderr


@pytest.mark.parametrize('value', [' secret-password', 'secret-password ', 'secret-password\t'])
def test_lossy_password_refused(box, value):
    box.options(DEFAULT_OPTIONS, ui_password=value)
    res = box.run('env')
    assert res.returncode == 1
    assert value not in res.stderr


def test_migrator_has_only_database_secret(box):
    box.boot()
    env = box.envdir('litellm-migrate')
    assert 'DATABASE_URL' in env
    assert not {'UI_PASSWORD', 'LITELLM_MASTER_KEY', 'LITELLM_SALT_KEY'} & env.keys()


@pytest.fixture
def predump(tmp_path):
    """Run the production shell, replacing only filesystem paths and external commands."""
    dumps = tmp_path / 'dumps'
    dumps.mkdir()
    common = tmp_path / 'common.sh'
    common.write_text('''
log() { echo "$*" >&2; }
fatal() { log "FATAL $*"; exit 1; }
woow_init() { echo old; }
jq() { echo new; }
PGSOCK=unused
psql_su() { printf '%s' "$QUERY_RESULT"; return "$QUERY_RC"; }
as_postgres() {
    for last; do :; done
    printf 'dump fixture' > "$last"
    if [ "$DUMP_MODE" = fail ]; then return 1; fi
    if [ "$DUMP_MODE" = term ]; then kill -TERM "$$"; fi
}
''')
    script = tmp_path / 'predump'
    script.write_text((LIB / 'litellm-predump').read_text()
        .replace('/usr/local/lib/woow-litellm/common.sh', str(common))
        .replace('DIR=/data/pre-upgrade-dumps', f'DIR={dumps}'))

    def run(result='t', rc='0', mode='ok'):
        return subprocess.run(['sh', str(script)], capture_output=True, text=True, timeout=5,
            env={**os.environ, 'QUERY_RESULT': result, 'QUERY_RC': rc, 'DUMP_MODE': mode})
    return dumps, run


@pytest.mark.parametrize('result,rc', [('', '1'), ('f', '1'), ('', '0'), ('unexpected', '0')])
def test_predump_query_failure_never_permits_migration(predump, result, rc):
    dumps, run = predump
    res = run(result, rc)
    assert res.returncode != 0
    assert 'fresh database' not in res.stderr
    assert not list(dumps.iterdir())


def test_predump_fresh_database_is_explicit_false(predump):
    dumps, run = predump
    assert run('f').returncode == 0
    assert not list(dumps.iterdir())


@pytest.mark.parametrize('mode', ['fail', 'term'])
def test_predump_failure_cleans_partial_file(predump, mode):
    dumps, run = predump
    res = run(mode=mode)
    assert res.returncode != 0
    assert not list(dumps.glob('*.tmp'))
    assert not list(dumps.glob('*.dump'))


def test_predump_removes_stale_temp_even_on_fresh_database(predump):
    dumps, run = predump
    (dumps / 'pre-old.dump.tmp').write_text('interrupted')
    (dumps / 'unrelated.tmp').write_text('keep')
    assert run('f').returncode == 0
    assert not (dumps / 'pre-old.dump.tmp').exists()
    assert (dumps / 'unrelated.tmp').exists()


def test_predump_retains_two_completed_backups(predump):
    dumps, run = predump
    for i in range(3):
        p = dumps / f'pre-{i}.dump'
        p.write_text('completed')
        os.utime(p, (i+1, i+1))
    assert run().returncode == 0
    assert len(list(dumps.glob('*.dump'))) == 2
    assert (dumps / 'pre-2.dump').exists()


@pytest.mark.parametrize('nginx_code,nginx_rc,api_rc,expected', [
    ('403', 0, 0, 0), ('500', 0, 0, 1), ('', 7, 0, 1), ('403', 0, 7, 1)])
def test_healthcheck_covers_both_listeners(tmp_path, nginx_code, nginx_rc, api_rc, expected):
    script = tmp_path / 'healthcheck'
    curl = tmp_path / 'curl'
    curl.write_text(f'#!/bin/sh\ncase "$*" in *8099*) echo {nginx_code}; exit {nginx_rc};; *) exit {api_rc};; esac\n')
    curl.chmod(0o755)
    script.write_text((ROOTFS / 'usr/local/bin/woow-healthcheck').read_text().replace('/usr/bin/curl', str(curl)))
    assert subprocess.run(['sh', str(script)]).returncode == expected


def test_long_startup_does_not_hold_s6_readiness_lock():
    s6 = ROOTFS / 'etc/s6-overlay/s6-rc.d'
    assert not (s6 / 'litellm/notification-fd').exists()
    for name in ('litellm-predump', 'litellm-migrate', 'woow-revoke-ui-sessions'):
        assert not (s6 / name).exists()
    assert (s6 / 'woow-spendlogs-indexes/type').read_text().strip() == 'longrun'


@pytest.fixture
def service():
    import types
    module = types.ModuleType('woow_service')
    exec(compile((LIB / 'woow_service.py').read_text(), str(LIB / 'woow_service.py'), 'exec'), module.__dict__)
    return module


def test_startup_failure_stops_before_migration_and_api(service, tmp_path, monkeypatch):
    trace = tmp_path / 'trace'
    for name, rc in [('litellm-predump', 1), ('litellm-migrate', 0), ('woow-revoke-ui-sessions', 0),
                     ('litellm-restore', 0), ('litellm-start', 0)]:
        script = tmp_path / name
        script.write_text(f'#!/bin/sh\necho {name} >> {trace}\nexit {rc}\n')
        script.chmod(0o755)
    monkeypatch.setattr(service, 'LIB', str(tmp_path))
    assert service.start_litellm() == 1
    assert trace.read_text().splitlines() == ['litellm-predump']


def test_startup_order_before_api_exec(service, tmp_path, monkeypatch):
    trace = []
    monkeypatch.setattr(service, 'run_step', lambda cmd, seconds: trace.append((cmd[0], seconds)) or 0)
    monkeypatch.setattr(service.os, 'execv', lambda path, args: trace.append((path, 'exec')))
    service.start_litellm()
    assert [(os.path.basename(cmd), seconds) for cmd, seconds in trace] == [
        ('litellm-predump', 600), ('litellm-migrate', 1500), ('woow-revoke-ui-sessions', 60),
        ('litellm-restore', 60), ('litellm-start', 'exec')]


@pytest.mark.parametrize('timeout', [False, True])
def test_startup_stop_or_timeout_reaches_grandchildren(tmp_path, timeout):
    import sys
    import textwrap
    ready, killed = tmp_path / 'ready', tmp_path / 'killed'
    child = tmp_path / 'child.py'
    child.write_text(textwrap.dedent(f'''
        import pathlib, signal, time
        def stop(*args):
            pathlib.Path({str(killed)!r}).touch()
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, stop)
        pathlib.Path({str(ready)!r}).touch()
        while True: time.sleep(.02)
    '''))
    runner = tmp_path / 'runner.py'
    runner.write_text(textwrap.dedent(f'''
        import pathlib, types, signal, subprocess
        m = types.ModuleType('service')
        exec(compile(pathlib.Path({str(LIB / 'woow_service.py')!r}).read_text(), 'service', 'exec'), m.__dict__)
        signal.signal(signal.SIGTERM, m.stop)
        try:
            m.run_step(['sh', '-c', {f'{sys.executable} {child} & wait'!r}], {1 if timeout else 30})
        except (m.Stopped, subprocess.TimeoutExpired):
            raise SystemExit(143)
    '''))
    proc = subprocess.Popen([sys.executable, str(runner)])
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            assert proc.poll() is None
            time.sleep(.02)
        assert ready.exists()
        if not timeout:
            proc.terminate()
        assert proc.wait(timeout=8) == 143
        assert killed.exists(), 'grandchild did not receive the stop signal'
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
