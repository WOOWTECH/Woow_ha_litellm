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
