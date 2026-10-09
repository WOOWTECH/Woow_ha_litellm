"""Woow LiteLLM add-on: the boot steps that s6-rc runs as root (DESIGN v3.2 §7).

Run as ``/app/.venv/bin/python3 -I woow_init.py <command>`` through ``woow-init``:

  perms              restore-proof owners and modes under /data, tmpfs work dirs (§9.2, §7.1)
  env                validate the options, write the non-secret env dirs and /run/litellm/config.yaml
  secrets            create or load the secrets in /data/secrets, write the secret env files (§7.4)
  supervisor         Supervisor /info + /addons/self/info: ingress_entry, option write-back,
                     nginx site + help page, then drop SUPERVISOR_TOKEN from the container env
  scram-sql          ALTER ROLE statement with a SCRAM-SHA-256 verifier (never the password)
  meta-sql           woow.meta + salt fingerprint
  platform-sql       woow_platform tables (same names and columns as the PaaS chart) + salt copy
  state-get KEY      print one top-level value of /data/woow/state.json
  state-migrated     record the LiteLLM / add-on version after a successful migration
  revoke-check       fresh | unchanged | changed  (master key / UI password fingerprints)
  revoke-record      store the current fingerprints

Nothing here prints a secret. Paths can be redirected with WOOW_* variables for the unit tests
(tests/test_init_scripts.py); in the add-on they come from the Supervisor-controlled container
environment, which the add-on options cannot reach (WOOW_ is a reserved env_vars prefix).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shutil
import socket
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request

# ── identities (fixed; contract-tested against the Dockerfile) ───────────────
LITELLM_UID = 65534  # upstream non_root image user (nobody), same as the PaaS
LITELLM_GID = 65534
POSTGRES_UID = 70
POSTGRES_GID = 70
NGINX_UID = 101
NGINX_GID = 101

# Paths inside the add-on as seen by LiteLLM (fixed; the PaaS uses the same values).
CHATGPT_TOKEN_DIR_VALUE = "/run/woow-chatgpt"
PLUGIN_PYTHONPATH = "/usr/share/woow-litellm/py"
UI_PATH = "/var/lib/litellm/ui"

INGRESS_ENTRY_RE = re.compile(r"^/api/hassio_ingress/[A-Za-z0-9_-]{16,128}$")
MASTER_KEY_RE = re.compile(r"^sk-[A-Za-z0-9_-]{32,250}$")
WEAK_MASTER_KEYS = frozenset({"sk-1234", "sk-12345678", "sk-password", "sk-changeme", "sk-admin"})
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
DURATION_RE = re.compile(r"^([1-9]|1[0-9]|2[0-4])h$")
RETENTION_RE = re.compile(r"^[1-9][0-9]{0,3}[dh]$")
CRON_SHAPE_RE = re.compile(r"^[0-9a-z*/,-]+( [0-9a-z*/,-]+){4}$")
ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
TZ_RE = re.compile(r"^[A-Za-z0-9_+./-]{1,64}$")
SECRET_VALUE_RE = re.compile(r"^[A-Za-z0-9_-]{16,256}$")

RESERVED_ENV = frozenset(
    {
        "LITELLM_MASTER_KEY", "LITELLM_SALT_KEY", "LITELLM_MIGRATE_FROM_MASTER_KEY", "DIRECT_URL",
        "STORE_MODEL_IN_DB", "DISABLE_SCHEMA_UPDATE", "SERVER_ROOT_PATH", "SERVER_ROOT_PATHS",
        "ROOT_REDIRECT_URL", "FORWARDED_ALLOW_IPS", "LITELLM_UI_PATH", "LITELLM_NON_ROOT",
        "LITELLM_UI_SESSION_DURATION", "UI_USERNAME", "UI_PASSWORD", "PROXY_BASE_URL", "PORT", "HOST",
        "NUM_WORKERS", "LITELLM_MODE", "CHECKPOINT_DISABLE", "DOCS_URL", "NO_DOCS", "NO_REDOC",
        "NO_OPENAPI", "CHATGPT_TOKEN_DIR", "PYTHONPATH", "PATH", "HOME", "TZ",
    }
)
RESERVED_PREFIXES = (
    "DATABASE_", "LITELLM_EXPIRED_UI_SESSION_KEY_CLEANUP_", "LITELLM_PGBOUNCER_", "LITELLM_DANGEROUSLY_",
    "CHATGPT_", "WOOW_", "PRISMA_", "PYTHON", "LD_", "S6_", "SUPERVISOR_", "HASSIO_",
)
SENSITIVE_MARKERS = ("KEY", "SECRET", "PASS", "TOKEN")

DEFAULT_OPTIONS = {
    "ui_username": "admin",
    "ui_session_duration": "4h",
    "log_level": "warning",
    "spend_logs_retention": "30d",
    "spend_logs_cleanup_cron": "30 3 * * *",
    "password_breach_check": True,
    "chatgpt_subscription": True,
    "api_docs": False,
    "env_vars": [],
}

SUPERVISOR_MIN = (2026, 7, 0)
CORE_MIN = (2026, 5, 0)

SECRET_FILES = ("master_key", "salt_key", "pg_password", "ui_password")


class Fatal(Exception):
    """Refuse to start: the message goes to the log, the oneshot exits 1."""


# ── paths ─────────────────────────────────────────────────────────────────────
def _p(name: str, default: str) -> str:
    return os.environ.get(name) or default


class Paths:
    def __init__(self) -> None:
        self.data = _p("WOOW_DATA_DIR", "/data")
        self.run = _p("WOOW_RUN_DIR", "/run/woow")
        self.chatgpt = _p("WOOW_CHATGPT_DIR", "/run/woow-chatgpt")
        self.litellm_run = _p("WOOW_LITELLM_RUN_DIR", "/run/litellm")
        self.container_env = _p("WOOW_CONTAINER_ENV_DIR", "/run/s6/container_environment")
        self.share = _p("WOOW_SHARE_DIR", "/usr/share/woow-litellm")
        self.shm = _p("WOOW_SHM_DIR", "/dev/shm")
        self.options = os.path.join(self.data, "options.json")
        self.secrets = os.path.join(self.data, "secrets")
        self.postgres = os.path.join(self.data, "postgres")
        self.woow = os.path.join(self.data, "woow")
        self.dumps = os.path.join(self.data, "pre-upgrade-dumps")
        self.state = os.path.join(self.woow, "state.json")
        self.env = os.path.join(self.run, "env")
        self.help = os.path.join(self.run, "help")
        self.nginx = os.path.join(self.run, "nginx")
        self.ingress_state = os.path.join(self.run, "ingress.json")
        self.litellm_config = os.path.join(self.litellm_run, "config.yaml")

    def envdir(self, service: str) -> str:
        return os.path.join(self.env, service)


P = Paths()


def log(step: str, msg: str) -> None:
    print(f"[woow-litellm] {step}: {msg}", file=sys.stderr, flush=True)


def is_root() -> bool:
    return os.geteuid() == 0


def chown(path: str, uid: int, gid: int) -> None:
    if is_root():
        os.lchown(path, uid, gid)


# ── small file helpers ────────────────────────────────────────────────────────
def write_file(path: str, data: str, mode: int = 0o600, uid: int = 0, gid: int = 0) -> None:
    """Atomic write; the file never exists with a wider mode than requested."""
    directory = os.path.dirname(path)
    tmp = os.path.join(directory, f".{os.path.basename(path)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data.encode())
        os.fchmod(fd, mode)
    finally:
        os.close(fd)
    chown(tmp, uid, gid)
    os.replace(tmp, path)


def read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def read_secret(name: str) -> str | None:
    value = read_text(os.path.join(P.secrets, name))
    if value is None:
        return None
    value = value.strip()
    return value or None


def ensure_dir(path: str, mode: int, uid: int = 0, gid: int = 0) -> None:
    if os.path.islink(path):
        raise Fatal(f"{path} is a symbolic link; refusing to use it")
    if os.path.exists(path) and not os.path.isdir(path):
        raise Fatal(f"{path} exists and is not a directory")
    os.makedirs(path, exist_ok=True)
    os.chmod(path, mode)
    chown(path, uid, gid)


def build_info() -> dict:
    try:
        with open(os.path.join(P.share, "build-info.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def load_state() -> dict:
    text = read_text(P.state)
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise Fatal(f"{P.state} is not valid JSON ({exc}); restore it from a backup or delete it") from exc
    return data if isinstance(data, dict) else {}


def save_state(state: dict) -> None:
    write_file(P.state, json.dumps(state, indent=2, sort_keys=True) + "\n", 0o600)


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def fingerprint(purpose: str, value: str, length: int = 32) -> str:
    return hashlib.sha256(f"woow-litellm-{purpose}-v1:{value}".encode()).hexdigest()[:length]


def salt_fingerprint(salt: str) -> str:
    return fingerprint("salt", salt, 16)


def mask(key: str, value: str) -> str:
    return "******" if any(m in key.upper() for m in SENSITIVE_MARKERS) else value


# ── perms (§9.2, §7.1 init-woow-perms) ───────────────────────────────────────
def _fs_type(path: str) -> str | None:
    """The file system type of the mount that holds path (longest mount-point prefix)."""
    real = os.path.realpath(path)
    best, best_type = "", None
    try:
        with open("/proc/self/mounts", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mnt = parts[1].replace("\\040", " ")
                if (real == mnt or real.startswith(mnt.rstrip("/") + "/") or mnt == "/") and len(mnt) >= len(best):
                    best, best_type = mnt, parts[2]
    except OSError:
        return None
    return best_type


def _tmpfs_required() -> bool:
    return os.environ.get("WOOW_TMPFS_CHECK", "1") != "0"


def fix_tree(top: str, uid: int, gid: int, dir_mode: int, file_mode: int) -> int:
    """chown -hR + modes, only where something is off. Returns the number of entries fixed."""
    fixed = 0
    for root, dirs, files in os.walk(top, followlinks=False):
        for name in [root] + [os.path.join(root, n) for n in dirs + files]:
            st = os.lstat(name)
            changed = False
            if is_root() and (st.st_uid != uid or st.st_gid != gid):
                os.lchown(name, uid, gid)
                changed = True
            if not os.path.islink(name):
                want = dir_mode if os.path.isdir(name) else file_mode
                if (st.st_mode & 0o7777) != want:
                    os.chmod(name, want)
                    changed = True
            fixed += changed
    return fixed


def _tmpfs_dir(name: str, run_path: str, mode: int, uid: int, gid: int) -> str:
    """run_path must end up on a tmpfs: /run itself, or a symlink into /dev/shm."""
    parent = os.path.dirname(run_path)
    os.makedirs(parent, exist_ok=True)
    if not _tmpfs_required() or _fs_type(parent) == "tmpfs":
        if os.path.islink(run_path):
            os.unlink(run_path)
        ensure_dir(run_path, mode, uid, gid)
        return run_path
    if _fs_type(P.shm) != "tmpfs":
        raise Fatal(f"neither {parent} nor {P.shm} is a tmpfs; {run_path} must never be on disk")
    target = os.path.join(P.shm, name)
    ensure_dir(target, mode, uid, gid)
    if os.path.lexists(run_path) and not os.path.islink(run_path):
        shutil.rmtree(run_path)
    if os.path.islink(run_path):
        os.unlink(run_path)
    os.symlink(target, run_path)
    return target


def cmd_perms() -> int:
    step = "init-woow-perms"
    for path in (P.secrets, P.postgres, P.woow, P.dumps):
        if os.path.islink(path):
            raise Fatal(f"{path} is a symbolic link; refusing to start (restore /data from a backup)")
    plan = (
        (P.secrets, 0, 0, 0o700, 0o600),
        (P.woow, 0, 0, 0o700, 0o600),
        (P.postgres, POSTGRES_UID, POSTGRES_GID, 0o700, 0o600),
        (P.dumps, POSTGRES_UID, POSTGRES_GID, 0o700, 0o600),
    )
    for path, uid, gid, dmode, fmode in plan:
        ensure_dir(path, dmode, uid, gid)
        n = fix_tree(path, uid, gid, dmode, fmode)
        if n:
            log(step, f"fixed owner/mode of {n} entr{'y' if n == 1 else 'ies'} under {path}")
    run_real = _tmpfs_dir("woow", P.run, 0o711, 0, 0)
    for sub, mode in (("env", 0o700), ("nginx", 0o700), ("help", 0o755), ("failures", 0o700)):
        ensure_dir(os.path.join(P.run, sub), mode)
    chat_real = _tmpfs_dir("woow-chatgpt", P.chatgpt, 0o700, LITELLM_UID, LITELLM_GID)
    ensure_dir(P.litellm_run, 0o750, 0, LITELLM_GID)
    log(step, f"work dirs on tmpfs: {run_real}, {chat_real}")
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            avail = next((int(line.split()[1]) for line in f if line.startswith("MemAvailable:")), None)
        if avail is not None and avail < 1536 * 1024:
            log(step, f"WARNING only {avail // 1024} MiB of memory available; LiteLLM needs about 0.6–1 GiB")
    except OSError:
        pass
    return 0


# ── options (§6, §6.1) ────────────────────────────────────────────────────────
def load_options() -> dict:
    text = read_text(P.options)
    if text is None:
        raise Fatal(f"{P.options} is missing")
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise Fatal(f"{P.options} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise Fatal(f"{P.options} is not a JSON object")
    return {**DEFAULT_OPTIONS, **raw}


def _cron_error(expr: str) -> str | None:
    if not CRON_SHAPE_RE.fullmatch(expr):
        return "must be 5 space-separated cron fields"
    try:
        from apscheduler.triggers.cron import CronTrigger
    except Exception as exc:  # noqa: BLE001 — fail closed: the image ships APScheduler
        return f"cannot check it (APScheduler not importable: {type(exc).__name__})"
    try:
        CronTrigger.from_crontab(expr)
    except Exception as exc:  # noqa: BLE001
        return f"APScheduler rejects it: {exc}"
    return None


def reserved_env(key: str) -> bool:
    return key in RESERVED_ENV or key.startswith(RESERVED_PREFIXES)


def master_key_error(value: str) -> str | None:
    if not MASTER_KEY_RE.fullmatch(value):
        return "must look like sk-<32 to 250 letters, digits, _ or ->"
    if value in WEAK_MASTER_KEYS or len(set(value[3:])) < 8:
        return "is a known or trivially weak value"
    return None


def validate_options(opts: dict) -> list[str]:
    errors: list[str] = []
    master = opts.get("master_key")
    if master not in (None, ""):
        if not isinstance(master, str):
            errors.append("master_key: must be text")
        elif err := master_key_error(master):
            errors.append(f"master_key {err}")
    ui_password = opts.get("ui_password")
    if ui_password not in (None, ""):
        if not isinstance(ui_password, str) or len(ui_password) < 12 or len(ui_password) > 256:
            errors.append("ui_password: must be 12 to 256 characters")
        elif any(c in ui_password for c in "\n\r\x00"):
            errors.append("ui_password: must be a single line")
    if not isinstance(opts.get("ui_username"), str) or not USERNAME_RE.fullmatch(opts["ui_username"]):
        errors.append("ui_username: 1–64 letters, digits, _ . @ -")
    if not isinstance(opts.get("ui_session_duration"), str) or not DURATION_RE.fullmatch(opts["ui_session_duration"]):
        errors.append("ui_session_duration: 1h to 24h, e.g. 4h")
    if opts.get("log_level") not in ("error", "warning"):
        errors.append("log_level: error or warning")
    if not isinstance(opts.get("spend_logs_retention"), str) or not RETENTION_RE.fullmatch(opts["spend_logs_retention"]):
        errors.append("spend_logs_retention: e.g. 30d or 12h")
    cron = opts.get("spend_logs_cleanup_cron")
    if not isinstance(cron, str):
        errors.append("spend_logs_cleanup_cron: must be text")
    elif err := _cron_error(cron):
        errors.append(f"spend_logs_cleanup_cron {err}")
    for key in ("password_breach_check", "chatgpt_subscription", "api_docs"):
        if not isinstance(opts.get(key), bool):
            errors.append(f"{key}: must be true or false")
    env_vars = opts.get("env_vars")
    if not isinstance(env_vars, list):
        errors.append("env_vars: must be a list")
    else:
        seen = set()
        for i, item in enumerate(env_vars):
            if not isinstance(item, dict) or not isinstance(item.get("key"), str) or not isinstance(item.get("value"), str):
                errors.append(f"env_vars[{i}]: needs key and value")
                continue
            key = item["key"]
            if not ENV_KEY_RE.fullmatch(key):
                errors.append(f"env_vars[{i}]: {key!r} is not an upper-case variable name")
            elif reserved_env(key):
                errors.append(f"env_vars[{i}]: {key} is set by the add-on and cannot be overridden")
            elif key in seen:
                errors.append(f"env_vars[{i}]: {key} is listed twice")
            elif any(c in item["value"] for c in "\n\r\x00"):
                errors.append(f"env_vars[{i}]: the value of {key} must be a single line")
            seen.add(key)
    return errors


# ── env dirs (§7.1 environment principle, §7.2 table) ─────────────────────────
def upstream_env() -> dict[str, str]:
    """The upstream image's Env (litellm/upstream.lock.json → upstream.env), restated for the
    services: s6 hands no container environment to them (S6_KEEP_ENV=0)."""
    text = read_text(os.path.join(P.share, "upstream.env"))
    if text is None:
        raise Fatal(f"{P.share}/upstream.env is missing")
    env = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            env[key] = value
    return env


def write_envdir(service: str, values: dict[str, str], replace: bool = True) -> None:
    path = P.envdir(service)
    if replace and os.path.isdir(path):
        shutil.rmtree(path)
    ensure_dir(path, 0o700)
    for key, value in values.items():
        if not ENV_KEY_RE.fullmatch(key):
            raise Fatal(f"internal: bad variable name {key!r}")
        if any(c in value for c in "\n\x00"):
            raise Fatal(f"internal: {key} has a multi-line value")
        write_file(os.path.join(path, key), value, 0o600)


def base_env(opts: dict) -> dict[str, str]:
    env = dict(upstream_env())
    tz = os.environ.get("TZ", "")
    if tz and TZ_RE.fullmatch(tz):
        env["TZ"] = tz
    env.update(
        {
            "STORE_MODEL_IN_DB": "True",
            "LITELLM_MODE": "PRODUCTION",
            "NUM_WORKERS": "1",
            "GRACEFUL_SHUTDOWN_TIMEOUT": "20",
            "CHECKPOINT_DISABLE": "1",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            "LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS": "True",
            "LITELLM_LOCAL_AUTOROUTER_PRESETS": "True",
            "LITELLM_LOCAL_BLOG_POSTS": "True",
            "LITELLM_LOCAL_POLICY_TEMPLATES": "True",
            "LITELLM_DISABLE_NO_REDIS_WARNING": "true",
            "LITELLM_LOG": "ERROR" if opts["log_level"] == "error" else "WARNING",
        }
    )
    return env


def litellm_env(opts: dict) -> dict[str, str]:
    env = base_env(opts)
    env.update(
        {
            "DISABLE_SCHEMA_UPDATE": "true",
            "UI_USERNAME": opts["ui_username"],
            "LITELLM_UI_SESSION_DURATION": opts["ui_session_duration"],
            "LITELLM_UI_PATH": UI_PATH,
            "LITELLM_EXPIRED_UI_SESSION_KEY_CLEANUP_ENABLED": "true",
            "LITELLM_EXPIRED_UI_SESSION_KEY_CLEANUP_INTERVAL_SECONDS": "3600",
            "FORWARDED_ALLOW_IPS": "127.0.0.1",
            "ROOT_REDIRECT_URL": "/ui/",
        }
    )
    if opts["api_docs"]:
        env["DOCS_URL"] = "/docs"
    else:
        env.update({"NO_DOCS": "True", "NO_REDOC": "True", "NO_OPENAPI": "True"})
    if opts["chatgpt_subscription"]:
        env.update(
            {
                "PYTHONPATH": PLUGIN_PYTHONPATH,
                "CHATGPT_TOKEN_DIR": CHATGPT_TOKEN_DIR_VALUE,
                "WOOW_CHATGPT_ENABLED": "true",
                "WOOW_CHATGPT_DEFAULT_MODELS": "visible",
                "WOOW_PROXY_PORT": "4000",
            }
        )
    for item in opts["env_vars"]:
        env[item["key"]] = item["value"]
    return env


def litellm_config(opts: dict) -> str:
    q = json.dumps
    lines = [
        "# Written at every start by init-woow-env (Woow LiteLLM add-on). Do not edit: it is rebuilt.",
        "# Keys set here win over the same setting edited in the admin UI (LiteLLM >= 1.103), so only",
        "# the add-on's security and operations contract lives here (DESIGN §7.2).",
        "model_list: []",
        "general_settings:",
        "  master_key: os.environ/LITELLM_MASTER_KEY",
        "  database_url: os.environ/DATABASE_URL",
        "  store_model_in_db: true",
        f"  maximum_spend_logs_retention_period: {q(opts['spend_logs_retention'])}",
        f"  maximum_spend_logs_cleanup_cron: {q(opts['spend_logs_cleanup_cron'])}",
        '  trusted_proxy_ranges: ["127.0.0.1/32"]',
        "  database_connection_pool_limit: 10",
        "  proxy_batch_write_at: 60",
    ]
    if not opts["password_breach_check"]:
        lines.append("  password_policy_check_breached_passwords: false")
    lines += ["litellm_settings:", "  drop_params: true", "  request_timeout: 600", ""]
    return "\n".join(lines)


def cmd_env() -> int:
    step = "init-woow-env"
    opts = load_options()
    errors = validate_options(opts)
    if errors:
        for err in errors:
            log(step, f"invalid option: {err}")
        raise Fatal("fix the options in the add-on's Configuration tab and start it again")
    write_envdir("litellm", litellm_env(opts))
    migrate = base_env(opts)  # never PYTHONPATH, SERVER_ROOT_PATH, ROOT_REDIRECT_URL or WOOW_* (§7.1)
    write_envdir("litellm-migrate", migrate)
    ensure_dir(P.litellm_run, 0o750, 0, LITELLM_GID)
    write_file(P.litellm_config, litellm_config(opts), 0o640, 0, LITELLM_GID)
    shown = ", ".join(f"{i['key']}={mask(i['key'], i['value'])}" for i in opts["env_vars"]) or "none"
    log(
        step,
        f"options ok (log_level={opts['log_level']}, session={opts['ui_session_duration']}, "
        f"spend logs kept {opts['spend_logs_retention']} cleaned at '{opts['spend_logs_cleanup_cron']}' "
        f"TZ={os.environ.get('TZ') or 'UTC'}, chatgpt_subscription={opts['chatgpt_subscription']}, "
        f"api_docs={opts['api_docs']}, env_vars: {shown})",
    )
    return 0


# ── secrets (§7.4) ────────────────────────────────────────────────────────────
def _alnum(n: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def new_key() -> str:
    return "sk-" + secrets.token_hex(32)


def database_url(pg_password: str) -> str:
    return f"postgresql://litellm:{pg_password}@127.0.0.1:5432/litellm"


def cmd_secrets() -> int:
    step = "init-woow-secrets"
    opts = load_options()
    ensure_dir(P.secrets, 0o700)
    fresh_db = not os.path.exists(os.path.join(P.postgres, "PG_VERSION"))
    made = []

    salt = read_secret("salt_key")
    if salt is None:
        if not fresh_db:
            raise Fatal(
                "the database already exists but /data/secrets/salt_key is missing. Without the salt key "
                "every stored provider key is unreadable, so a new one is NEVER generated. Restore a backup "
                "that contains /data/secrets (or put the original salt key back) and start again"
            )
        salt = new_key()
        write_file(os.path.join(P.secrets, "salt_key"), salt, 0o600)
        made.append("salt key")
    elif not SECRET_VALUE_RE.fullmatch(salt):
        raise Fatal("/data/secrets/salt_key is not in the expected format; refusing to start")

    master_opt = opts.get("master_key") or ""
    master = read_secret("master_key")
    if master_opt:
        if master_opt != master:
            write_file(os.path.join(P.secrets, "master_key"), master_opt, 0o600)
            made.append("master key (from the options)" if master else "master key (from the options, first start)")
        master = master_opt
    elif master is None:
        master = new_key()
        write_file(os.path.join(P.secrets, "master_key"), master, 0o600)
        made.append("master key")
    elif master_key_error(master):
        raise Fatal("/data/secrets/master_key is not a valid master key; set master_key in the options")

    ui_opt = opts.get("ui_password") or ""
    ui_password = read_secret("ui_password")
    if ui_opt:
        if ui_opt != ui_password:
            write_file(os.path.join(P.secrets, "ui_password"), ui_opt, 0o600)
            made.append("UI password (from the options)")
        ui_password = ui_opt
    elif ui_password is None:
        ui_password = _alnum(24)
        write_file(os.path.join(P.secrets, "ui_password"), ui_password, 0o600)
        made.append("UI password")

    pg_password = read_secret("pg_password")
    if pg_password is None or not re.fullmatch(r"[A-Za-z0-9]{32,128}", pg_password):
        pg_password = _alnum(32)
        write_file(os.path.join(P.secrets, "pg_password"), pg_password, 0o600)
        made.append("database password")

    for name in SECRET_FILES:  # restore can leave other modes behind
        path = os.path.join(P.secrets, name)
        os.chmod(path, 0o600)
        chown(path, 0, 0)

    values = {
        "LITELLM_MASTER_KEY": master,
        "LITELLM_SALT_KEY": salt,
        "DATABASE_URL": database_url(pg_password),
        "UI_PASSWORD": ui_password,
    }
    for service in ("litellm", "litellm-migrate"):
        write_envdir(service, values, replace=False)
    log(step, ("generated or updated: " + ", ".join(made)) if made else "using the stored secrets")
    return 0


# ── Supervisor (§7.1 init-woow-supervisor, §7.4 write-back, §8.2, §8.3) ───────
def parse_version(value) -> tuple[int, ...] | None:
    if not isinstance(value, str):
        return None
    m = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", value)
    if not m:
        return None
    return tuple(int(x or 0) for x in m.groups())


def version_ok(value, minimum: tuple[int, ...]) -> bool:
    parsed = parse_version(value)
    return parsed is not None and parsed >= minimum


class SupervisorClient:
    def __init__(self, token: str, budget: float) -> None:
        self.base = (os.environ.get("WOOW_SUPERVISOR_URL") or "http://supervisor").rstrip("/")
        self.token = token
        self.deadline = time.monotonic() + budget

    def _request(self, method: str, path: str, body: dict | None = None, timeout: float = 10.0) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed internal URL
            payload = json.loads(resp.read().decode() or "{}")
        if payload.get("result") != "ok":
            raise RuntimeError(f"{path}: result={payload.get('result')!r}")
        return payload.get("data") or {}

    def get(self, path: str) -> dict:
        """Retry until the shared 60 s budget is spent (Supervisor may still be busy at boot)."""
        last = None
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"GET {path}: no answer within the retry budget ({last})")
            try:
                return self._request("GET", path, timeout=max(1.0, min(10.0, remaining)))
            except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
                last = type(exc).__name__ if not isinstance(exc, RuntimeError) else str(exc)
                time.sleep(min(2.0, max(0.0, self.deadline - time.monotonic())))

    def post(self, path: str, body: dict) -> dict:
        return self._request("POST", path, body, timeout=15.0)


def _writeback(step: str, client: SupervisorClient, info: dict, self_info: dict, state: dict) -> str:
    """Merge-then-write master_key / ui_password into the options when they are empty there, the
    versions are new enough and this exact value was never written back before (§7.4)."""
    sup_v, core_v = info.get("supervisor"), info.get("homeassistant")
    options = self_info.get("options")
    if not isinstance(options, dict):
        return "skipped (the Supervisor did not return the current options)"
    pending = {}
    done = state.setdefault("writeback", {})
    for name in ("master_key", "ui_password"):
        value = read_secret(name)
        if options.get(name) or value is None:
            continue
        if done.get(name) == fingerprint("writeback", value):
            continue
        pending[name] = value
    if not pending:
        return "not needed"
    if not (version_ok(sup_v, SUPERVISOR_MIN) and version_ok(core_v, CORE_MIN)):
        log(
            step,
            f"not writing the generated {' and '.join(pending)} back to the options: Supervisor {sup_v} / "
            f"Core {core_v} is older than 2026.07.0 / 2026.5.0 (older versions show options to more "
            "readers). The values stay in /data/secrets; type them into the options yourself if needed",
        )
        return "skipped (versions)"
    merged = {**options, **pending}
    try:
        client.post("/addons/self/options", {"options": merged})
    except Exception as exc:  # noqa: BLE001 — retried at the next start
        log(step, f"WARNING could not write the options back ({type(exc).__name__}); will retry at the next start")
        return "failed"
    for name, value in pending.items():
        done[name] = fingerprint("writeback", value)
    save_state(state)
    log(step, f"wrote the generated {' and '.join(pending)} into the add-on options (once; clearing them later is kept)")
    return "written: " + ", ".join(pending)


def _render(template: str, values: dict[str, str]) -> str:
    out = template
    for key, value in values.items():
        out = out.replace("@" + key + "@", value)
    if re.search(r"@[A-Z_]+@", out):
        raise Fatal("internal: unrendered placeholder in a template")
    return out


def nginx_site(mode: str, entry: str | None) -> str:
    name = "ingress.conf.tmpl" if mode == "ingress" else "help.conf.tmpl"
    template = read_text(os.path.join(P.share, "nginx", name))
    if template is None:
        raise Fatal(f"{P.share}/nginx/{name} is missing")
    if mode == "ingress":
        if not entry or not INGRESS_ENTRY_RE.fullmatch(entry):
            raise Fatal("internal: unvalidated ingress entry")
        return _render(template, {"INGRESS_ENTRY": entry})
    return _render(template, {})


def _pg_version() -> str:
    try:
        out = subprocess.run(["/usr/bin/postgres", "--version"], capture_output=True, text=True, timeout=10)
        m = re.search(r"(\d+(?:\.\d+)?)", out.stdout)
        return m.group(1) if m else "18"
    except (OSError, subprocess.SubprocessError):
        return "18"


REASONS = {
    "no-token": "這次開機沒有 Supervisor 權杖（例如在本機測試），無法取得側邊欄位址。",
    "supervisor-unreachable": "這次開機 60 秒內連不到 Supervisor，無法取得側邊欄位址。",
    "no-entry": "Supervisor 沒有回傳這個 add-on 的側邊欄位址（ingress_entry）。",
    "bad-entry": "Supervisor 回傳的側邊欄位址格式不符，為安全起見不使用。",
}


def write_help(reason: str, writeback: str) -> None:
    info = build_info()
    template = read_text(os.path.join(P.share, "help", "index.html.tmpl"))
    if template is None:
        raise Fatal(f"{P.share}/help/index.html.tmpl is missing")
    keys_note = (
        "master key 與 UI 密碼已寫回 add-on 的「設定」分頁（只有 HA 管理員看得到）。"
        if writeback.startswith("written") or writeback == "not needed"
        else "master key 與 UI 密碼沒有寫回設定分頁（版本不足或無法連線 Supervisor）；可在設定分頁自行填入新的值後重新啟動。"
    )
    values = {
        "ADDON_VERSION": html.escape(str(info.get("addon_version", "?"))),
        "LITELLM_VERSION": html.escape(str(info.get("litellm_version", "?"))),
        "LITELLM_DIGEST": html.escape(str(info.get("litellm_digest", "?"))),
        "PG_VERSION": html.escape(_pg_version()),
        "HOSTNAME": html.escape(socket.gethostname()),
        "REASON": html.escape(REASONS.get(reason, reason)),
        "KEYS_NOTE": html.escape(keys_note),
    }
    ensure_dir(P.help, 0o755)
    write_file(os.path.join(P.help, "index.html"), _render(template, values), 0o644)
    for static in ("status.js", "style.css"):
        src = os.path.join(P.share, "help", static)
        write_file(os.path.join(P.help, static), read_text(src) or "", 0o644)


def drop_supervisor_token() -> None:
    for name in ("SUPERVISOR_TOKEN", "HASSIO_TOKEN"):
        path = os.path.join(P.container_env, name)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def cmd_supervisor() -> int:
    step = "init-woow-supervisor"
    try:
        return _supervisor(step)
    finally:
        drop_supervisor_token()  # every long-running service starts after this (§7.1, S2)


def _supervisor(step: str) -> int:
    token = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN") or ""
    budget = float(os.environ.get("WOOW_SUPERVISOR_RETRY_SECONDS") or 60)
    state = load_state()
    mode, entry, reason, writeback = "help", None, "no-token", "skipped (no Supervisor)"
    versions = {}
    if token:
        client = SupervisorClient(token, budget)
        try:
            versions = client.get("/info")
            self_info = client.get("/addons/self/info")
        except TimeoutError:
            reason = "supervisor-unreachable"
            log(step, "WARNING the Supervisor did not answer within 60 s: sidebar shows the help page this time; restart the add-on to retry")
        else:
            raw = self_info.get("ingress_entry")
            if not raw:
                reason = "no-entry"
            elif isinstance(raw, str) and INGRESS_ENTRY_RE.fullmatch(raw):
                mode, entry, reason = "ingress", raw, ""
            else:
                reason = "bad-entry"
                log(step, "WARNING ingress_entry from the Supervisor has an unexpected shape; using the help page")
            writeback = _writeback(step, client, versions, self_info, state)
    else:
        log(step, "no SUPERVISOR_TOKEN (local run): no option write-back, the sidebar shows the help page")

    if mode == "ingress":
        write_envdir("litellm", {"SERVER_ROOT_PATH": entry, "ROOT_REDIRECT_URL": f"{entry}/ui/"}, replace=False)
    else:
        for name in ("SERVER_ROOT_PATH",):
            try:
                os.unlink(os.path.join(P.envdir("litellm"), name))
            except FileNotFoundError:
                pass
    ensure_dir(P.nginx, 0o700)
    write_file(os.path.join(P.nginx, "site.conf"), nginx_site(mode, entry), 0o600)
    write_help(reason or "ingress", writeback)
    write_file(
        P.ingress_state,
        json.dumps({"mode": mode, "entry": entry, "reason": reason, "writeback": writeback}) + "\n",
        0o600,
    )
    info = build_info()
    log(
        step,
        f"add-on {info.get('addon_version', '?')}, LiteLLM {info.get('litellm_version', '?')} "
        f"({info.get('litellm_digest', '?')}), Woow plugin {info.get('woow_plugin_source', '?')}, "
        f"PostgreSQL {_pg_version()}; API for other add-ons: http://{socket.gethostname()}:4000; "
        f"sidebar: {'LiteLLM admin UI' if mode == 'ingress' else 'help page (' + reason + ')'}; "
        f"Supervisor {versions.get('supervisor', '?')} / Core {versions.get('homeassistant', '?')}; "
        f"option write-back: {writeback}",
    )
    return 0


# ── database statements (§7.1 init-db) ───────────────────────────────────────
def scram_verifier(password: str, salt: bytes | None = None, iterations: int = 4096) -> str:
    salt = salt if salt is not None else os.urandom(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


VERIFIER_RE = re.compile(r"^SCRAM-SHA-256\$4096:[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+$")


def cmd_scram_sql() -> int:
    password = read_secret("pg_password")
    if not password:
        raise Fatal("/data/secrets/pg_password is missing")
    verifier = scram_verifier(password)
    if not VERIFIER_RE.fullmatch(verifier):
        raise Fatal("internal: bad SCRAM verifier")
    sys.stdout.write(
        "SET log_min_error_statement = panic;\n"
        f"ALTER ROLE litellm WITH LOGIN PASSWORD '{verifier}';\n"
    )
    return 0


def _salt() -> str:
    salt = read_secret("salt_key")
    if not salt or not SECRET_VALUE_RE.fullmatch(salt):
        raise Fatal("/data/secrets/salt_key is missing or malformed")
    return salt


def cmd_meta_sql() -> int:
    fp = salt_fingerprint(_salt())
    sys.stdout.write(
        "SET client_min_messages = warning;\n"
        "CREATE SCHEMA IF NOT EXISTS woow AUTHORIZATION postgres;\n"
        "REVOKE ALL ON SCHEMA woow FROM PUBLIC;\n"
        "CREATE TABLE IF NOT EXISTS woow.meta (\n"
        "  key        text PRIMARY KEY,\n"
        "  value      text NOT NULL,\n"
        "  updated_at timestamptz NOT NULL DEFAULT now()\n"
        ");\n"
        "REVOKE ALL ON woow.meta FROM PUBLIC;\n"
        f"INSERT INTO woow.meta (key, value) VALUES ('salt_fingerprint_v1', '{fp}') ON CONFLICT (key) DO NOTHING;\n"
        f"SELECT CASE WHEN value = '{fp}' THEN 'same' ELSE 'different' END FROM woow.meta WHERE key = 'salt_fingerprint_v1';\n"
    )
    return 0


# Same schema, tables and columns as woow-paas-charts charts/litellm db-prepare.sh (chart 0.3.0).
PLATFORM_DDL = """SET client_min_messages = warning;
SET log_min_error_statement = panic;
SET ROLE litellm;
CREATE SCHEMA IF NOT EXISTS woow_platform;
CREATE TABLE IF NOT EXISTS woow_platform.settings (
  key        text PRIMARY KEY,
  value      text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE woow_platform.settings IS 'Woow LiteLLM add-on state (same table as the WOOW PaaS litellm chart). litellm_salt_key = LITELLM_SALT_KEY: losing it makes every stored provider key unreadable; /data/secrets/salt_key is authoritative.';
CREATE TABLE IF NOT EXISTS woow_platform.chatgpt_auth (
  id         smallint PRIMARY KEY CHECK (id = 1),
  auth       jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE woow_platform.chatgpt_auth IS 'Woow LiteLLM add-on (same as the WOOW PaaS litellm chart): the default ChatGPT subscription sign-in, restored to tmpfs at every start. Delete the row to sign out.';
CREATE TABLE IF NOT EXISTS woow_platform.chatgpt_accounts (
  account    text PRIMARY KEY,
  auth       jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE woow_platform.chatgpt_accounts IS 'Woow LiteLLM add-on (same as the WOOW PaaS litellm chart 0.2.3+): one ChatGPT subscription sign-in per account, restored to tmpfs at every start. The default account is mirrored to chatgpt_auth.';
"""


def cmd_platform_sql() -> int:
    salt = _salt()
    sys.stdout.write(
        PLATFORM_DDL
        + f"INSERT INTO woow_platform.settings (key, value) VALUES ('litellm_salt_key', '{salt}') ON CONFLICT (key) DO NOTHING;\n"
        + "SELECT CASE WHEN value = '' THEN 'empty' "
        + f"WHEN value = '{salt}' THEN 'same' ELSE 'different' END "
        + "FROM woow_platform.settings WHERE key = 'litellm_salt_key';\n"
        + "RESET ROLE;\n"
    )
    return 0


# ── state (§9.1) ──────────────────────────────────────────────────────────────
def cmd_state_get(key: str) -> int:
    value = load_state().get(key)
    if isinstance(value, (str, int, float)):
        print(value)
    return 0


def cmd_state_migrated() -> int:
    state = load_state()
    info = build_info()
    state.setdefault("schema", 1)
    state.setdefault("first_init_at", utcnow())
    state["salt_fingerprint_version"] = 1
    state["litellm_version"] = info.get("litellm_version")
    state["addon_version"] = info.get("addon_version")
    state["migrated_at"] = utcnow()
    save_state(state)
    return 0


def _revoke_fps() -> dict[str, str]:
    out = {}
    for name in ("master_key", "ui_password"):
        value = read_secret(name)
        if value is None:
            raise Fatal(f"/data/secrets/{name} is missing")
        out[name] = fingerprint("revoke", value)
    return out


def cmd_revoke_check() -> int:
    stored = load_state().get("revoke")
    current = _revoke_fps()
    if not isinstance(stored, dict) or not stored:
        print("fresh")
    elif all(stored.get(k) == v for k, v in current.items()):
        print("unchanged")
    else:
        print("changed:" + ",".join(k for k, v in current.items() if stored.get(k) != v))
    return 0


def cmd_revoke_record() -> int:
    state = load_state()
    state["revoke"] = _revoke_fps()
    save_state(state)
    return 0


COMMANDS = {
    "perms": cmd_perms,
    "env": cmd_env,
    "secrets": cmd_secrets,
    "supervisor": cmd_supervisor,
    "scram-sql": cmd_scram_sql,
    "meta-sql": cmd_meta_sql,
    "platform-sql": cmd_platform_sql,
    "state-migrated": cmd_state_migrated,
    "revoke-check": cmd_revoke_check,
    "revoke-record": cmd_revoke_record,
}


def main(argv: list[str]) -> int:
    os.umask(0o077)
    if len(argv) == 2 and argv[0] == "state-get":
        return cmd_state_get(argv[1])
    if len(argv) != 1 or argv[0] not in COMMANDS:
        print(f"usage: woow-init {{{'|'.join(sorted(COMMANDS))}|state-get KEY}}", file=sys.stderr)
        return 2
    try:
        return COMMANDS[argv[0]]()
    except Fatal as exc:
        log(argv[0], f"FATAL {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
