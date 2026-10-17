"""AI-FIX-LLM-2-2: the add-on ships the Woow plugin's reasoning output floor (woow_reasoning.py).

A reasoning route given a small max_tokens answers 200 with an empty content and
finish_reason "length" (AI-LLM-003-1: gpt-6-luna, max_tokens=16, 4 of 4). The canonical fix is
woow-paas-charts charts/litellm/files/woow_reasoning.py (chart 0.3.7), hooked in by
woow_chatgpt.py; its behaviour is tested there (tests/test_m4_reasoning_floor.py). Here: the
copy is present, wired into the copied woow_chatgpt.py, and does the right thing with the
add-on's settings (no WOOW_REASONING_* in the env unless set through env_vars -> 512, raise).
"""

import importlib
import sys
import types

import pytest

from test_config_contract import PLUGIN_LOCK, REPO

PY = REPO / PLUGIN_LOCK["destination"]
SETTINGS = ("WOOW_REASONING_MIN_OUTPUT_TOKENS", "WOOW_REASONING_MIN_OUTPUT_MODE", "WOOW_REASONING_MODEL_PATTERNS")


@pytest.fixture
def reasoning(monkeypatch):
    for var in SETTINGS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)  # no __pycache__ next to the locked copies
    monkeypatch.syspath_prepend(str(PY))
    monkeypatch.delitem(sys.modules, "woow_reasoning", raising=False)
    module = importlib.import_module("woow_reasoning")
    proxy = types.ModuleType("litellm.proxy.proxy_server")
    proxy.llm_router = types.SimpleNamespace(model_list=[
        {"model_name": "qa-reasoning", "litellm_params": {"model": "openai/openai/gpt-6-luna"}, "model_info": {}},
        {"model_name": "qa-cheap", "litellm_params": {"model": "openai/xiaomi/mimo-v2.6-flash"}, "model_info": {}},
        {"model_name": "chatgpt-astra", "litellm_params": {"model": "chatgpt/gpt-6-astra"}, "model_info": {}},
    ])
    monkeypatch.setitem(sys.modules, module.PROXY_TARGET, proxy)
    yield module
    sys.modules.pop("woow_reasoning", None)


def test_copy_is_wired_into_the_copied_core():
    core = (PY / "woow_chatgpt.py").read_text()
    assert "import woow_reasoning" in core
    assert "reasoning.pre_call(data)" in core and "reasoning.post_call(" in core


def test_add_on_defaults_raise_to_512(reasoning):
    assert reasoning.settings() == ("raise", 512)


@pytest.mark.parametrize("model", ["qa-reasoning", "chatgpt-astra"])
def test_small_limit_on_a_reasoning_route_is_raised(reasoning, model):
    body = {"model": model, "messages": [], "max_tokens": 16}
    reasoning.pre_call(body)
    assert body["max_tokens"] == 512


def test_non_reasoning_route_and_reasoning_off_are_untouched(reasoning):
    cheap = {"model": "qa-cheap", "messages": [], "max_tokens": 16}
    off = {"model": "qa-reasoning", "messages": [], "max_tokens": 16, "reasoning_effort": "none"}
    reasoning.pre_call(cheap)
    reasoning.pre_call(off)
    assert cheap["max_tokens"] == 16 and off["max_tokens"] == 16


@pytest.mark.parametrize("env", [{"WOOW_REASONING_MIN_OUTPUT_MODE": "off"}, {"WOOW_REASONING_MIN_OUTPUT_TOKENS": "0"}])
def test_env_vars_rollback_switch(reasoning, monkeypatch, env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    body = {"model": "qa-reasoning", "messages": [], "max_tokens": 16}
    reasoning.pre_call(body)
    assert body["max_tokens"] == 16
