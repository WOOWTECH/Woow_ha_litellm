"""The Woow plugin modules take their settings from the environment; the add-on's startup environment
(woow_init.litellm_env) must say, on purpose, what the PaaS chart says -- except the two documented HA
decisions (request body cap 64 MB like the Ingress, dashboard cookie hardening off on plain http).

Runs without LiteLLM: the modules only import it lazily. A plugin default that changes at the next
sync fails here instead of silently changing the add-on.
"""
import importlib.util
import sys

import pytest

from conftest import DEFAULT_OPTIONS, INIT_PY, SHARE

PY = SHARE / "py"
HA_DECISIONS = {"WOOW_MAX_REQUEST_SIZE_MB": "64", "WOOW_UI_TOKEN_HTTPONLY": "false"}
ENV_NAMES = (
    "WOOW_REASONING_MIN_OUTPUT_MODE", "WOOW_REASONING_MIN_OUTPUT_TOKENS", "WOOW_REASONING_MODEL_PATTERNS",
    "WOOW_UNPRICED_MODEL_MODE", "WOOW_FORWARD_RETRY_AFTER", "WOOW_UPSTREAM_TIMEOUT_STATUS",
    "WOOW_MAX_REQUEST_SIZE_MB", "WOOW_UI_TOKEN_HTTPONLY", "WOOW_USAGE_LIMIT_COOLDOWN_MAX_SECONDS",
)


def load(name):
    spec = importlib.util.spec_from_file_location("ha_env_" + name, PY / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    keep, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # the plugin directory must stay clean
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = keep
    return module


@pytest.fixture
def clean_env(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def addon_env(box):
    box.boot()
    return box.envdir("litellm")


def test_written_values_equal_the_plugin_defaults(box, clean_env):
    env = addon_env(box)
    reasoning, unpriced = load("woow_reasoning"), load("woow_unpriced")
    upstream = load("woow_upstream_errors")
    assert reasoning.settings() == (env["WOOW_REASONING_MIN_OUTPUT_MODE"], int(env["WOOW_REASONING_MIN_OUTPUT_TOKENS"]))
    assert unpriced.mode() == env["WOOW_UNPRICED_MODEL_MODE"]
    assert upstream.forward_enabled() is (env["WOOW_FORWARD_RETRY_AFTER"] == "true")
    assert upstream.timeout_status() == int(env["WOOW_UPSTREAM_TIMEOUT_STATUS"])


def test_usage_limit_cooldown_is_the_paas_default_and_the_plugin_reads_it(box, clean_env, monkeypatch):
    """0.3.14: unset -> the plugin's own default is 900 s, the add-on writes the same 900 (= the PaaS chart)."""
    env = addon_env(box)
    module = load("woow_usage_limit")
    assert module.DEFAULT_MAX_SECONDS == 900 and module.max_seconds() == 900  # nothing set: the default
    assert env["WOOW_USAGE_LIMIT_COOLDOWN_MAX_SECONDS"] == "900"
    monkeypatch.setenv(module.MAX_ENV, env["WOOW_USAGE_LIMIT_COOLDOWN_MAX_SECONDS"])
    assert module.max_seconds() == 900
    assert module.MAX_ENV == "WOOW_USAGE_LIMIT_COOLDOWN_MAX_SECONDS"


@pytest.mark.parametrize("value,seconds", [("0", 0), ("61", 61), ("1800", 1800), ("86400", 86400)])
def test_usage_limit_switch_values_the_module_reads(box, clean_env, monkeypatch, value, seconds):
    box.options(DEFAULT_OPTIONS, env_vars=[{"key": "WOOW_USAGE_LIMIT_COOLDOWN_MAX_SECONDS", "value": value}])
    assert addon_env(box)["WOOW_USAGE_LIMIT_COOLDOWN_MAX_SECONDS"] == value
    module = load("woow_usage_limit")
    monkeypatch.setenv(module.MAX_ENV, value)
    assert module.max_seconds() == seconds


def test_the_addon_sets_no_router_settings_so_the_cooldown_header_decides(box):
    """The 0.3.14 cooldown works through LiteLLM's own Router defaults: a 429 cools a deployment down at
    once when no allowed_fails / allowed_fails_policy / cooldown_time is configured, and the Retry-After
    header then sets how long. The add-on's config must therefore not set any of them (the real Router
    behaviour is pinned in tests/api_compat/test_woow_plugin_composition.py)."""
    box.boot()
    text = (box.run_dir / "litellm" / "config.yaml").read_text()
    for name in ("router_settings", "allowed_fails", "cooldown_time", "num_retries", "disable_cooldowns", "retry_policy"):
        assert name not in text, name


def test_ha_decisions_differ_from_the_plugin_defaults_on_purpose(box, clean_env):
    env = addon_env(box)
    assert {k: env[k] for k in HA_DECISIONS} == HA_DECISIONS
    assert load("woow_body_limit").limit_from_env() == 32 * 1024 * 1024  # plugin default; HA writes 64
    # Ingress `client_max_body_size` and the body cap say the same number.
    ingress = (SHARE / "nginx/ingress.conf.tmpl").read_text()
    assert f"client_max_body_size {HA_DECISIONS['WOOW_MAX_REQUEST_SIZE_MB']}m;" in ingress
    # sitecustomize turns the cookie hardening on unless the variable says false-like.
    site = (PY / "sitecustomize.py").read_text()
    assert 'os.environ.get("WOOW_UI_TOKEN_HTTPONLY", "true")' in site


def test_body_cap_reads_the_written_value(box, clean_env, monkeypatch):
    env = addon_env(box)
    monkeypatch.setenv("WOOW_MAX_REQUEST_SIZE_MB", env["WOOW_MAX_REQUEST_SIZE_MB"])
    assert load("woow_body_limit").limit_from_env() == 64 * 1024 * 1024


@pytest.mark.parametrize("value", ["0", "0.5", "64", "1024", "9999"])
def test_body_cap_switch_values_the_module_reads(box, clean_env, monkeypatch, value):
    box.options(DEFAULT_OPTIONS, env_vars=[{"key": "WOOW_MAX_REQUEST_SIZE_MB", "value": value}])
    assert addon_env(box)["WOOW_MAX_REQUEST_SIZE_MB"] == value
    monkeypatch.setenv("WOOW_MAX_REQUEST_SIZE_MB", value)
    limit = load("woow_body_limit").limit_from_env()
    assert limit is None if float(value) == 0 else limit == int(float(value) * 1024 * 1024)


def test_every_plugin_setting_has_a_checked_switch():
    """Each WOOW_* variable the synced modules read is either set by the add-on and switchable, or the
    PaaS/HA-only exception listed here (an unlisted new variable needs a decision at the next sync)."""
    import re
    init = INIT_PY.read_text()
    used = set()
    for path in PY.glob("*.py"):
        used |= set(re.findall(r'\bWOOW_[A-Z][A-Z0-9_]+\b', path.read_text()))
    # Not settings: set by the add-on elsewhere, read for the account/UI plugin, or this add-on's own.
    not_settings = {
        "WOOW_CHATGPT_ENABLED", "WOOW_CHATGPT_DEFAULT_MODELS", "WOOW_PROXY_PORT", "WOOW_CHATGPT_UI_BRIDGE",
        "WOOW_CODEX_CLIENT_VERSION",
        # PaaS only: read by woow_revoke_ui_sessions.py (not synced) and the cookie hardening's session policy.
        "WOOW_UI_SESSION_POLICY_VERSION",
        # Optional, no default written: extra provider-model regexes for the reasoning floor.
        "WOOW_REASONING_MODEL_PATTERNS",
    }
    unknown = sorted(name for name in used - not_settings if f'"{name}"' not in init)
    assert not unknown, f"plugin variables the add-on neither writes nor lets an operator switch: {unknown}"
