"""Build installer guardrails; provider behavior is tested against real LiteLLM."""
import hashlib
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PATCHES = REPO / "litellm/patches"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


patch = load("chatgpt_roles", PATCHES / "apply_chatgpt_system_role.py")


@pytest.fixture
def tree(tmp_path, monkeypatch):
    before, after = "value = 1\n", "value = 2\n"
    hashes = [hashlib.sha256(s.encode()).hexdigest() for s in (before, after)]
    monkeypatch.setattr(patch, "PATCHES", {"target.py": (*hashes, ((before, after, 1),))})
    (tmp_path / "target.py").write_text(before)
    return tmp_path


@pytest.mark.parametrize("failure", ["input_hash", "context", "output_hash", "syntax"])
def test_invalid_source_or_patch_never_writes(tree, monkeypatch, failure):
    before, after, edits = patch.PATCHES["target.py"]
    bad = {
        "input_hash": ("0" * 64, after, edits),
        "context": (before, after, (("absent", "value = 2", 1),)),
        "output_hash": (before, "0" * 64, edits),
        "syntax": (before, hashlib.sha256(b"value = [\n").hexdigest(),
                   (("value = 1\n", "value = [\n", 1),)),
    }[failure]
    monkeypatch.setitem(patch.PATCHES, "target.py", bad)
    with pytest.raises((SystemExit, SyntaxError)):
        patch.apply(tree)
    assert (tree / "target.py").read_text() == "value = 1\n"


def test_success_and_second_application_is_refused(tree):
    patch.apply(tree)
    assert (tree / "target.py").read_text() == "value = 2\n"
    with pytest.raises(SystemExit, match="changed upstream source"):
        patch.apply(tree)
    assert (tree / "target.py").read_text() == "value = 2\n"


def test_symlink_cannot_redirect_the_write(tree):
    (tree / "outside.py").write_text("value = 1\n")
    (tree / "target.py").unlink()
    (tree / "target.py").symlink_to(tree / "outside.py")
    with pytest.raises(SystemExit, match="symlink"):
        patch.apply(tree)
    assert (tree / "outside.py").read_text() == "value = 1\n"


def test_final_image_contract_binds_the_prior_and_new_provider_hashes(tmp_path):
    harness = load("role_harness", REPO / "tests/api_contract_harness/run_image_contracts.py")
    earlier = [load("role_" + name, PATCHES / ("apply_" + name + ".py"))
               for name in ("api_compat", "routes_auth", "api_errors", "upstream_guard",
                            "m4_batch1", "stream_slot_release")]
    final = harness.final_api_patches(*earlier, patch)
    target = "llms/chatgpt/responses/transformation.py"
    assert patch.PATCHES[target][0] == "f20de0053f82f2fc1826b4bef778c02d8235fe02c0fca50dc560bea98a4eb258"
    assert final[target][1] == patch.PATCHES[target][1]
    installed = tmp_path / target
    installed.parent.mkdir(parents=True)
    installed.write_text("unpatched_provider = True\n")
    with pytest.raises(RuntimeError, match="Built image API patch mismatch"):
        harness.verify_patched_source(tmp_path, {target: final[target]})


def test_native_build_installs_patch_and_final_image_harness_verifies_it():
    docker = (REPO / "litellm/Dockerfile").read_text()
    assert docker.index("RUN python -I /usr/share/woow-litellm/patches/apply_chatgpt_system_role.py") > \
        docker.index("RUN python -I /usr/share/woow-litellm/patches/apply_stream_slot_release.py")
    assert '"$REPO/litellm/patches/apply_chatgpt_system_role.py"' in (REPO / "tools/ci-api-contracts.sh").read_text()
    assert "'apply_chatgpt_system_role.py'" in (REPO / "tests/api_contract_harness/run_image_contracts.py").read_text()
