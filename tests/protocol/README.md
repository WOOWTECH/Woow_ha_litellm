# Offline Responses regression contract

Run `python tests/protocol/check_responses.py` in an image built by this
repository's Dockerfile. The existing CI build-smoke job runs this command with
`--network none`. It now includes `check_output_recovery.py` automatically.
The ordinary pytest job covers source-patch preflight, not these image tests.

The suite uses the installed LiteLLM HTTP handlers, ChatGPT SSE parser,
Responses iterator, sync/async bridge collectors, chat conversion, and public
`completion` / `acompletion` entrypoints. Only credentials, model metadata and
HTTP transport are synthetic: `httpx.MockTransport` serves fixture SSE.
A Python audit hook rejects network and process operations before provider
imports. Authentication construction/lookups are stubbed before config creation;
auth file access is forbidden. The audit hook is a test guard, not an OS sandbox;
keep CI's `--network none` isolation.

The public API fixture explicitly selects Responses mode in the local model map
and injects the async HTTP-client factory as well as the client argument. It does
not test live catalog routing or authentication. No fixture uses a production
account, credential, request log, host address or filesystem path.

## Lost-output regression and patch integration

The base project commit is `ddffc45bb38bde6fc5fe004c822c827e829dba69`.
Its existing patch already sends non-stream ChatGPT clients through the buffered
SSE parser. That upstream parser recovers item.done/text.done, but loses text
available only in deltas. The bridge collector also discarded all preceding
items and used only the terminal payload. Those are independently reproducible
with LiteLLM 1.104.0 and synthetic terminal responses whose output is empty.

`litellm/patches/apply_responses_fix.py` extends the existing image-build patch:

- Four upstream files must match their complete SHA-256 pins before any write.
  The existing shared recovery dependency is also hash checked, unchanged.
- A small `woow_output_recovery.py` module is installed under `litellm/responses`.
- The buffered ChatGPT parser and ChatGPT-only bridge collector recover observed
  text deltas, text.done and item.done. A nonempty terminal output is authoritative;
  otherwise item.done takes precedence over text.done and accumulated deltas.
- Top-level ChatGPT stream error fields reach the existing error classifier, so
  a rate-limit error remains 429 instead of being mapped to generic 500.
- Existing helper collisions, changed source/context and changed recovery
  dependencies fail preflight. This is intentionally not an idempotent updater.
  Apply to the pinned pristine upstream image, not an already patched image.

No Dockerfile or CI command change is necessary: the current Dockerfile copies
the whole `patches` directory and invokes the patch script. A Helm-only change
cannot apply this Python patch. PaaS integration must build a reviewed image
containing it and select that image through the separate chart review. This
branch does not alter charts, image references, HA plugin state or production.

## Coverage and limits

The completed matrix has 35 test methods, including 86 subtest invocations.
It tests sync/async buffered parser and real iterator-to-collector paths for:

- text only in deltas, text.done, item.done or terminal output;
- interleaved output/content slots and authoritative-event precedence;
- incomplete partial/empty output retaining `length` semantics;
- tool-only output preserving exact tool name, arguments and call ID;
- truly empty completed and reasoning-only output remaining conversion errors;
- provider errors after partial output, failed terminals and truncated streams;
- unrelated provider collector behavior remaining unchanged;
- public completion success and genuine empty-output failure without retries;
- denied network/process attempts.

Against the existing base patch, this identical matrix produces 12 assertion
failures and 20 errors. With this patch it passes with zero failures/errors/skips.
Repository pytest: 125 passed, including four patch preflight negative tests.
These counts describe the local Python 3.14 / LiteLLM 1.104.0 environment;
container build/smoke and image-specific dependency validation remain CI work.

This reproduces a lost-output bug, not any particular historical production
request. Without that request's upstream SSE, genuine upstream emptiness remains
possible. No content is fabricated, no retry is added, no error is turned into a
successful response. The provider's existing output-budget filtering is unchanged;
the public-entrypoint fixture verifies it drops max_tokens/max_output_tokens.
Recovery of function argument deltas without a final tool item, live provider
behavior, streaming-to-chat clients and other LiteLLM versions are not claimed.
