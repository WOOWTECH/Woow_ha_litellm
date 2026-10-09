"""Static contract of the add-on (DESIGN v3.2 §4, §5, §6, §7, §11, §12.2 test_config_contract)."""

import hashlib
import json
import re
import subprocess

import pytest
import yaml

from conftest import ADDON, REPO, ROOTFS, SHARE

CONFIG = yaml.safe_load((ADDON / "config.yaml").read_text())
DOCKERFILE = (ADDON / "Dockerfile").read_text()
LOCK = json.loads((ADDON / "upstream.lock.json").read_text())
PLUGIN_LOCK = json.loads((ADDON / "woow-plugin.lock.json").read_text())
S6 = ROOTFS / "etc/s6-overlay/s6-rc.d"
BUNDLE = ROOTFS / "etc/s6-overlay/user-bundles.d/user/contents.d"

ONESHOTS = {
    "init-woow-perms": "base",
    "init-woow-env": "init-woow-perms",
    "init-woow-secrets": "init-woow-env",
    "init-woow-supervisor": "init-woow-secrets",
    "init-postgres": "init-woow-supervisor",
    "init-db": "postgres",
    "litellm-predump": "init-db",
    "litellm-migrate": "litellm-predump",
    "woow-revoke-ui-sessions": "litellm-migrate",
    "woow-spendlogs-indexes": "litellm",
}
LONGRUNS = {"postgres": "init-postgres", "litellm": "woow-revoke-ui-sessions", "nginx": "init-woow-supervisor"}


def code_lines(path) -> str:
    return "\n".join(ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#"))


def dockerfile_instructions() -> list[str]:
    joined = re.sub(r"\\\n", " ", DOCKERFILE)
    return [ln.strip() for ln in joined.splitlines() if ln.strip() and not ln.strip().startswith("#")]


# ── config.yaml (§5.5, §6, §11 S1) ───────────────────────────────────────────
def test_prebuilt_image_only():
    assert CONFIG["image"] == "ghcr.io/woowtech/woow-ha-litellm-{arch}"
    assert not (ADDON / "build.yaml").exists() and not (ADDON / "build.json").exists()
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", CONFIG["version"])


def test_version_everywhere():
    v = CONFIG["version"]
    assert json.loads((SHARE / "build-info.json").read_text())["addon_version"] == v
    assert re.search(rf"^## {re.escape(v)}\b", (ADDON / "CHANGELOG.md").read_text(), re.M)
    assert f"ARG BUILD_VERSION={v}" in DOCKERFILE


def test_identity_and_platform():
    assert CONFIG["slug"] == "woow-litellm" and CONFIG["name"] == "Woow LiteLLM"
    assert CONFIG["arch"] == ["amd64"]
    assert CONFIG["init"] is False
    assert CONFIG["homeassistant"] == "2026.5.0"
    assert CONFIG["stage"] == "experimental"


def test_least_privilege():
    for key in ("hassio_api", "homeassistant_api", "auth_api", "docker_api", "host_network"):
        assert CONFIG[key] is False, key
    assert CONFIG["apparmor"] is True
    for key in ("full_access", "privileged", "devices", "host_pid", "host_ipc", "map", "hassio_role", "kernel_modules"):
        assert key not in CONFIG, key


def test_ingress_ports_webui_backup():
    assert CONFIG["ingress"] is True and CONFIG["ingress_port"] == 8099 and CONFIG["ingress_stream"] is True
    assert CONFIG["panel_admin"] is True and CONFIG["panel_title"] == "LiteLLM"
    assert CONFIG["ports"] == {"4000/tcp": None}
    assert set(CONFIG["ports_description"]) == {"4000/tcp"}
    assert CONFIG["webui"] == "http://[HOST]:[PORT:4000]/"
    assert CONFIG["backup"] == "cold"
    assert CONFIG["backup_exclude"] == ["**/pre-upgrade-dumps"]
    assert "watchdog" not in CONFIG
    assert CONFIG["timeout"] == 120


def test_option_defaults():
    o = CONFIG["options"]
    assert o["spend_logs_retention"] == "30d"
    assert o["api_docs"] is False
    assert o["chatgpt_subscription"] is True
    assert o["ui_session_duration"] == "4h"
    assert o["log_level"] == "warning"
    assert o["env_vars"] == []
    assert "master_key" not in o and "ui_password" not in o
    assert set(o) <= set(CONFIG["schema"])
    assert json.loads((REPO / "tests/fixtures/options.default.json").read_text()) == o


@pytest.mark.parametrize("lang", ["en", "zh-Hant"])
def test_translations_cover_schema_and_ports(lang):
    tr = yaml.safe_load((ADDON / f"translations/{lang}.yaml").read_text())
    assert set(tr["configuration"]) == set(CONFIG["schema"])
    assert set(tr["network"]) == set(CONFIG["ports"])
    for key, entry in tr["configuration"].items():
        assert entry.get("name") and entry.get("description"), f"{lang}: {key}"


# ── Dockerfile (§5.1, §5.2, §5.4, §7.6) ──────────────────────────────────────
def test_dockerfile_stages():
    froms = [ln for ln in dockerfile_instructions() if ln.startswith("FROM ")]
    assert froms == [
        "FROM ${LITELLM_IMAGE} AS upstream",
        "FROM upstream AS pruned",
        "FROM scratch AS s6-overlay",
        "FROM scratch AS base",
        "FROM base AS runtime",
    ]
    assert froms[-1] == "FROM base AS runtime", "the final stage must sit on the FROM scratch base"
    assert "COPY --from=pruned / /" in DOCKERFILE
    assert re.search(r'ENTRYPOINT \["/init"\]\s*$', DOCKERFILE)


def test_dockerfile_upstream_digest_matches_lock():
    ref = f"{LOCK['image']}:{LOCK['tag']}@{LOCK['index_digest']}"
    assert f"ARG LITELLM_IMAGE={ref}" in DOCKERFILE
    amd64 = LOCK["platforms"]["linux/amd64"]["manifest_digest"]
    assert f"io.woowtech.litellm.digest={amd64}" in DOCKERFILE
    assert f"io.woowtech.litellm.version={LOCK['litellm_version']}" in DOCKERFILE


def test_dockerfile_prunes_enterprise_and_chainguard():
    for path in ("/app/enterprise", "litellm_enterprise", "/usr/local/bin/pgbouncer", "/etc/apko.json",
                 "/etc/apk/keys/chainguard-*.rsa.pub", "/var/lib/db/sbom"):
        assert path in DOCKERFILE
    assert f"ARG WOLFI_SIGNING_PUB_SHA256={LOCK['wolfi_signing_key_sha256']}" in DOCKERFILE
    assert f"ARG WOLFI_REPOSITORY={LOCK['wolfi_repository']}" in DOCKERFILE


def test_dockerfile_apk_pins_equal_lock():
    run = next(ln for ln in dockerfile_instructions() if ln.startswith("RUN apk add"))
    pins = re.findall(r"([A-Za-z0-9_.+-]+=[0-9][A-Za-z0-9_.+-]*-r[0-9]+)", run.split("&&")[0])
    want = [f"{p['name']}={p['version']}" for p in LOCK["wolfi_apk"]["apk_add"]]
    assert pins == want
    assert len(want) == 38
    assert "rm -rf /var/lib/db/sbom" in run, "the new packages' SBOMs must not reach a layer"


def test_dockerfile_s6_overlay_checksums_equal_lock():
    adds = [ln for ln in dockerfile_instructions() if ln.startswith("ADD ")]
    assert len(adds) == len(LOCK["s6_overlay"]["assets"]) == 2
    for asset in LOCK["s6_overlay"]["assets"]:
        assert any(f"--checksum=sha256:{asset['sha256']}" in ln and asset["url"] in ln for ln in adds), asset["name"]


def test_lock_cosign_key_and_fixture():
    pub = REPO / LOCK["cosign_pub"]["path"]
    assert hashlib.sha256(pub.read_bytes()).hexdigest() == LOCK["cosign_pub"]["sha256"]
    rows = [ln for ln in (REPO / "tests/fixtures/enterprise-unguarded.txt").read_text().splitlines()
            if ln and not ln.startswith("#")]
    assert len(rows) == 14 and sum(r.endswith("\terror") for r in rows) == 1
    assert LOCK["source_date_epoch"] == 1791063324


def test_dockerfile_env_restates_upstream_env():
    joined = re.sub(r"\\\n\s*", " ", DOCKERFILE)
    envs = " ".join(ln for ln in joined.splitlines() if ln.startswith("ENV "))
    for kv in LOCK["env"]:
        assert kv in envs.split(), kv
    for kv in ("CHECKPOINT_DISABLE=1", "S6_BEHAVIOUR_IF_STAGE2_FAILS=2", "S6_CMD_WAIT_FOR_SERVICES_MAXTIME=0",
               "S6_KILL_GRACETIME=10000"):
        assert kv in envs.split()
    shipped = [ln for ln in (SHARE / "upstream.env").read_text().splitlines() if ln and not ln.startswith("#")]
    assert shipped == LOCK["env"]


def test_dockerfile_accounts():
    assert "addgroup -S -g 70 postgres" in DOCKERFILE and "-u 70 -G postgres" in DOCKERFILE
    assert "addgroup -S -g 101 woow-nginx" in DOCKERFILE and "-u 101 -G woow-nginx" in DOCKERFILE
    nginx = (ROOTFS / "etc/nginx/nginx.conf").read_text()
    assert re.search(r"^user woow-nginx woow-nginx;", nginx, re.M)
    init = (ROOTFS / "usr/local/lib/woow-litellm/woow_init.py").read_text()
    assert "LITELLM_UID = 65534" in init and "POSTGRES_UID = 70" in init and "NGINX_UID = 101" in init
    assert 65534 not in (70, 101)


def test_labels():
    for label in ("org.opencontainers.image.vendor=WOOWTECH", "io.hass.type=addon", "io.hass.arch=${BUILD_ARCH}",
                  "io.hass.version=${BUILD_VERSION}", "org.opencontainers.image.revision=${BUILD_REF}"):
        assert label in DOCKERFILE
    src = PLUGIN_LOCK["source"]
    assert f"io.woowtech.woow-plugin.source={src['chart_version']}@{src['commit']}" in DOCKERFILE
    labels = " ".join(ln for ln in dockerfile_instructions() if ln.startswith("LABEL "))
    assert "chainguard" not in labels.lower()


def test_healthcheck_start_period_covers_boot():
    m = re.search(r"HEALTHCHECK --start-period=(\d+)m", DOCKERFILE)
    assert m
    start = int(m.group(1)) * 60_000
    predump = int((S6 / "litellm-predump/timeout-up").read_text())
    migrate = int((S6 / "litellm-migrate/timeout-up").read_text())
    assert predump == 600_000 and migrate == 1_500_000
    assert start >= 60_000 + predump + migrate + 300_000
    assert "/usr/local/bin/woow-healthcheck" in DOCKERFILE
    hc = code_lines(ROOTFS / "usr/local/bin/woow-healthcheck")
    assert "/health/liveliness" in hc and "/health " not in hc


# ── s6 services (§7.1) ────────────────────────────────────────────────────────
def test_thirteen_services_enabled():
    names = {p.name for p in S6.iterdir()}
    assert names == set(ONESHOTS) | set(LONGRUNS)
    assert {p.name for p in BUNDLE.iterdir()} == names
    assert (ROOTFS / "etc/s6-overlay/user-bundles.d/user/type").read_text().strip() == "bundle"


@pytest.mark.parametrize("name,dep", list(ONESHOTS.items()) + list(LONGRUNS.items()))
def test_service_type_and_dependency(name, dep):
    kind = "oneshot" if name in ONESHOTS else "longrun"
    assert (S6 / name / "type").read_text().strip() == kind
    assert {p.name for p in (S6 / name / "dependencies.d").iterdir()} == {dep}


def test_with_contenv_only_where_designed():
    users = set()
    for svc in S6.iterdir():
        for f in ("up", "run", "finish"):
            p = svc / f
            if p.exists() and "with-contenv" in code_lines(p):
                users.add(svc.name)
    assert users == {"init-woow-env", "init-woow-supervisor"}
    for path in (ROOTFS / "usr/local/lib/woow-litellm").iterdir():
        assert "with-contenv" not in code_lines(path) or path.name == "woow_init.py"


def test_longrun_controls():
    assert (S6 / "postgres/down-signal").read_text().strip() == "SIGINT"
    assert int((S6 / "postgres/timeout-down").read_text()) == 60000
    assert int((S6 / "litellm/timeout-down").read_text()) == 30000
    for svc in ("postgres", "litellm"):
        assert (S6 / svc / "notification-fd").read_text().strip() == "3"
        assert (S6 / svc / "data/check").exists()
        assert "svc-finish" in (S6 / svc / "finish").read_text()
    run = (S6 / "litellm/run").read_text()
    assert "--log_config /usr/share/woow-litellm/uvicorn-log.json" in run
    assert "s6-setuidgid nobody" in run and "/command/cd /app" in run
    assert "woow_chatgpt.py restore" in run


def test_executable_bits():
    for path in list((ROOTFS / "usr/local/lib/woow-litellm").iterdir()) + list((ROOTFS / "usr/local/bin").iterdir()):
        if path.name in ("common.sh", "woow_init.py"):
            continue
        assert path.stat().st_mode & 0o111, f"{path} must be executable"
    for svc in S6.iterdir():
        for f in ("run", "finish", "data/check"):
            p = svc / f
            if p.exists():
                assert p.stat().st_mode & 0o111, f"{p} must be executable"


def test_uvicorn_log_config():
    cfg = json.loads((SHARE / "uvicorn-log.json").read_text())
    assert cfg["disable_existing_loggers"] is False
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert cfg["loggers"][name]["level"] == "WARNING"


def test_postgres_settings():
    conf = (SHARE / "postgresql.woow.conf").read_text()
    for line in ("listen_addresses = '127.0.0.1'", "max_connections = 30", "shared_buffers = 64MB", "jit = off",
                 "wal_level = minimal", "max_wal_senders = 0", "log_statement = 'none'"):
        assert line in conf
    hba = [ln.split() for ln in (SHARE / "pg_hba.conf").read_text().splitlines() if ln and not ln.startswith("#")]
    assert hba[0] == ["local", "all", "postgres", "peer"]
    assert hba[1] == ["host", "litellm", "litellm", "127.0.0.1/32", "scram-sha-256"]
    assert all(row[-1] == "reject" for row in hba[2:])


# ── Woow plugin copy (§5.9) ───────────────────────────────────────────────────
def test_plugin_copy_matches_lock():
    dest = REPO / PLUGIN_LOCK["destination"]
    assert set(PLUGIN_LOCK["files"]) == {"sitecustomize.py", "woow_chatgpt.py", "woow_chatgpt_plugin.py"}
    assert {p.name for p in dest.iterdir()} == set(PLUGIN_LOCK["files"])
    for name, want in PLUGIN_LOCK["files"].items():
        data = (dest / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == want["sha256"], name
        blob = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
        assert blob == want["git_blob"], name
    src = PLUGIN_LOCK["source"]
    assert re.fullmatch(r"[0-9a-f]{40}", src["commit"])
    assert src["path"] == "charts/litellm/files/" and src["repository"] == "woow-paas/woow-paas-charts"
    assert re.fullmatch(r"\d+\.\d+\.\d+", src["chart_version"])


def test_plugin_env_names_same_as_paas():
    init = (ROOTFS / "usr/local/lib/woow-litellm/woow_init.py").read_text()
    for name in ('"CHATGPT_TOKEN_DIR"', '"WOOW_CHATGPT_ENABLED"', '"WOOW_CHATGPT_DEFAULT_MODELS"', '"WOOW_PROXY_PORT"',
                 'CHATGPT_TOKEN_DIR_VALUE = "/run/woow-chatgpt"'):
        assert name in init


# ── workflow rules (§5.7, §12.1) ──────────────────────────────────────────────
def workflows():
    return sorted((REPO / ".github/workflows").glob("*.yml"))


def test_workflows_never_cache_push_or_upload_images():
    assert workflows()
    for wf in workflows():
        text = wf.read_text()
        assert isinstance(yaml.safe_load(text), dict), wf.name
        for bad in ("cache-to", "type=gha", "mode=max", "cache-from"):
            assert bad not in text, f"{wf.name}: {bad}"
        if wf.name == "ci.yml":
            assert "docker push" not in text and "--push" not in text and "push: true" not in text
            assert "upload-artifact" not in text
            assert "docker save" not in text
        if "docker/build-push-action" in text:
            assert "DOCKER_BUILD_RECORD_UPLOAD: false" in text and "DOCKER_BUILD_SUMMARY: false" in text


def test_actions_pinned_by_sha():
    for wf in workflows():
        for ref in re.findall(r"uses:\s*([^\s#]+)", wf.read_text()):
            assert re.search(r"@[0-9a-f]{40}$", ref), f"{wf.name}: {ref} is not pinned to a commit"


def test_shell_scripts_parse():
    for path in list((ROOTFS / "usr/local/lib/woow-litellm").iterdir()) + list(S6.glob("*/run")) + list(
        S6.glob("*/finish")) + list(S6.glob("*/data/check")) + list((REPO / "tools").glob("*.sh")):
        if path.suffix == ".py" or path.name == "woow-init":
            continue
        subprocess.run(["sh", "-n", str(path)], check=True)
