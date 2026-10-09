"""woow_init.py in a temporary root (DESIGN v3.2 §7.1–§7.4, §8.2, §8.3, §12.2 test_init_scripts)."""

import base64
import hashlib
import hmac
import http.server
import json
import os
import re
import stat
import threading
import time

import pytest

from conftest import DEFAULT_OPTIONS

ENTRY = "/api/hassio_ingress/AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-abcde"

UPSTREAM_KEYS = {
    "PATH", "SSL_CERT_FILE", "PRISMA_BINARY_CACHE_DIR", "PRISMA_CLI_PATH", "PRISMA_CLI_QUERY_ENGINE_TYPE", "HOME",
    "LITELLM_NON_ROOT", "PRISMA_SKIP_POSTINSTALL_GENERATE", "PRISMA_HIDE_UPDATE_MESSAGE",
    "PRISMA_ENGINES_CHECKSUM_IGNORE_MISSING", "PRISMA_OFFLINE_MODE",
}
COMMON_KEYS = UPSTREAM_KEYS | {
    "TZ", "STORE_MODEL_IN_DB", "LITELLM_MODE", "NUM_WORKERS", "GRACEFUL_SHUTDOWN_TIMEOUT", "CHECKPOINT_DISABLE",
    "LITELLM_LOCAL_MODEL_COST_MAP", "LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS", "LITELLM_LOCAL_AUTOROUTER_PRESETS",
    "LITELLM_LOCAL_BLOG_POSTS", "LITELLM_LOCAL_POLICY_TEMPLATES", "LITELLM_DISABLE_NO_REDIS_WARNING", "LITELLM_LOG",
    "LITELLM_MASTER_KEY", "LITELLM_SALT_KEY", "DATABASE_URL", "UI_PASSWORD",
}
LITELLM_ONLY = {
    "DISABLE_SCHEMA_UPDATE", "UI_USERNAME", "LITELLM_UI_SESSION_DURATION", "LITELLM_UI_PATH",
    "LITELLM_EXPIRED_UI_SESSION_KEY_CLEANUP_ENABLED", "LITELLM_EXPIRED_UI_SESSION_KEY_CLEANUP_INTERVAL_SECONDS",
    "FORWARDED_ALLOW_IPS", "ROOT_REDIRECT_URL", "NO_DOCS", "NO_REDOC", "NO_OPENAPI",
}
PLUGIN = {"PYTHONPATH", "CHATGPT_TOKEN_DIR", "WOOW_CHATGPT_ENABLED", "WOOW_CHATGPT_DEFAULT_MODELS", "WOOW_PROXY_PORT"}


def mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


# ── Supervisor stand-in ──────────────────────────────────────────────────────
class FakeSupervisor:
    def __init__(self, token="test-token", supervisor="2026.10.1", core="2026.10.0", entry=ENTRY, options=None):
        self.token = token
        self.versions = {"supervisor": supervisor, "homeassistant": core}
        self.entry = entry
        self.options = dict(options if options is not None else DEFAULT_OPTIONS)
        self.posts = []
        self.unavailable_until = 0.0
        self.always_fail = False
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _gate(self):
                if fake.always_fail or time.monotonic() < fake.unavailable_until:
                    self._send(503, {"result": "error"})
                    return False
                if self.headers.get("Authorization") != f"Bearer {fake.token}":
                    self._send(401, {"result": "error", "message": "unauthorized"})
                    return False
                return True

            def do_GET(self):
                if not self._gate():
                    return
                if self.path == "/info":
                    self._send(200, {"result": "ok", "data": dict(fake.versions)})
                elif self.path == "/addons/self/info":
                    data = {"options": dict(fake.options), "hostname": "1b7b4ce7-woow-litellm"}
                    if fake.entry is not None:
                        data["ingress_entry"] = fake.entry
                    self._send(200, {"result": "ok", "data": data})
                else:
                    self._send(404, {"result": "error"})

            def do_POST(self):
                if not self._gate():
                    return
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/addons/self/options":
                    fake.posts.append(body)
                    fake.options = dict(body["options"])
                    self._send(200, {"result": "ok", "data": {}})
                else:
                    self._send(404, {"result": "error"})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def env(self):
        return {"SUPERVISOR_TOKEN": self.token, "WOOW_SUPERVISOR_URL": self.url}

    def close(self):
        self.server.shutdown()


@pytest.fixture
def sup():
    s = FakeSupervisor()
    yield s
    s.close()


# ── first boot, env dirs (§7.2 table) ─────────────────────────────────────────
def test_first_boot_local_mode_env_complete(box):
    box.boot()
    lit, mig = box.envdir("litellm"), box.envdir("litellm-migrate")
    for key in COMMON_KEYS | LITELLM_ONLY | PLUGIN:
        assert lit.get(key), f"litellm env lacks {key}"
    for key in COMMON_KEYS:
        assert mig.get(key), f"litellm-migrate env lacks {key}"
    forbidden = {"PYTHONPATH", "SERVER_ROOT_PATH", "ROOT_REDIRECT_URL"}
    assert not (forbidden & set(mig)), "litellm-migrate must not load the plugin or rewrite the UI"
    assert not [k for k in mig if k.startswith("WOOW_")]
    assert "SERVER_ROOT_PATH" not in lit  # no ingress_entry in a local run
    assert lit["ROOT_REDIRECT_URL"] == "/ui/"
    assert lit["FORWARDED_ALLOW_IPS"] == "127.0.0.1"
    assert lit["TZ"] == "Asia/Taipei"
    assert lit["DISABLE_SCHEMA_UPDATE"] == "true"
    assert "DISABLE_SCHEMA_UPDATE" not in mig
    assert lit["CHATGPT_TOKEN_DIR"] == "/run/woow-chatgpt"
    assert lit["PYTHONPATH"] == "/usr/share/woow-litellm/py"
    assert lit["LITELLM_UI_SESSION_DURATION"] == "4h"
    assert lit["LITELLM_EXPIRED_UI_SESSION_KEY_CLEANUP_INTERVAL_SECONDS"] == "3600"
    assert lit["DATABASE_URL"] == f"postgresql://litellm:{box.secret('pg_password')}@127.0.0.1:5432/litellm"
    assert lit["LITELLM_MASTER_KEY"] == box.secret("master_key")
    assert lit["UI_PASSWORD"] == box.secret("ui_password")
    for d in ("litellm", "litellm-migrate"):
        envdir = box.run_dir / "woow/env" / d
        assert mode(envdir) == 0o700
        assert all(mode(p) == 0o600 for p in envdir.iterdir())


def test_secrets_shapes_and_modes(box):
    box.boot()
    assert re.fullmatch(r"sk-[0-9a-f]{64}", box.secret("master_key"))
    assert re.fullmatch(r"sk-[0-9a-f]{64}", box.secret("salt_key"))
    assert re.fullmatch(r"[A-Za-z0-9]{24}", box.secret("ui_password"))
    assert re.fullmatch(r"[A-Za-z0-9]{32}", box.secret("pg_password"))
    assert mode(box.data / "secrets") == 0o700
    for name in ("master_key", "salt_key", "pg_password", "ui_password"):
        assert mode(box.data / "secrets" / name) == 0o600
    assert mode(box.data / "woow") == 0o700
    assert mode(box.data / "postgres") == 0o700
    assert mode(box.data / "pre-upgrade-dumps") == 0o700
    assert mode(box.run_dir / "woow-chatgpt") == 0o700


def test_secrets_stable_across_starts(box):
    box.boot()
    first = {n: box.secret(n) for n in ("master_key", "salt_key", "pg_password", "ui_password")}
    box.boot()
    assert first == {n: box.secret(n) for n in first}


def test_master_key_and_ui_password_from_options(box):
    box.boot()
    salt = box.secret("salt_key")
    key = "sk-Zq8x_Lm2-Pw7Rt4Yv9Bn3Kd6Hs1Gf0Jc5Ue"
    box.options(DEFAULT_OPTIONS, master_key=key, ui_password="correct horse battery")
    box.boot()
    assert box.secret("master_key") == key
    assert box.secret("ui_password") == "correct horse battery"
    assert box.secret("salt_key") == salt
    assert box.envdir("litellm")["LITELLM_MASTER_KEY"] == key


def test_existing_database_without_salt_refuses(box):
    box.boot()
    (box.data / "postgres" / "PG_VERSION").write_text("18\n")
    (box.data / "secrets" / "salt_key").unlink()
    res = box.run("secrets")
    assert res.returncode == 1
    assert "salt_key is missing" in res.stderr
    assert not (box.data / "secrets" / "salt_key").exists(), "a new salt key must never be generated"


def test_litellm_config_yaml(box):
    box.boot()
    text = (box.run_dir / "litellm" / "config.yaml").read_text()
    assert "master_key: os.environ/LITELLM_MASTER_KEY" in text
    assert 'maximum_spend_logs_retention_period: "30d"' in text
    assert 'maximum_spend_logs_cleanup_cron: "30 3 * * *"' in text
    assert 'trusted_proxy_ranges: ["127.0.0.1/32"]' in text
    assert "use_x_forwarded_for" not in text
    assert "callbacks" not in text
    assert "password_policy_check_breached_passwords" not in text
    assert mode(box.run_dir / "litellm" / "config.yaml") == 0o640
    box.options(DEFAULT_OPTIONS, password_breach_check=False, spend_logs_retention="12h")
    box.ok("env")
    text = (box.run_dir / "litellm" / "config.yaml").read_text()
    assert "password_policy_check_breached_passwords: false" in text
    assert 'maximum_spend_logs_retention_period: "12h"' in text


def test_chatgpt_subscription_off(box):
    box.options(DEFAULT_OPTIONS, chatgpt_subscription=False)
    box.boot()
    lit = box.envdir("litellm")
    assert not (PLUGIN & set(lit))
    assert not [k for k in lit if k.startswith(("WOOW_", "CHATGPT_"))]


def test_api_docs_switch(box):
    box.boot()
    lit = box.envdir("litellm")
    assert lit["NO_DOCS"] == lit["NO_REDOC"] == lit["NO_OPENAPI"] == "True" and "DOCS_URL" not in lit
    box.options(DEFAULT_OPTIONS, api_docs=True)
    box.boot()
    lit = box.envdir("litellm")
    assert lit["DOCS_URL"] == "/docs" and not ({"NO_DOCS", "NO_REDOC", "NO_OPENAPI"} & set(lit))


# ── option validation (§6.1, §12.2 item 10) ───────────────────────────────────
@pytest.mark.parametrize(
    "key",
    [
        "FORWARDED_ALLOW_IPS", "ROOT_REDIRECT_URL", "LITELLM_DANGEROUSLY_PERMIT_WEAK_OR_UNSET_MASTER_KEY",
        "CHATGPT_AUTH_FILE", "CHATGPT_API_BASE", "WOOW_CHATGPT_ENABLED", "PYTHONPATH", "PYTHONSTARTUP",
        "DATABASE_URL", "DATABASE_HOST", "SERVER_ROOT_PATH", "SERVER_ROOT_PATHS", "LITELLM_PGBOUNCER_ENABLED",
        "LITELLM_EXPIRED_UI_SESSION_KEY_CLEANUP_INTERVAL_SECONDS", "S6_KEEP_ENV", "SUPERVISOR_TOKEN", "HASSIO_TOKEN",
        "PRISMA_CLI_PATH", "LD_PRELOAD", "TZ", "PATH", "HOME", "UI_PASSWORD", "LITELLM_MASTER_KEY", "LITELLM_SALT_KEY",
        "PROXY_BASE_URL", "NUM_WORKERS", "DOCS_URL", "NO_DOCS", "STORE_MODEL_IN_DB", "DISABLE_SCHEMA_UPDATE",
        "CHECKPOINT_DISABLE", "LITELLM_MODE", "PORT", "HOST", "DIRECT_URL", "LITELLM_MIGRATE_FROM_MASTER_KEY",
    ],
)
def test_reserved_env_vars_refused(box, key):
    box.options(DEFAULT_OPTIONS, env_vars=[{"key": key, "value": "x"}])
    res = box.run("env")
    assert res.returncode == 1
    assert key in res.stderr and "cannot be overridden" in res.stderr


def test_user_env_var_reaches_litellm_only_and_is_masked(box):
    box.options(
        DEFAULT_OPTIONS,
        env_vars=[{"key": "OPENAI_API_KEY", "value": "sk-not-a-real-key-123"}, {"key": "LITELLM_LOG", "value": "DEBUG"}],
    )
    res = box.run("env")
    assert res.returncode == 0, res.stderr
    assert "sk-not-a-real-key-123" not in res.stderr and "OPENAI_API_KEY=******" in res.stderr
    box.ok("secrets")
    assert box.envdir("litellm")["OPENAI_API_KEY"] == "sk-not-a-real-key-123"
    assert box.envdir("litellm")["LITELLM_LOG"] == "DEBUG"
    assert "OPENAI_API_KEY" not in box.envdir("litellm-migrate")


@pytest.mark.parametrize(
    "override, needle",
    [
        ({"master_key": "sk-1234"}, "master_key"),
        ({"master_key": "sk-" + "a" * 40}, "weak"),
        ({"master_key": "not-a-key-at-all-but-long-enough-xxxxxxxxx"}, "master_key"),
        ({"ui_password": "short"}, "ui_password"),
        ({"ui_session_duration": "25h"}, "ui_session_duration"),
        ({"ui_session_duration": "0h"}, "ui_session_duration"),
        ({"log_level": "debug"}, "log_level"),
        ({"spend_logs_retention": "30m"}, "spend_logs_retention"),
        ({"spend_logs_cleanup_cron": "30 3 * *"}, "spend_logs_cleanup_cron"),
        ({"ui_username": "bad user"}, "ui_username"),
        ({"env_vars": [{"key": "lower", "value": "x"}]}, "upper-case"),
        ({"env_vars": [{"key": "A_B", "value": "x"}, {"key": "A_B", "value": "y"}]}, "twice"),
        ({"env_vars": [{"key": "A_B", "value": "x\ny"}]}, "single line"),
    ],
)
def test_invalid_options_refused(box, override, needle):
    box.options(DEFAULT_OPTIONS, **override)
    res = box.run("env")
    assert res.returncode == 1
    assert needle in res.stderr
    assert "sk-1234" not in res.stdout


def test_cron_checked_with_apscheduler(box):
    pytest.importorskip("apscheduler")
    box.options(DEFAULT_OPTIONS, spend_logs_cleanup_cron="61 3 * * *")
    res = box.run("env")
    assert res.returncode == 1 and "APScheduler rejects it" in res.stderr
    box.options(DEFAULT_OPTIONS, spend_logs_cleanup_cron="*/2 * * * mon-fri")
    box.ok("env")


# ── Supervisor: ingress_entry, nginx site, write-back (§7.1, §7.4, §8.3) ─────
def test_ingress_entry_sets_root_path_and_nginx_site(box, sup):
    (box.container_env / "SUPERVISOR_TOKEN").write_text(sup.token)
    (box.container_env / "HASSIO_TOKEN").write_text(sup.token)
    (box.container_env / "TZ").write_text("Asia/Taipei")
    box.boot(env=sup.env())
    lit = box.envdir("litellm")
    assert lit["SERVER_ROOT_PATH"] == ENTRY
    assert lit["ROOT_REDIRECT_URL"] == ENTRY + "/ui/"
    assert "SERVER_ROOT_PATH" not in box.envdir("litellm-migrate")
    assert box.ingress()["mode"] == "ingress"
    conf = box.site_conf()
    assert f"proxy_pass http://127.0.0.1:4000{ENTRY}$request_uri;" in conf
    assert f"proxy_cookie_path ~^/$ {ENTRY}/;" in conf
    assert "proxy_redirect ~^https?://[^/]+(/.*)$ $1;" in conf
    assert "proxy_redirect default" not in conf.replace("`proxy_redirect default`", "")
    assert "proxy_set_header Host $http_host;" in conf
    assert '"~*^deny$" "SAMEORIGIN";' in conf
    assert "\"frame-ancestors 'none'\" \"frame-ancestors 'self'\";" in conf
    assert 'if ($realip_remote_addr != "172.30.32.2")' in conf
    assert "$http_x_ingress_path" not in conf, "the prefix must never come from a request header"
    assert not re.search(r"@[A-Z_]+@", conf)
    # the Supervisor token is gone from the container environment before any longrun starts (S2)
    assert not (box.container_env / "SUPERVISOR_TOKEN").exists()
    assert not (box.container_env / "HASSIO_TOKEN").exists()
    assert (box.container_env / "TZ").exists()


@pytest.mark.parametrize(
    "entry",
    [
        "/api/hassio_ingress/short",
        "/api/hassio_ingress/AbCdEfGhIjKlMnOpQr/../x",
        "/api/hassio_ingress/AbCdEfGhIjKlMnOpQr;return 200",
        "/api/hassio_ingress/AbCdEfGhIjKlMnOpQr$request_uri",
        "/elsewhere/AbCdEfGhIjKlMnOpQrStUv",
        "",
        None,
    ],
)
def test_bad_or_missing_ingress_entry_falls_back_to_help(box, sup, entry):
    sup.entry = entry
    box.boot(env=sup.env())
    assert box.ingress()["mode"] == "help"
    assert "SERVER_ROOT_PATH" not in box.envdir("litellm")
    assert box.envdir("litellm")["ROOT_REDIRECT_URL"] == "/ui/"
    conf = box.site_conf()
    assert "allow 172.30.32.2;" in conf and "deny all;" in conf
    assert "root /run/woow/help;" in conf
    if entry:
        assert entry not in conf


def test_help_page_has_no_secrets(box):
    box.boot()
    assert box.ingress() == {"mode": "help", "entry": None, "reason": "no-token", "writeback": "skipped (no Supervisor)"}
    page = (box.run_dir / "woow/help/index.html").read_text()
    for name in ("master_key", "salt_key", "pg_password", "ui_password"):
        assert box.secret(name) not in page
    assert not re.search(r"@[A-Z_]+@", page)
    assert "1.104.0" in page and ":4000" in page
    assert (box.run_dir / "woow/help/status.js").exists()


def test_writeback_merges_and_happens_once(box, sup):
    sup.options = {**DEFAULT_OPTIONS, "ui_username": "bob", "log_level": "error"}
    box.options(sup.options)
    box.boot(env=sup.env())
    assert len(sup.posts) == 1
    sent = sup.posts[0]["options"]
    assert sent["master_key"] == box.secret("master_key")
    assert sent["ui_password"] == box.secret("ui_password")
    assert sent["ui_username"] == "bob" and sent["log_level"] == "error", "user options must survive the merge"
    assert set(box.state()["writeback"]) == {"master_key", "ui_password"}
    assert box.secret("master_key") not in json.dumps(box.state())
    # next start: options already hold the values → nothing to do
    box.options(sup.options)
    box.boot(env=sup.env())
    assert len(sup.posts) == 1
    # the admin copies the key and clears the field: it is not written back again
    sup.options = {k: v for k, v in sup.options.items() if k != "master_key"}
    box.options(sup.options)
    box.boot(env=sup.env())
    assert len(sup.posts) == 1
    # the UI password file is lost and regenerated: the new value is written back once
    (box.data / "secrets" / "ui_password").unlink()
    sup.options = {k: v for k, v in sup.options.items() if k != "ui_password"}
    box.options(sup.options)
    box.boot(env=sup.env())
    assert len(sup.posts) == 2
    assert sup.posts[1]["options"]["ui_password"] == box.secret("ui_password")
    assert "master_key" not in sup.posts[1]["options"]


def test_writeback_waits_for_versions_then_catches_up(box, sup):
    sup.versions = {"supervisor": "2026.06.2", "homeassistant": "2026.10.0"}
    box.boot(env=sup.env())
    assert sup.posts == []
    assert "writeback" not in box.state() or not box.state()["writeback"]
    sup.versions = {"supervisor": "2026.07.0", "homeassistant": "2026.5.0"}
    box.options(sup.options)
    box.boot(env=sup.env())
    assert len(sup.posts) == 1
    assert sup.posts[0]["options"]["master_key"] == box.secret("master_key")


def test_supervisor_slow_then_ok(box, sup):
    sup.unavailable_until = time.monotonic() + 1.5
    box.boot(env=sup.env())
    assert box.ingress()["mode"] == "ingress"


def test_supervisor_silent_falls_back_and_starts(box, sup):
    sup.always_fail = True
    (box.container_env / "SUPERVISOR_TOKEN").write_text(sup.token)
    t0 = time.monotonic()
    box.boot(env=sup.env())
    assert time.monotonic() - t0 >= 3
    ing = box.ingress()
    assert ing["mode"] == "help" and ing["reason"] == "supervisor-unreachable"
    assert sup.posts == []
    assert not (box.container_env / "SUPERVISOR_TOKEN").exists()


# ── database statements ───────────────────────────────────────────────────────
def test_scram_sql_has_only_the_verifier(box):
    box.boot()
    password = box.secret("pg_password")
    res = box.ok("scram-sql")
    assert password not in res.stdout
    m = re.search(r"PASSWORD 'SCRAM-SHA-256\$4096:([^$]+)\$([^:]+):([^']+)'", res.stdout)
    assert m, res.stdout
    salt, stored, server = (base64.b64decode(x) for x in m.groups())
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 4096)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    assert hashlib.sha256(client_key).digest() == stored
    assert hmac.new(salted, b"Server Key", hashlib.sha256).digest() == server
    assert res.stdout.startswith("SET log_min_error_statement = panic;")


def test_platform_tables_match_paas_names(box):
    box.boot()
    sql = box.ok("platform-sql").stdout
    for table, cols in (
        ("settings", ("key        text PRIMARY KEY", "value      text NOT NULL", "updated_at timestamptz NOT NULL DEFAULT now()")),
        ("chatgpt_auth", ("id         smallint PRIMARY KEY CHECK (id = 1)", "auth       jsonb NOT NULL")),
        ("chatgpt_accounts", ("account    text PRIMARY KEY", "auth       jsonb NOT NULL")),
    ):
        assert f"CREATE TABLE IF NOT EXISTS woow_platform.{table} (" in sql
        for col in cols:
            assert col in sql
    assert "SET ROLE litellm;" in sql and "'litellm_salt_key'" in sql
    meta = box.ok("meta-sql").stdout
    fp = hashlib.sha256(("woow-litellm-salt-v1:" + box.secret("salt_key")).encode()).hexdigest()[:16]
    assert f"'{fp}'" in meta and box.secret("salt_key") not in meta


def test_revoke_check_transitions(box):
    box.boot()
    assert box.ok("revoke-check").stdout.strip() == "fresh"
    box.ok("revoke-record")
    assert box.ok("revoke-check").stdout.strip() == "unchanged"
    box.options(DEFAULT_OPTIONS, ui_password="another long password")
    box.boot()
    assert box.ok("revoke-check").stdout.strip() == "changed:ui_password"
    box.ok("revoke-record")
    assert box.ok("revoke-check").stdout.strip() == "unchanged"


def test_state_migrated(box):
    box.boot()
    assert box.ok("state-get", "litellm_version").stdout.strip() == ""
    box.ok("state-migrated")
    assert box.ok("state-get", "litellm_version").stdout.strip() == "1.104.0"
    assert box.state()["addon_version"] == "0.1.0"
    assert mode(box.data / "woow" / "state.json") == 0o600


# ── perms (§9.2) ──────────────────────────────────────────────────────────────
def test_perms_tightens_modes_after_restore(box):
    box.boot()
    for p in (box.data / "secrets", box.data / "postgres", box.data / "woow"):
        os.chmod(p, 0o755)
    (box.data / "postgres" / "PG_VERSION").write_text("18\n")
    os.chmod(box.data / "postgres" / "PG_VERSION", 0o644)
    os.chmod(box.data / "secrets" / "master_key", 0o644)
    box.ok("perms")
    assert mode(box.data / "secrets") == 0o700 and mode(box.data / "postgres") == 0o700
    assert mode(box.data / "postgres" / "PG_VERSION") == 0o600
    assert mode(box.data / "secrets" / "master_key") == 0o600


def test_perms_refuses_symlinked_data_dirs(box, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (box.data / "postgres").symlink_to(elsewhere)
    res = box.run("perms")
    assert res.returncode == 1 and "symbolic link" in res.stderr


def test_tmpfs_fallback_symlinks_into_shm(box, tmp_path):
    # with the check on, a non-tmpfs /run and a non-tmpfs "shm" must refuse (tokens never on disk)
    res = box.run("perms", env={"WOOW_TMPFS_CHECK": "1"})
    if res.returncode == 0:
        pytest.skip("this test machine has tmpfs where the test expected a disk")
    assert "must never be on disk" in res.stderr
