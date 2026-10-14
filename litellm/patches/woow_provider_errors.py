"""Woow client-safe provider errors and provider headers for pinned LiteLLM 1.104.0.

Installed as ``litellm/litellm_core_utils/woow_provider_errors.py`` by
``apply_upstream_guard.py``. Standard library only: it never imports LiteLLM, so the patched
upstream modules can import it lazily from any layer, and lean tests load it by path.

OpenRouter error bodies carry the gateway's own OpenRouter account (``user_id``), the
upstream provider's raw answer (``error.metadata.raw``), BYOK flags, flagged input and
provider headers. LiteLLM copies that text into the client error, the streaming error frame
and the spend log, which internal users can read. Everything here rebuilds such a body as
``{"error":{"message":M,"code":C}}`` with a status-dependent ``M`` and replaces account,
organisation, request, generation and key identifiers. The original text only goes to the
server log (WARNING, already secret-redacted by LiteLLM).

Every function is idempotent: a router or text-completion wrapper may scrub the same text
again without changing it.
"""
import json
import logging
import os
import re
from typing import Final

_LOG: Final = logging.getLogger("LiteLLM")

MSG_401: Final = (
    "The proxy's upstream credentials were rejected. Your API key is valid; contact the proxy administrator."
)
MSG_402: Final = "The upstream provider declined the request for a billing reason. Contact the proxy administrator."
MSG_403: Final = "The upstream provider refused the request. Contact the proxy administrator."
MSG_403_FLAGGED: Final = "The input was flagged by upstream moderation ({reasons})."
MSG_TIMEOUT: Final = "The upstream provider timed out."
MSG_429: Final = "The upstream provider is rate limiting this model. Retry later."
GENERIC_STREAM_MESSAGE: Final = "The upstream provider stream failed."

_FLAGGED_PREFIX: Final = MSG_403_FLAGGED.split("{", 1)[0]
_KEEP_MESSAGE: Final = frozenset({400, 404, 413, 422})
_NESTED_FOR: Final = frozenset({400, 413, 422})
_NESTED_LIMIT: Final = 300
_REASONS_LIMIT: Final = 5
_REASON_LIMIT: Final = 64
_LOG_LIMIT: Final = 2000
_PROVIDER_ERROR_MARK: Final = "Provider returned error"

REDACTED: Final = "[redacted]"
REDACTED_URL: Final = "[redacted-url]"
_ID_RULES: Final = (
    (re.compile(r"https?://[^\s\"'<>]*openrouter\.ai[^\s\"'<>]*", re.I), REDACTED_URL),
    (re.compile(r"\buser_[A-Za-z0-9]{6,}"), REDACTED),
    (re.compile(r"\borg-[A-Za-z0-9]{6,}"), REDACTED),
    (re.compile(r"\b(?:req|gen|msg|chatcmpl)[_-][A-Za-z0-9-]{6,}"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9-]{8,}"), REDACTED),
    (re.compile(r"\b[0-9a-f]{32,}\b"), REDACTED),
)

PROVIDER_HEADERS_ENV: Final = "WOOW_PROVIDER_HEADERS"
_PROVIDER_HEADER_PREFIX: Final = "llm_provider-"
_ALLOWED_PROVIDER_HEADER_PREFIXES: Final = ("x-ratelimit-",)
_ALLOWED_PROVIDER_HEADERS: Final = frozenset({"retry-after"})


def redact_ids(text: str) -> str:
    """Replace account, organisation, request, generation and key identifiers and OpenRouter URLs."""
    for pattern, replacement in _ID_RULES:
        text = pattern.sub(replacement, text)
    return text


def _status(status: object, code: object) -> int | None:
    for value in (status, code):
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and 100 <= value <= 599:
            return value
        if isinstance(value, str) and value.isdigit() and 100 <= int(value) <= 599:
            return int(value)
    return None


def _nested_message(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    try:
        nested = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(nested, dict):
        return None
    inner = nested.get("error")
    message = inner.get("message") if isinstance(inner, dict) else (inner if isinstance(inner, str) else nested.get("message"))
    if not isinstance(message, str) or not message.strip():
        return None
    return message.strip()[:_NESTED_LIMIT]


def _flagged(reasons: list) -> str:
    return MSG_403_FLAGGED.format(reasons=", ".join(str(reason)[:_REASON_LIMIT] for reason in reasons[:_REASONS_LIMIT]))


def _client_message(status: int | None, message: str, metadata: dict) -> str:
    if status == 401:
        return MSG_401
    if status == 402:
        return MSG_402
    if status == 403:
        if message.startswith(_FLAGGED_PREFIX):
            return message  # already scrubbed: the reasons are gone from the rebuilt body
        reasons = metadata.get("reasons")
        if isinstance(reasons, list) and reasons:
            return _flagged(reasons)  # never the flagged input itself
        return MSG_403
    if status in (408, 504):
        return MSG_TIMEOUT
    if status == 429:
        return MSG_429
    if status in _NESTED_FOR and message == _PROVIDER_ERROR_MARK:
        nested = _nested_message(metadata.get("raw"))
        if nested is not None:
            return message + ": " + nested
    return message  # 400/404/413/422, other 4xx and 5xx keep the provider's own message


def _body_parts(body: dict) -> tuple[str, object, dict]:
    error = body.get("error")
    if isinstance(error, dict):
        message, code, metadata = error.get("message"), error.get("code"), error.get("metadata")
    elif isinstance(error, str):
        message, code, metadata = error, body.get("code"), None
    else:
        message, code, metadata = body.get("message"), body.get("code"), None
    message = "" if message is None else message if isinstance(message, str) else json.dumps(message)
    code = code if isinstance(code, (int, str)) or code is None else None
    return message, code, metadata if isinstance(metadata, dict) else {}


def _rebuild(text: str, status: object) -> str:
    start = text.find("{")
    if start < 0:
        return redact_ids(text)
    try:
        body, end = json.JSONDecoder().raw_decode(text, start)
    except ValueError:
        return redact_ids(text)
    if not isinstance(body, dict):
        return redact_ids(text)
    message, code, metadata = _body_parts(body)
    client = _client_message(_status(status, code), message, metadata)
    rebuilt = json.dumps({"error": {"message": redact_ids(client), "code": code}}, separators=(",", ":"), ensure_ascii=False)
    return redact_ids(text[:start]) + rebuilt + redact_ids(text[end:])


def _is_openrouter_error(provider: object, text: str) -> bool:
    return provider == "openrouter" or ("error" in text and "user_id" in text)


def scrub_provider_error(provider: object, error_str: str, status: object = None) -> str:
    """Client-safe text of a provider error; other providers' text is returned unchanged.

    ``error_str`` is LiteLLM's (already secret-redacted) provider error text. OpenRouter
    errors, and any error text that carries a ``user_id`` (``openai/`` routes pointed at
    OpenRouter), are rebuilt; the original goes to the server log only.
    """
    if not isinstance(error_str, str) or not _is_openrouter_error(provider, error_str):
        return error_str
    scrubbed = _rebuild(error_str, status)
    if scrubbed != error_str:
        _LOG.warning(
            "Woow upstream error scrubbed (provider=%s status=%s) raw=%s",
            provider, status, error_str[:_LOG_LIMIT],
        )
    return scrubbed


def stream_error_parts(error_chunk: object) -> tuple[str, int]:
    """Message and HTTP status for an OpenRouter mid-stream ``{"error": {...}}`` chunk.

    Only ``message`` and ``code`` survive; ``metadata`` (raw provider text, provider headers)
    and ``user_id`` are dropped. A non-numeric code (``"server_error"``) maps to 500 and
    ``rate_limit*`` to 429 instead of crashing on ``int()``.
    """
    chunk = error_chunk if isinstance(error_chunk, dict) else {}
    message = chunk.get("message")
    code = chunk.get("code")
    if isinstance(code, bool) or not isinstance(code, (int, str)):
        code = None
    status = _status(None, code)
    if status is None or status < 400:
        status = 429 if isinstance(code, str) and code.lower().startswith("rate_limit") else 500
    text = GENERIC_STREAM_MESSAGE if message is None or message == "" else str(message)
    body = {"error": {"message": redact_ids(text), "code": code}}
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False), status


def client_safe_stream_message(message: object) -> str:
    """Second line of defence for SSE error events (Anthropic ``/v1/messages`` adapter)."""
    if not isinstance(message, str):
        message = "" if message is None else str(message)
    return _rebuild(message, None)


def provider_header_allowed(name: str) -> bool:
    """Forward only rate-limit and retry-after provider headers (``WOOW_PROVIDER_HEADERS=all`` restores all)."""
    if os.environ.get(PROVIDER_HEADERS_ENV, "allowlist").strip().lower() == "all":
        return True
    key = name.lower()
    if key.startswith(_PROVIDER_HEADER_PREFIX):
        key = key[len(_PROVIDER_HEADER_PREFIX):]
    return key in _ALLOWED_PROVIDER_HEADERS or key.startswith(_ALLOWED_PROVIDER_HEADER_PREFIXES)
