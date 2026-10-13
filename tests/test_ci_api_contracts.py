"""Lean tests for mandatory image gates and fail-closed harness boundaries."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import zipfile
import pytest
import yaml

REPO=Path(__file__).resolve().parents[1]
HARNESS=REPO/'tests/api_contract_harness'
spec=importlib.util.spec_from_file_location('api_image_harness',HARNESS/'run_image_contracts.py')
harness=importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)

@pytest.mark.parametrize('file,job,image',[
    ('ci.yml','build-smoke','woow-litellm:ci'),
    ('publish-litellm-addon-images.yml','publish','${IMAGE}:${VERSION}'),
])
def test_built_image_contract_is_required_before_publication(file,job,image):
    workflow=yaml.safe_load((REPO/'.github/workflows'/file).read_text())
    steps=workflow['jobs'][job]['steps']
    gates=[(i,s) for i,s in enumerate(steps) if 'tools/ci-api-contracts.sh' in s.get('run','')]
    assert len(gates)==1
    index,gate=gates[0]
    assert image in gate['run'] and 'if' not in gate and not gate.get('continue-on-error',False)
    assert '||' not in gate['run']
    builds=[i for i,s in enumerate(steps) if 'docker buildx build' in s.get('run','')]
    assert builds and max(builds)<index
    for i,step in enumerate(steps):
        if 'docker push ' in step.get('run',''):assert i>index
    assert any('check_responses.py' in s.get('run','') for s in steps)


def test_lean_collection_explicitly_excludes_runtime_suite():
    workflow=yaml.safe_load((REPO/'.github/workflows/ci.yml').read_text())
    step=next(s for s in workflow['jobs']['lint-unit']['steps'] if s.get('name')=='unit tests')
    assert '--ignore=tests/api_compat' in step['run']
    assert 'pytest -q tests ' in step['run']
    assert not any('importorskip' in f.read_text() for f in (REPO/'tests/api_compat').glob('*.py'))


def test_wheel_lock_matches_pip_hash_requirements():
    lock=json.loads((HARNESS/'wheels.lock.json').read_text())
    assert {r['name'] for r in lock}=={'pytest','pluggy','packaging','iniconfig'}
    lines=[s for s in (HARNESS/'requirements.lock').read_text().splitlines() if s and not s.startswith('#')]
    assert set(lines)=={f"{r['name']}=={r['version']} --hash=sha256:{r['sha256']}" for r in lock}

@pytest.mark.parametrize('corruption',['hash','extra','traversal'])
def test_wheel_harness_refuses_untrusted_inputs(tmp_path,corruption):
    wheels=tmp_path/'wheels';wheels.mkdir();dest=tmp_path/'dest';dest.mkdir()
    path=wheels/'fixture.whl'
    with zipfile.ZipFile(path,'w') as archive:archive.writestr('../escape.py' if corruption=='traversal' else 'fixture.py','pass\n')
    row={'filename':path.name,'name':'fixture','sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    if corruption=='hash':row['sha256']='0'*64
    if corruption=='extra':(wheels/'extra.whl').write_bytes(b'unreviewed')
    with pytest.raises(RuntimeError):harness.unpack_wheels(wheels,[row],dest)
    assert not (tmp_path/'escape.py').exists()


def test_old_or_unpatched_image_is_rejected(tmp_path):
    (tmp_path/'file.py').write_text('old = True\n')
    with pytest.raises(RuntimeError,match='Built image API patch mismatch'):
        harness.verify_patched_source(tmp_path,{'file.py':('irrelevant','0'*64,())})


def test_shell_propagates_container_test_failure(tmp_path):
    fake=tmp_path/'bin';fake.mkdir();calls=tmp_path/'calls'
    docker=fake/'docker'
    docker.write_text('''#!/bin/sh
if [ "$1" = image ]; then echo sha256:fixture-built-image; exit 0; fi
printf '%s\\n' "$@" > "$API_CONTRACT_TEST_CALLS"
exit 42
''');docker.chmod(0o755)
    python=fake/'python3'
    python.write_text('''#!/bin/sh
[ "$1" = -m ] && [ "$2" = venv ] || exit 98
mkdir -p "$3/bin"
printf '#!/bin/sh\\nexit 0\\n' > "$3/bin/python"
chmod +x "$3/bin/python"
''');python.chmod(0o755)
    result=subprocess.run(['sh',str(REPO/'tools/ci-api-contracts.sh'),'candidate:fixture'],
        env={**os.environ,'PATH':str(fake)+os.pathsep+os.environ['PATH'],'API_CONTRACT_TEST_CALLS':str(calls)},capture_output=True,text=True)
    assert result.returncode==42
    args=calls.read_text().splitlines()
    assert args[args.index('--network')+1]=='none'
    assert '--read-only' in args and '--pull' in args and 'never' in args
    assert 'sha256:fixture-built-image' in args and 'candidate:fixture' not in args
    assert '-I' in args and '-B' in args


def test_empty_or_skipped_suite_is_not_green():
    class Session:
        testscollected=0
        exitstatus=0
    plugin=harness.RequireExecutedTests();session=Session()
    plugin.pytest_sessionfinish(session,0)
    assert session.exitstatus==1
    session.testscollected=1;session.exitstatus=0;plugin.skipped=1
    plugin.pytest_sessionfinish(session,0)
    assert session.exitstatus==1


@pytest.mark.parametrize('mode,expected',[
    ('pass',0), ('empty',1), ('runtime_skip',1), ('collection_skip',1),
    ('collection_error',2), ('runtime_error',1),
])
def test_real_pytest_events_enforce_execution_guards(tmp_path,mode,expected):
    """Exercise real collection and runtest reports, including mixed pass + skip."""
    import sys
    contracts=tmp_path/'contracts';contracts.mkdir()
    if mode!='empty':
        (contracts/'test_pass.py').write_text('def test_pass(): assert True\n')
    additional={
        'runtime_skip': 'import pytest\ndef test_skip(): pytest.skip("fixture runtime skip")\n',
        'collection_skip': 'import pytest\npytest.skip("fixture collection skip", allow_module_level=True)\n',
        'collection_error': 'raise ImportError("fixture collection failure")\n',
        'runtime_error': 'def test_failure(): assert False, "fixture failure"\n',
    }
    if mode in additional:
        (contracts/'test_negative.py').write_text(additional[mode])
    runner=tmp_path/'run_guard.py'
    runner.write_text("""import importlib.util,sys
import pytest
spec=importlib.util.spec_from_file_location('api_harness',sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
raise SystemExit(pytest.main(['-q','-p','no:cacheprovider','--confcutdir='+sys.argv[2],sys.argv[2]],plugins=[m.RequireExecutedTests()]))
""")
    result=subprocess.run([sys.executable,'-I','-B',str(runner),str(HARNESS/'run_image_contracts.py'),str(contracts)],
        env={**os.environ,'PYTEST_DISABLE_PLUGIN_AUTOLOAD':'1'},capture_output=True,text=True,timeout=30)
    assert result.returncode==expected,result.stdout+result.stderr
    if mode in ('runtime_skip','collection_skip'):
        assert '1 passed, 1 skipped' in result.stdout
