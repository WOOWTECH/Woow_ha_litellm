"""Lean tests for the request-guard pure functions (no LiteLLM installed)."""
import importlib.util
import logging
from pathlib import Path
import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('woow_request_guard', REPO / 'litellm/patches/woow_request_guard.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

ENVS = ('WOOW_TOKEN_LIMIT_VALIDATION', 'WOOW_BUDGET_ADMISSION', 'WOOW_BUDGET_UNPRICED',
        'WOOW_BUDGET_REASONING_RESERVE_TOKENS', 'WOOW_BUDGET_MIN_INPUT_TOKENS')
REASONING = {'supports_reasoning': True}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for env in ENVS:
        monkeypatch.delenv(env, raising=False)


@pytest.mark.parametrize('value,problem', [
    (None, None), (1, None), (16, None), (16.0, None), (2**31 - 1, None), (50_000_000, None),
    (True, "Invalid type for 'max_tokens': expected an integer, but got a boolean instead."),
    (False, "Invalid type for 'max_tokens': expected an integer, but got a boolean instead."),
    (1.5, "Invalid type for 'max_tokens': expected an integer, but got a decimal number instead."),
    (float('nan'), "Invalid type for 'max_tokens': expected an integer, but got a decimal number instead."),
    (float('inf'), "Invalid type for 'max_tokens': expected an integer, but got a decimal number instead."),
    ('10', "Invalid type for 'max_tokens': expected an integer, but got a string instead."),
    ([5], "Invalid type for 'max_tokens': expected an integer, but got an array instead."),
    ({}, "Invalid type for 'max_tokens': expected an integer, but got an object instead."),
    (0, "Invalid 'max_tokens': integer below minimum value. Expected a value >= 1, but got 0 instead."),
    (-1.0, "Invalid 'max_tokens': integer below minimum value. Expected a value >= 1, but got -1 instead."),
    (2**31, "Invalid 'max_tokens': integer above maximum value. Expected a value <= 2147483647, but got 2147483648 instead."),
    (2**63, "Invalid 'max_tokens': integer above maximum value. Expected a value <= 2147483647, "
            "but got 9223372036854775808 instead."),
])
def test_token_limit_problem(value, problem):
    assert guard.token_limit_problem('max_tokens', value) == problem


def test_route_fields():
    assert guard.TOKEN_LIMIT_PARAMS == {
        'acompletion': ('max_tokens', 'max_completion_tokens'), 'atext_completion': ('max_tokens',),
        'aresponses': ('max_output_tokens',), 'anthropic_messages': ('max_tokens',)}


def test_validate_skips_other_routes_and_valid_bodies():
    guard.validate_token_limits('aembedding', {'max_tokens': -1})
    guard.validate_token_limits('acompletion', {'max_tokens': 16, 'max_completion_tokens': 16.0})


@pytest.mark.parametrize('mode', ['warn', 'off'])
def test_warn_and_off_never_raise(monkeypatch, caplog, mode):
    monkeypatch.setenv('WOOW_TOKEN_LIMIT_VALIDATION', mode)
    caplog.set_level(logging.WARNING, logger='LiteLLM')
    guard.validate_token_limits('acompletion', {'max_tokens': -1, 'messages': [{'content': 'PROMPT-SENTINEL'}]})
    logged = [r.getMessage() for r in caplog.records if 'output token limit' in r.getMessage()]
    assert len(logged) == (mode == 'warn') and all('PROMPT-SENTINEL' not in m and 'type=int' in m for m in logged)


@pytest.mark.parametrize('value,count', [(None, None), (True, None), (False, None), (0, None), (-1, None),
                                         (16, 16), ('16', 16), (16.7, 16), ('x', None), ([1], None)])
def test_positive_token_count(value, count):
    assert guard.positive_token_count(value) == count


def test_reserve_output_tokens():
    assert guard.reserve_output_tokens({}, REASONING, 16, 100_000) == 2048
    assert guard.reserve_output_tokens({}, REASONING, 4000, 100_000) == 4000
    assert guard.reserve_output_tokens({}, REASONING, 16, 1000) == 1000  # clamped by the model ceiling
    assert guard.reserve_output_tokens({}, {}, 16, 100_000) == 16
    assert guard.reserve_output_tokens({}, {'woow_max_tokens_enforced': False}, 16, 100_000) == 2048
    assert guard.reserve_output_tokens({}, dict(REASONING, woow_reasoning_output_reserve_tokens=512), 16, 100_000) == 512
    assert guard.reserve_output_tokens({}, REASONING, None, 100_000) is None


@pytest.mark.parametrize('body', [{'reasoning': {'enabled': False}}, {'reasoning': {'effort': 'none'}},
                                  {'reasoning_effort': 'none'}])
def test_reasoning_disabled_body_gets_no_reserve(body):
    assert guard.reserve_output_tokens(body, REASONING, 16, 100_000) == 16


@pytest.mark.parametrize('body', [{'reasoning': {'enabled': True}}, {'reasoning_effort': 'low'}, {'reasoning': 'off'}])
def test_reasoning_not_disabled(body):
    assert guard.reserve_output_tokens(body, REASONING, 16, 100_000) == 2048


def test_env_reserve_and_invalid_values(monkeypatch, caplog):
    monkeypatch.setenv('WOOW_BUDGET_REASONING_RESERVE_TOKENS', '4096')
    assert guard.reserve_output_tokens({}, REASONING, 16, 100_000) == 4096
    monkeypatch.setenv('WOOW_BUDGET_REASONING_RESERVE_TOKENS', 'lots')
    caplog.set_level(logging.WARNING, logger='LiteLLM')
    assert guard.reserve_output_tokens({}, REASONING, 16, 100_000) == 2048
    assert any('WOOW_BUDGET_REASONING_RESERVE_TOKENS' in r.getMessage() for r in caplog.records)


def test_reserve_input_tokens(monkeypatch):
    assert guard.reserve_input_tokens(REASONING, 15) == 256
    assert guard.reserve_input_tokens(REASONING, 900) == 900
    assert guard.reserve_input_tokens({}, 15) == 15
    assert guard.reserve_input_tokens(dict(REASONING, woow_min_input_tokens=64), 15) == 64
    monkeypatch.setenv('WOOW_BUDGET_MIN_INPUT_TOKENS', '100')
    assert guard.reserve_input_tokens(REASONING, 15) == 100


@pytest.mark.parametrize('env,read,values', [
    ('WOOW_BUDGET_ADMISSION', 'budget_admission_mode', ('strict', 'resize')),
    ('WOOW_BUDGET_UNPRICED', 'unpriced_policy', ('allow', 'block_budgeted_keys')),
    ('WOOW_TOKEN_LIMIT_VALIDATION', 'token_limit_mode', ('enforce', 'warn', 'off')),
])
def test_modes_default_and_invalid(monkeypatch, caplog, env, read, values):
    assert getattr(guard, read)() == values[0]
    for value in values:
        monkeypatch.setenv(env, ' ' + value.upper() + ' ')
        assert getattr(guard, read)() == value
    caplog.set_level(logging.WARNING, logger='LiteLLM')
    monkeypatch.setenv(env, 'bogus')
    assert getattr(guard, read)() == values[0]
    assert any(env in r.getMessage() for r in caplog.records)


def test_deployment_overrides_take_the_largest_value():
    merged = guard.with_deployment_overrides({'supports_reasoning': True, 'input_cost_per_token': 1.0}, [
        {'model_info': {'woow_reasoning_output_reserve_tokens': 512}},
        {'model_info': {'woow_reasoning_output_reserve_tokens': 1024, 'woow_min_input_tokens': 'bad'}},
        {'model_info': {'woow_max_tokens_enforced': False}}, {'model_info': None}, 'not-a-deployment',
    ])
    assert merged == {'supports_reasoning': True, 'input_cost_per_token': 1.0,
                      'woow_reasoning_output_reserve_tokens': 1024, 'woow_max_tokens_enforced': False}


def test_unpriced_allow_logs_only(caplog):
    caplog.set_level(logging.WARNING, logger='LiteLLM')
    guard.check_unpriced_budgeted_request('vendor/model')
    assert [r.getMessage() for r in caplog.records] == ['Woow event=unpriced_budgeted_request model=vendor/model']
