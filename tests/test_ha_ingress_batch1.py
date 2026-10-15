"""HA add-on side of M4 batch 1: Ingress buffers and readiness (R10), Ingress name guard (R01).

The real nginx behaviour (large headers relayed, JSON 502 after readiness, 409 for '%' writes
while the marker exists) is exercised by tests/nginx/check_ingress_headers.py in the image smoke
test; these lean tests pin the pieces that must agree with each other.
"""
import os
from pathlib import Path
import re
import subprocess
import types
import pytest

REPO = Path(__file__).resolve().parents[1]
ROOTFS = REPO / 'litellm/rootfs'
LIB = ROOTFS / 'usr/local/lib/woow-litellm'
TEMPLATE = (ROOTFS / 'usr/share/woow-litellm/nginx/ingress.conf.tmpl').read_text()
READY = '/run/woow/litellm.ready'
MARKER = '/run/woow/ingress-percent-guard'


@pytest.fixture
def service(tmp_path, monkeypatch):
    module = types.ModuleType('woow_service')
    exec(compile((LIB / 'woow_service.py').read_text(), str(LIB / 'woow_service.py'), 'exec'), module.__dict__)
    monkeypatch.setattr(module, 'READY_FLAG', str(tmp_path / 'litellm.ready'))
    return module


# R10 ------------------------------------------------------------------------------------------
def test_ingress_buffers_fit_large_upstream_headers():
    location = TEMPLATE[TEMPLATE.index('    location / {'):TEMPLATE.index('    location @upstream_error')]
    for line in ('proxy_buffer_size 16k;', 'proxy_buffers 8 16k;', 'proxy_busy_buffers_size 32k;', 'proxy_buffering off;'):
        assert line in location


def test_starting_page_only_before_readiness():
    assert 'error_page 502 = $woow_upstream_502;' in TEMPLATE and 'error_page 502 = @starting;' not in TEMPLATE
    assert re.search(r'set \$woow_upstream_502 @starting;\n\s*if \(-f ' + re.escape(READY) + r'\) \{\n'
                     r'\s*set \$woow_upstream_502 @upstream_error;', TEMPLATE)
    error = TEMPLATE[TEMPLATE.index('location @upstream_error'):TEMPLATE.index('location = /.woow/encoded-write')]
    assert 'default_type application/json;' in error and 'return 502 ' in error and 'ingress_proxy_error' in error
    assert "\\'" not in TEMPLATE  # nginx strings stay unescaped
    assert 'LiteLLM 啟動中' in TEMPLATE[TEMPLATE.index('location @starting'):]


def test_readiness_flag_is_one_path_written_by_the_service_and_removed_on_exit(service):
    assert service.READY_FLAG.endswith('litellm.ready')
    source = (LIB / 'woow_service.py').read_text()
    assert f"READY_FLAG = '{READY}'" in source
    finish = (ROOTFS / 'etc/s6-overlay/s6-rc.d/litellm/finish').read_text()
    assert f'rm -f {READY}\n' in finish and finish.rstrip().endswith('svc-finish litellm "$@"')


def test_flag_cleared_before_startup_and_written_after_liveliness(service, monkeypatch):
    flag = Path(service.READY_FLAG)
    flag.write_text('')
    trace = []
    monkeypatch.setattr(service, 'run_step', lambda cmd, seconds: trace.append((os.path.basename(cmd[0]), flag.exists())) or 1)
    assert service.start_litellm() == 1
    assert trace == [('litellm-predump', False)]


def test_indexes_marks_ready_scans_names_then_keeps_the_flag_in_step(service, monkeypatch):
    flag = Path(service.READY_FLAG)
    probes = iter([1, 0, 0])  # down, then up at startup; up again after a LiteLLM restart
    trace, sleeps = [], []

    class Done(Exception):
        pass

    def run_step(cmd, seconds):
        curl = cmd[0].endswith('curl')
        trace.append(('liveliness' if curl else os.path.basename(cmd[0]), flag.exists()))
        return next(probes) if curl else 0

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            flag.unlink()  # LiteLLM restarted: its finish script removed the flag
        if len(sleeps) == 3:
            raise Done()

    monkeypatch.setattr(service, 'run_step', run_step)
    monkeypatch.setattr(service.time, 'sleep', sleep)
    with pytest.raises(Done):
        service.indexes()
    # No probe while the flag is there; one probe re-marks it after the restart.
    assert trace == [('liveliness', False), ('liveliness', False), ('woow-name-scan', True),
                     ('woow-spendlogs-indexes', True), ('liveliness', False)]
    assert flag.exists() and sleeps == [2, service.READY_POLL, service.READY_POLL]


def test_flag_helpers_never_raise(service, monkeypatch):
    monkeypatch.setattr(service, 'READY_FLAG', '/nonexistent-dir/for/sure/litellm.ready')
    service.mark_ready()
    service.clear_ready()


# R01 ------------------------------------------------------------------------------------------
def test_percent_write_guard_is_conditional_on_the_scan_marker():
    assert '"~^(?:DELETE|PATCH|PUT) [^?]*%"  1;' in TEMPLATE
    assert re.search(r'set \$woow_encoded_write 0;\n\s*if \(-f ' + re.escape(MARKER) + r'\) \{\n'
                     r'\s*set \$woow_encoded_write \$woow_write_with_percent;', TEMPLATE)
    refused = TEMPLATE[TEMPLATE.index('location = /.woow/encoded-write'):]
    assert 'internal;' in refused and 'return 409 ' in refused and '4000' in refused
    assert f'MARKER={MARKER}\n' in (LIB / 'woow-name-scan').read_text()


SCAN_STUB = '''log() { printf '[woow-litellm] %s: %s\\n' "${WOOW_STEP:-woow}" "$*" >&2; }
psql_su() {
  [ -n "${FAKE_FAIL:-}" ] && return 1
  case "$*" in
    *"count(*)"*) printf '%s\\n' "$FAKE_PERCENT" ;;
    *LiteLLM_CredentialsTable*) [ -z "$FAKE_CREDS" ] || printf '%s\\n' "$FAKE_CREDS" ;;
    *LiteLLM_ProxyModelTable*) [ -z "$FAKE_MODELS" ] || printf '%s\\n' "$FAKE_MODELS" ;;
  esac
  return 0
}
'''


def run_scan(tmp_path, creds='', models='', percent='0', fail=False):
    stub = tmp_path / 'common.sh'
    stub.write_text(SCAN_STUB)
    script = (LIB / 'woow-name-scan').read_text()
    marker = tmp_path / 'ingress-percent-guard'
    script = script.replace('. /usr/local/lib/woow-litellm/common.sh', f'. {stub}').replace(MARKER, str(marker))
    path = tmp_path / 'woow-name-scan'
    path.write_text(script)
    env = {'PATH': os.environ['PATH'], 'FAKE_CREDS': creds, 'FAKE_MODELS': models, 'FAKE_PERCENT': percent}
    if fail:
        env['FAKE_FAIL'] = '1'
    result = subprocess.run(['sh', str(path)], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stderr, marker


def test_scan_arms_the_guard_while_a_percent_name_exists(tmp_path):
    log, marker = run_scan(tmp_path, creds='x%2Fy\nteam/a', models='dep%1  (model_name: m)', percent='2')
    assert marker.exists() and oct(marker.stat().st_mode & 0o777) == '0o644'
    assert 'x%2Fy' in log and 'team/a' in log and 'dep%1' in log and '(409)' in log


def test_scan_warns_about_slash_names_without_arming_the_guard(tmp_path):
    (tmp_path / 'ingress-percent-guard').write_text('')
    log, marker = run_scan(tmp_path, creds='team/a', percent='0')
    assert not marker.exists() and 'team/a' in log and 'WARNING' in log


def test_scan_clean_database(tmp_path):
    log, marker = run_scan(tmp_path)
    assert not marker.exists() and 'all can be managed through the sidebar' in log


def test_scan_never_fails_the_start(tmp_path):
    (tmp_path / 'ingress-percent-guard').write_text('')
    log, marker = run_scan(tmp_path, fail=True)
    assert marker.exists() and 'could not read' in log  # unchanged when the database cannot be read


def test_scan_runs_in_ci_shellcheck_and_docs_explain_the_rule():
    assert 'litellm/rootfs/usr/local/lib/woow-litellm/woow-name-scan' in (REPO / '.github/workflows/ci.yml').read_text()
    assert (LIB / 'woow-name-scan').stat().st_mode & 0o111
    docs = (REPO / 'litellm/DOCS.md').read_text()
    assert '名稱不可含「%」或「/」' in docs and '409' in docs and 'woow-name-scan' in docs
    assert 'ingress_proxy_error' in docs
