# LiteLLM 1.104.0 API compatibility regression checks

These offline tests import the real LiteLLM package. They do not extract functions
with AST or replace the behavior under test. Set `PYTHONPATH` to a private copy of
the **unmodified 1.104.0 package**, with its proxy dependencies installed, then run:

```sh
python -m pytest -q tests/api_compat tests/test_api_compat_patch.py
python litellm/patches/apply_api_compat.py /path/to/private/litellm
python -m pytest -q tests/api_compat tests/test_api_compat_patch.py tests/test_config_contract.py
```

The first run is expected to fail on the observed compatibility bugs. The second
run must pass. The patch validates the complete input and output SHA-256, the exact
replacement count, and Python syntax for **all five files before writing any**.
Applying twice intentionally fails closed. Do not repin around an unexpected hash:
inspect the upstream change and rerun the regression tests.

## Covered seams

- Real ASGI application, registered `/models` and `/v1/models` routes, master-key
  authentication, cached virtual-key authentication, common role/route checks and
  model access filters. Only the database rows and router deployments are synthetic.
  Tests cover raw and URL-encoded slashes, simple IDs, unauthenticated access,
  nonexistent/forbidden IDs, key route restrictions and secret-free response bodies.
- Actual Responses-to-chat translator, sync/async `CustomStreamWrapper`, Pydantic
  serialization and proxy SSE data generator. Text, tools and incomplete output keep
  their finish reasons; usage is last with `choices=[]`, and `[DONE]` occurs once.
  Disabling usage does not add a usage packet. OpenAI-provider regression is included.
- Full proxy processing tail for chat, Responses and Messages with offline provider
  and telemetry boundaries. Known numeric zero must survive hidden response metadata,
  logging recovery and cost calculation; unknown cost stays absent. The streaming
  header seam also tests numeric zero, string zero, nonzero and unknown values.
- AI Hub source diagnosis: real ASGI config read with a real SettingsStore, premium
  true/false and unset/explicit false/explicit true. These fixtures change only local
  in-memory state; they neither edit runtime configuration nor bypass a license gate.

Socket connections are forbidden during every contract test. All keys, identifiers,
provider responses and users are synthetic; no operator credential file is read.
No inference request reaches an upstream provider.

## M4 API error contracts (`apply_api_errors.py`)

`test_api_errors.py` drives the real ASGI app with an OpenRouter-shaped provider at the
httpx transport (including its usage chunk after the finish chunk) and the real
`_ProxyDBLogger` writing into a recording DB writer. It covers API-04.S05/S06 (mock fields
refused; validation rejections write no spend log and call no provider), API-07.S07 (no
choice after the finish chunk, proxy and sync/async wrapper), API-11.S02 (tool_call_id must
answer an earlier assistant tool call), API-22.S01/S02 (OpenAI envelope with `code` equal to
the HTTP status on OpenAI routes; generic 500 for unhandled exceptions; `/v1/completions`
without `prompt` is a 400). Each has a guard: key-allowed mock responses, provider failures
and successes still logged, matching tool ids forwarded, management/Anthropic/unknown-root
error bodies unchanged, provider errors keep their status and message.

The patch runs last (after `apply_login_throttle.py`) on both the HA and PaaS chains and
adds `proxy/woow_api_errors.py`. The image harness verifies its final hashes through
`final_api_patches(api, routes, errors, guard)`.

## Upstream guard contracts (`apply_upstream_guard.py`)

`test_upstream_guard.py` uses the same real ASGI app, auth cache and recording spend writer,
with an OpenRouter-shaped provider at the httpx transport whose fake api_base host picks the
answer (`or400` … `or504`, `midstream`, `midstream-strcode`). Error bodies carry a fake
gateway account (`user_id`), `error.metadata` (raw upstream text, provider name, BYOK flag,
flagged input) and probe headers. It covers:

- P0 leaks on chat, streaming before/after the first chunk, `/v1/completions`, embeddings,
  Responses and `/v1/messages`, for the master key and a virtual key: no account, org,
  request or generation id, metadata, flagged input, OpenRouter URL, `API Base`, `Messages:`,
  64-hex value or non-allowlisted `llm_provider-*` header in the body or headers. The
  `(status, type, code, param)` envelope equals a table recorded on the replayed HA 0.1.8
  chain; only messages change. The spend-log failure row keeps its error code, class and the
  upstream request id, and the raw text appears exactly once in the server log.
- The server-log record of the original text: with the LiteLLM logger at ERROR (what
  `LITELLM_LOG=ERROR` leaves), exactly one ERROR record per provider error on every route,
  `openai/` deployments and mid-stream chunks included; credentials (`sk-`, Bearer, JWT,
  `api_key=`) redacted and the text cut to 2000 characters, while the body, headers and spend
  log stay scrubbed. `WOOW_PROVIDER_ERROR_LOG=off` writes no record; an invalid value is
  reported at ERROR and the raw record is kept.
- The same P0 checks for `openai/` deployments pointed at OpenRouter (chat, chat streaming,
  `/v1/completions` with and without streaming, embeddings), where the OpenAI SDK reports the
  body as a Python repr or only the inner message: same envelope table, the 401/402/403/429
  wording, a scrubbed spend-log row. An Anthropic error that merely mentions
  `metadata.user_id` stays byte-identical. `/health` gives a virtual key no raw request,
  api_base host or stack trace.
- P1 output-token limits (OpenAI wording, `param`, no provider call, no spend row; the
  Anthropic envelope on `/v1/messages`; warn/off switches) and budget admission (strict
  refusal before the provider when input plus the reasoning reserve does not fit, while the
  tail of a budget still admits requests without max_tokens or with Claude Code's 32000;
  422 by default or 429 when configured, `x-should-retry: false`, no key hash, reasoning
  reserve and input floor, explicit reasoning-off forms including `thinking.type: disabled`,
  bounded concurrent spend, full and resize switches, unpriced-model policy).
- P2 `reasoning_effort` forwarded for a model missing from the cost map, route-level
  `reasoning` defaults and client overrides, and the empty-content/`length` pass-through.

`test_auth_key_hash.py` (D1) uses the same gateway with a database that holds no
verification-token row. For an unknown key, a key whose row was deleted after it worked, an
expired, a blocked, a pasted key hash and a key without the `sk-` prefix, on chat, chat
streaming, `/v1/messages` (with and without streaming), `/v1/models` and `/key/info`: no 64-hex
value in the body or headers, the `(status, type, code, param)` envelope recorded on bf0d29b, the
masked `sk-...last4` kept in the unknown-key message, spend-log `error_information` (message,
traceback, class) without the hash while the row's `api_key` column and a budget error's
`error_budget_entity_id` stay, exactly one ERROR server-log record that still holds the hash, and
a failure callback's replacement error scrubbed too. Key-scoped rate limits (`api_key`,
`model_per_key`) answer 429 with `[redacted]` in place of the hash; key budget refusals keep
naming the key by alias.

The patch runs last (after `apply_api_errors.py`) on both chains. It shares only
`proxy/common_request_processing.py` with earlier installers, starting from the API errors
output; its other nine targets are pristine 1.104.0 files. It adds
`litellm_core_utils/woow_provider_errors.py`, `proxy/woow_request_guard.py` and
`proxy/auth/woow_auth_errors.py`, and the image harness verifies its final hashes through
`final_api_patches(api, routes, errors, guard)`.
`x-litellm-model-api-base` on successful responses still names the internal api_base; that
header is out of scope here and excluded from the leak scan.

## M4 batch 1 contracts (`apply_m4_batch1.py`)

`test_m4_batch1.py` uses the same real ASGI app, auth cache and recording spend writer; the
provider at the httpx transport echoes the Authorization header it received (`echo`, `echoraw`
without a Bearer prefix, `echojwt` with a JWT key) and an internal URL into its error body,
breaks a stream mid-way the same way (`midecho`) or cannot be connected to (`dead`). It covers:

- R03: on chat, chat streaming, `/v1/completions` (with and without streaming), embeddings,
  Responses (with and without streaming) and `/v1/messages` (with and without streaming), for
  the master key and a virtual key, no deployment key, JWT, internal host or raw connection
  detail reaches the client (JSON errors, SSE error frames, the `/v1/messages` adapter's SSE
  error event, Responses WebSocket failure frames), while the provider's own wording stays;
  connection failures read `Connection error`; a ChatGPT access token resolved for a request
  is masked by value.
- R04: a `proxy_admin_viewer` key gets 403 `user not allowed to access this OpenAI routes` on
  chat, completions, embeddings, Responses, messages, images and audio, with no provider call
  and nothing billed; its `GET /v1/models` and other roles are unchanged.
- R11: every Responses stream event that carries `response` names the requested model group,
  as the non-streaming response does.
- R15: `/health` for an admin, the rows written to LiteLLM_HealthCheckTable and
  `/health/latest` carry a first-line, masked error; ChatGPT endpoints show an `api_base` to
  admins only.
- R01: new credential names with `%` or `/`, model names with `%` and client-chosen model ids
  with `%` or `/` get 400 on create and rename; other names (spaces, CJK, `chatgpt/…` model
  names) and keeping a stored name are not refused.

The patch runs last (after `apply_upstream_guard.py`) on both chains and checks the upstream
guard's installed `woow_provider_errors.py` first. Four targets start from earlier outputs
(`proxy/proxy_server.py` from the API errors patch; `proxy/common_request_processing.py`,
`proxy/health_endpoints/_health_endpoints.py` and the Anthropic adapter streaming iterator from
the upstream guard); the other eight are pristine 1.104.0 files. It adds
`litellm_core_utils/woow_client_guard.py`; the image harness verifies the final hashes through
`final_api_patches(api, routes, errors, guard, batch1)`.

## Stream slot release contracts (`apply_stream_slot_release.py`)

`test_stream_slots.py` runs the real ASGI app with the v3 parallel request limiter registered the
way proxy startup does it and a virtual key with `max_parallel_requests=1`. Requests are driven at
the ASGI interface the way uvicorn serves them (the client vanishing turns into `http.disconnect`),
each in a task of its own. The provider at the httpx transport answers at once (`quick`), stops
after its first event (`hold`, `holdlate`), withholds its first token longer than the keepalive
interval (`slow`, `slowhold`) or until the client is gone (`silent`). A second request with the same
key tells whether the slot is free (200) or still taken (429). With
`sse_keepalive_ping_interval_seconds` set it covers API-09:

- chat, Responses and `/v1/messages` streams read to the end, or left after their first event, free
  the slot within 2 s, also when the keepalive opened the response with `: ping`;
- a client that leaves after only a `: ping` (the request is cancelled while the provider call is
  pending, which no logging callback sees) frees it too, and the provider call is abandoned.

Guards: an open stream keeps its slot (three probes get 429), and without a keepalive interval, as
well as for non-streaming requests, the slot is held and released as before.

The patch runs last (after `apply_m4_batch1.py`) on both chains. Its one target,
`proxy/common_request_processing.py`, starts from the batch 1 output; first it checks the files its
helper calls into (the limiter and the budget reservation as the upstream guard leaves them, and
pristine `proxy/utils.py`). It adds `proxy/woow_stream_slots.py`; the image harness verifies the
final hashes, the pristine dependency included, through
`final_api_patches(api, routes, errors, guard, batch1, stream)`.

## Build integration

The candidate Dockerfile invokes the new `apply_api_compat.py` after the existing
Responses patch. Its upstream targets do not overlap the Responses recovery patch:

- `proxy/_types.py`: keep internal-user route ACL templates aligned with path routing.
- `proxy/proxy_server.py`: accept slash-bearing IDs and preserve collection slash redirects.
- `proxy/auth/auth_utils.py`: avoid Google action inference only for the actual dispatched
  OpenAI model-info GET; retain the dedicated handler’s alias and model ACL checks.
- `litellm_core_utils/streaming_handler.py`: clear choices only on final usage packets.
- `proxy/common_request_processing.py`: remove zero-destroying truthiness fallbacks.

This patch changes no Woow plugin file, key verification or model ACL implementation, license gate or SSE
recovery patch file. Both patch orders are expected to produce identical source.
Run the sibling Responses protocol suite in **a separate pytest process**: it installs
an intentional process-wide audit hook forbidding subprocesses, whereas repository
config tests invoke `sh -n`. Mixing these suites would test the audit hook rather than
shell syntax. Building/publishing/deploying a new image remains the integrator's step.

## Required CI separation and image gate

Lean CI installs no LiteLLM/httpx. Its command is explicitly:

```sh
pytest -q tests --ignore=tests/api_compat
```

No import-skips are used. Both `ci.yml` build-smoke and the publish workflow invoke
`tools/ci-api-contracts.sh <already-built-image>` after build and before publication.
It resolves an immutable local image ID, downloads **only four version/hash-locked
pytest harness wheels** from PyPI, then runs that exact image with `--network none`,
`--read-only`, no pull and only read-only test/harness mounts. No LiteLLM source or
HTTP/ASGI dependency is copied from the checkout into the runtime. The temporary
overlay consists exactly of pytest, pluggy, iniconfig and packaging; their versions
and hashes are recorded, and they are not installed into the release image. The harness verifies
all five patched image-source hashes against the candidate before collecting tests,
rejects zero collected/skipped tests, and propagates any nonzero result. The image is
not modified and is never exported to a public artifact. Pinned wheels are not part
of the release image.

An implementation/lean pass does not count as a built-image pass. A successful actual
CI image gate is required before merge; publish independently repeats the same gate.
The pre-existing multi-choice second-finish-reason gap is documented in the review
receipts; original API-07 has no n>1 requirement, so this patch does not broaden the
streaming core to address that unrelated behavior.
