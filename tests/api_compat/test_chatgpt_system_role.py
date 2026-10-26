"""Offline contracts against the real ChatGPT provider and Anthropic bridge.

No live account, routing configuration, or production runtime is used.
"""
from copy import deepcopy
import asyncio
import json
import signal
import socket

import httpx
import litellm
import pytest
from litellm.llms.chatgpt.authenticator import Authenticator
from litellm.llms.chatgpt.common_utils import get_chatgpt_default_instructions
from litellm.llms.chatgpt.responses.transformation import ChatGPTResponsesAPIConfig
from litellm.llms.openai.responses.transformation import OpenAIResponsesAPIConfig
from litellm.types.router import GenericLiteLLMParams


@pytest.fixture(autouse=True)
def bounded_offline_case(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Role regression forbids DNS/network/auth-file access")

    def deadline(*args):
        raise TimeoutError("Role regression exceeded the 20-second case limit")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(Authenticator, "_read_auth_file", forbidden)
    previous = signal.signal(signal.SIGALRM, deadline)
    signal.setitimer(signal.ITIMER_REAL, 20)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@pytest.fixture
def provider(monkeypatch, tmp_path):
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(tmp_path / "unused-auth"))
    monkeypatch.setattr(litellm, "disable_aiohttp_transport", True)
    return ChatGPTResponsesAPIConfig()


def transform(provider, items, **options):
    return provider.transform_responses_api_request(
        model="gpt-6-luna", input=items,
        response_api_optional_request_params=options,
        litellm_params=GenericLiteLLMParams(), headers={},
    )


def test_system_input_is_serialized_as_developer_without_changing_caller(provider):
    items = [
        {"role": "system", "content": "Keep the configured rules."},
        {"role": "user", "content": "Hello"},
    ]
    original = deepcopy(items)
    request = transform(provider, items)
    assert request["input"] == [
        {"role": "developer", "content": "Keep the configured rules."},
        {"role": "user", "content": "Hello"},
    ], "ChatGPT rejects system input with 400: System messages are not allowed"
    assert items == original


@pytest.mark.parametrize("content", ["", [], [{"type": "input_text", "text": ""}],
                                      [{"type": "input_text", "text": "First"},
                                       {"type": "input_text", "text": "Second"}]])
def test_empty_and_multiple_system_content_blocks_are_retained(provider, content):
    items = [{"type": "message", "role": "system", "content": content}]
    original = deepcopy(items)
    assert transform(provider, items)["input"] == [
        {"type": "message", "role": "developer", "content": content}
    ]
    assert items == original


REPLAY = [
    {"role": "developer", "content": "Existing developer rule"},
    {"role": "user", "content": [{"type": "input_text", "text": "Use the tool"}]},
    {"type": "reasoning", "id": "rs_fixture", "summary": [],
     "encrypted_content": "synthetic-encrypted-reasoning"},
    {"type": "function_call", "id": "fc_fixture", "call_id": "call_fixture",
     "name": "lookup", "arguments": '{"q":"offline"}'},
    {"type": "function_call_output", "call_id": "call_fixture", "output": "tool result"},
    {"type": "message", "role": "assistant", "content": [
        {"type": "output_text", "text": "Earlier answer", "annotations": []}]},
    {"role": "user", "content": "Continue"},
]


def test_mixed_history_preserves_order_tools_developer_and_existing_instructions(provider):
    items = deepcopy(REPLAY)
    items.insert(0, {"role": "system", "content": "First rule"})
    items.insert(6, {"role": "system", "content": "Mid-turn rule"})
    original = deepcopy(items)
    expected = deepcopy(items)
    expected[0]["role"] = expected[6]["role"] = "developer"
    base = get_chatgpt_default_instructions()
    instructions = base + "\n\nKeep this explicit instruction unchanged."
    request = transform(provider, items, instructions=instructions)
    assert request["input"] == expected
    assert request["instructions"] == instructions
    assert items == original


def test_native_responses_shapes_are_unchanged(provider):
    options = {"instructions": get_chatgpt_default_instructions() + "\n\nNative CLI rule",
               "tools": [{"type": "function", "name": "lookup", "parameters": {
                   "type": "object", "properties": {"q": {"type": "string"}}}}],
               "tool_choice": "auto", "reasoning": {"effort": "low"},
               "previous_response_id": "resp_previous_fixture",
               "include": ["reasoning.encrypted_content"], "store": False, "stream": True}
    original = deepcopy(options)
    request = transform(provider, deepcopy(REPLAY), **options)
    assert request == {"model": "gpt-6-luna", "input": REPLAY, **original}


def test_plain_string_remains_user_input(provider):
    assert transform(provider, "Hello")["input"] == [{"role": "user", "content": "Hello"}]


def test_generic_openai_system_role_is_not_changed():
    items = [{"role": "system", "content": "OpenAI system rule"}]
    request = transform(OpenAIResponsesAPIConfig(), deepcopy(items))
    assert request["input"] == items


def response_events():
    item = {"type": "message", "id": "msg_fixture", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": "OK", "annotations": []}]}
    response = {"id": "resp_fixture", "object": "response", "created_at": 1,
                "status": "completed", "model": "gpt-6-luna", "output": [item],
                "usage": {"input_tokens": 8, "output_tokens": 1, "total_tokens": 9,
                          "input_tokens_details": {"cached_tokens": 0},
                          "output_tokens_details": {"reasoning_tokens": 0}}}
    return [
        {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
        {"type": "response.output_item.added", "output_index": 0,
         "item": {**item, "status": "in_progress", "content": []}},
        {"type": "response.content_part.added", "output_index": 0, "content_index": 0,
         "item_id": "msg_fixture", "part": {"type": "output_text", "text": "", "annotations": []}},
        {"type": "response.output_text.delta", "output_index": 0, "content_index": 0,
         "item_id": "msg_fixture", "delta": "OK"},
        {"type": "response.output_text.done", "output_index": 0, "content_index": 0,
         "item_id": "msg_fixture", "text": "OK"},
        {"type": "response.content_part.done", "output_index": 0, "content_index": 0,
         "item_id": "msg_fixture", "part": item["content"][0]},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": response},
    ]


@pytest.fixture
def wire(monkeypatch, tmp_path):
    """Replace only external auth/HTTP; all request/response adapters remain real."""
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(tmp_path / "unused-auth"))
    # Some bridge paths resolve their base from the authenticator instead of the
    # outer api_base argument. Both paths must stay on the synthetic host.
    monkeypatch.setenv("CHATGPT_API_BASE", "https://offline.invalid/backend-api/codex")
    monkeypatch.setattr(litellm, "disable_aiohttp_transport", True)
    monkeypatch.setattr(Authenticator, "get_access_token", lambda self: "fixture-access-token")
    monkeypatch.setattr(Authenticator, "get_account_id", lambda self: "fixture-account")
    seen = []

    def upstream_response(request):
        assert request.url.host == "offline.invalid"
        assert request.url.path.endswith("/responses")
        body = json.loads(request.content)
        seen.append(body)
        # Reproduce the observed backend rejection at the actual outgoing HTTP boundary.
        if any(item.get("role") == "system" for item in body["input"]):
            return httpx.Response(400, json={"detail": "System messages are not allowed"}, request=request)
        events = response_events()
        data = "".join("data: " + json.dumps({**event, "sequence_number": i}) + "\n\n"
                       for i, event in enumerate(events))
        return httpx.Response(200, text=data, headers={"content-type": "text/event-stream"}, request=request)

    async def upstream(self, request):
        return upstream_response(request)

    def sync_upstream(self, request):
        return upstream_response(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", upstream)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", sync_upstream)
    return seen


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("system", ["Keep the Claude Code rules.",
                                   [{"type": "text", "text": "First Claude rule."},
                                    {"type": "text", "text": "Second Claude rule."}]])
def test_real_anthropic_messages_bridge_to_chatgpt_wire(wire, stream, system):
    from litellm.llms.anthropic.experimental_pass_through.messages.handler import anthropic_messages

    async def run():
        result = await anthropic_messages(
            # This model is marked Responses-native in the pinned 1.104.0 offline
            # registry. No fetched model map or fake routing function is needed.
            model="chatgpt/gpt-5.4", max_tokens=32, system=deepcopy(system),
            messages=[{"role": "user", "content": "Hello"}], stream=stream,
            num_retries=0, api_base="https://offline.invalid/backend-api/codex",
        )
        if stream:
            chunks = [chunk async for chunk in result]
            assert "OK" in str(chunks)
        else:
            content = result["content"] if isinstance(result, dict) else result.content
            assert any((c.get("text") if isinstance(c, dict) else c.text) == "OK" for c in content)

    asyncio.run(asyncio.wait_for(run(), timeout=15))
    assert len(wire) == 1  # no retry or alternate endpoint bypass
    body = wire[0]
    assert body["stream"] is True and body["store"] is False
    assert "system" not in [item.get("role") for item in body["input"]]
    developers = [item for item in body["input"] if item.get("role") == "developer"]
    if isinstance(system, str):
        # The native bridge already puts plain system text in instructions.
        assert body["instructions"] == get_chatgpt_default_instructions() + "\n\n" + system
    else:
        assert developers, "The bridge's system blocks must remain input instructions"
        text = json.dumps(developers, ensure_ascii=False)
        for expected in [x["text"] for x in system]:
            assert expected in text
    assert body["input"][-1] == {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Hello"}]}


@pytest.mark.parametrize("client", ["codex", "pi"])
def test_native_codex_pi_responses_wire_preserves_input(wire, client):
    items = deepcopy(REPLAY)
    if client == "pi":
        # Pi carries its rules in instructions; Codex also supplies developer input.
        items = items[1:]
    original = deepcopy(items)
    instructions = get_chatgpt_default_instructions() + "\n\n" + client + " native instructions"

    async def run():
        response = await litellm.aresponses(
            model="chatgpt/gpt-6-luna", input=items, instructions=instructions,
            stream=False, num_retries=0, api_base="https://offline.invalid/backend-api/codex",
        )
        assert response.output[0].content[0].text == "OK"

    asyncio.run(asyncio.wait_for(run(), timeout=15))
    assert len(wire) == 1
    assert wire[0]["input"] == original
    assert wire[0]["instructions"] == instructions
    assert items == original


def test_direct_anthropic_responses_adapter_preserves_midturn_rules(provider):
    from litellm.llms.anthropic.experimental_pass_through.responses_adapters.transformation import (
        LiteLLMAnthropicToResponsesAPIAdapter,
    )
    incoming = {"model": "chatgpt/gpt-6-luna", "max_tokens": 32,
                "system": "Top-level instructions", "messages": [
                    {"role": "user", "content": "First"},
                    {"role": "system", "content": "Mid-turn instructions"},
                    {"role": "user", "content": "Second"}]}
    original = deepcopy(incoming)
    bridged = LiteLLMAnthropicToResponsesAPIAdapter().translate_request(incoming)
    before = deepcopy(bridged)
    assert bridged["input"][1]["role"] == "system"
    request = transform(provider, bridged["input"], instructions=bridged["instructions"])
    expected = deepcopy(before["input"])
    expected[1]["role"] = "developer"
    assert request["input"] == expected
    assert request["instructions"] == get_chatgpt_default_instructions() + "\n\nTop-level instructions"
    assert incoming == original and bridged == before


def test_non_message_tool_item_with_role_metadata_is_not_rewritten(provider):
    item = {"type": "function_call_output", "call_id": "call_fixture",
            "output": "tool result", "role": "system"}
    assert transform(provider, [deepcopy(item)])["input"] == [item]
