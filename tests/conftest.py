import json
import os
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
ADDON = REPO / "litellm"
ROOTFS = ADDON / "rootfs"
SHARE = ROOTFS / "usr/share/woow-litellm"
INIT_PY = ROOTFS / "usr/local/lib/woow-litellm/woow_init.py"

DEFAULT_OPTIONS = json.loads((REPO / "tests/fixtures/options.default.json").read_text())


class Box:
    """A temporary root for running woow_init.py the way s6 does, without root or a container."""

    def __init__(self, tmp: pathlib.Path):
        self.tmp = tmp
        self.data = tmp / "data"
        self.data.mkdir()
        self.run_dir = tmp / "run"
        self.run_dir.mkdir()
        self.container_env = self.run_dir / "s6" / "container_environment"
        self.container_env.mkdir(parents=True)
        (tmp / "shm").mkdir()
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "WOOW_DATA_DIR": str(self.data),
            "WOOW_RUN_DIR": str(self.run_dir / "woow"),
            "WOOW_CHATGPT_DIR": str(self.run_dir / "woow-chatgpt"),
            "WOOW_LITELLM_RUN_DIR": str(self.run_dir / "litellm"),
            "WOOW_CONTAINER_ENV_DIR": str(self.container_env),
            "WOOW_SHARE_DIR": str(SHARE),
            "WOOW_SHM_DIR": str(tmp / "shm"),
            "WOOW_TMPFS_CHECK": "0",
            "WOOW_SUPERVISOR_RETRY_SECONDS": "4",
            "TZ": "Asia/Taipei",
        }
        self.options(DEFAULT_OPTIONS)

    def options(self, opts: dict, **overrides) -> None:
        merged = {**opts, **overrides}
        (self.data / "options.json").write_text(json.dumps(merged))

    def run(self, *args: str, env: dict | None = None, stdin: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-I", str(INIT_PY), *args],
            env={**self.env, **(env or {})},
            input=stdin,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def ok(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        res = self.run(*args, env=env)
        assert res.returncode == 0, f"{args} failed:\n{res.stderr}"
        return res

    def boot(self, env: dict | None = None) -> None:
        for step in ("perms", "env", "secrets", "supervisor"):
            self.ok(step, env=env)

    def envdir(self, service: str) -> dict[str, str]:
        d = self.run_dir / "woow" / "env" / service
        return {p.name: p.read_text() for p in d.iterdir()}

    def secret(self, name: str) -> str:
        return (self.data / "secrets" / name).read_text()

    def state(self) -> dict:
        p = self.data / "woow" / "state.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def ingress(self) -> dict:
        return json.loads((self.run_dir / "woow" / "ingress.json").read_text())

    def site_conf(self) -> str:
        return (self.run_dir / "woow" / "nginx" / "site.conf").read_text()


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)
