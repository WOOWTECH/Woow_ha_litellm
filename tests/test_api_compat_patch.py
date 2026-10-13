"""Build guard failure must happen before any file is modified."""
import hashlib
import importlib.util
from pathlib import Path
import pytest

SCRIPT=Path(__file__).resolve().parents[1]/'litellm/patches/apply_api_compat.py'
spec=importlib.util.spec_from_file_location('api_compat_patch',SCRIPT)
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def pin(before,after,old='value = 1',new='value = 2',count=1):
    sha=lambda t:hashlib.sha256(t.encode()).hexdigest()
    return (sha(before),sha(after),((old,new,count),))

@pytest.mark.parametrize('failure',['input_hash','context','output_hash','syntax'])
def test_validate_all_before_any_write(tmp_path,monkeypatch,failure):
    before='value = 1\n';after='value = 2\n'
    for name in ('first.py','last.py'): (tmp_path/name).write_text(before)
    bad=pin(before,after)
    if failure=='input_hash': bad=('0'*64,bad[1],bad[2])
    if failure=='context': bad=pin(before,after,old='missing')
    if failure=='output_hash': bad=(bad[0],'0'*64,bad[2])
    if failure=='syntax': bad=pin(before,'value = [\n',new='value = [')
    monkeypatch.setattr(module,'PATCHES',{'first.py':pin(before,after),'last.py':bad})
    with pytest.raises((SystemExit,SyntaxError)):module.apply(tmp_path)
    assert all((tmp_path/name).read_text()==before for name in ('first.py','last.py'))


def test_success_and_repeat_refusal(tmp_path,monkeypatch):
    before='value = 1\n';after='value = 2\n'
    (tmp_path/'one.py').write_text(before)
    monkeypatch.setattr(module,'PATCHES',{'one.py':pin(before,after)})
    module.apply(tmp_path)
    assert (tmp_path/'one.py').read_text()==after
    with pytest.raises(SystemExit,match='Refusing changed upstream source'):module.apply(tmp_path)
    assert (tmp_path/'one.py').read_text()==after


def test_dockerfile_wires_distinct_patch():
    docker=(SCRIPT.parents[2]/'litellm/Dockerfile').read_text()
    assert 'COPY patches/ /usr/share/woow-litellm/patches/' in docker
    assert 'RUN python -I /usr/share/woow-litellm/patches/apply_api_compat.py' in docker
