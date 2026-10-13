# M4 HA candidate contracts (2026-10-13)

Original M4 base: `ddffc45`, add-on 0.1.1, LiteLLM 1.104.0; integrated in 0.1.2. The follow-up below is based on `bdb5739` (0.1.2); the parent chooses its release version and integrates separately owned plugin changes after review.

## Startup and cold backup

The six short s6 oneshots end at `init-db`. `litellm` depends on `init-db` and is a longrun without readiness notification. Its supervised Python startup runs predump (600 s), migration (1500 s), UI-session revocation (60 s), and subscription restore (60 s), then execs LiteLLM. HTTP readiness is checked by Docker, not the s6-rc transition. `woow-spendlogs-indexes` is a separate longrun depending on `litellm`; it waits for HTTP liveliness, builds once and sleeps until stopped. Ten services replace the old thirteen-service graph.

A single ownership scope covers child spawn, signal-mask restoration and the handoff to wait; cancellation at either unmask or the first line after it terminates and reaps the acquired child before propagating. Spawn failure restores the original signal mask without attempting to clean up an unacquired child. Stop/timeout forwards SIGTERM to each startup process group, waits at most five seconds and kills survivors. s6 stops dependents before PostgreSQL. Index worker shutdown is bounded at ten seconds, LiteLLM at thirty, PostgreSQL at sixty, plus the existing ten-second container grace. The 120-second Supervisor timeout is retained. This budget and actual PostgreSQL clean shutdown must be verified in the built-image smoke test, including interruption while predump/migration is running. Local process tests and graph compilation do not prove container stop behavior.

Only a successful migration-table query returning `f` means fresh database. Query errors/unknown results and dump failures cannot proceed to migration. Incomplete `pre-*.dump.tmp` files are removed on signal/exit and at the next startup (including after SIGKILL). Two completed dumps are retained. `backup: cold`, the full `/data` payload and the exclusion of `pre-upgrade-dumps` remain unchanged. Parent owns real encrypted backup/restore, off-host copies and HA scheduling.

## Environment and secrets

Init logs env names only, regardless of whether names look sensitive. Extra env values must be nonempty single lines, at most 4095 UTF-8 bytes, with no trailing space/tab. UI passwords cannot have surrounding whitespace. Values are rejected rather than silently changed by s6-envdir. This tightens startup validation, not the Supervisor schema.

Config-file/bucket, TLS paths, login-throttle, process-count/restart and metrics-listener overrides are reserved. Migration gets DATABASE_URL as its only secret; master key, salt and UI password stay with LiteLLM. Generated-secret writeback remains the owner's established behavior. Supervisor validation can still disclose user options; docs advise clearing written-back secrets and redacting diagnostics. The add-on cannot repair Supervisor's own formatter.

## Health, metadata and UI

Health requires LiteLLM liveliness and nginx's expected loopback ACL response (403); connection failures and other statuses fail. nginx finish logs exit status and permits s6 restart without halting the independent API. Retain the 45-minute Docker startup grace and recommend HA Watchdog.

Supervisor defaults preserve disabled APIs/host access, enabled AppArmor, admin-only Ingress on 8099 and automatic application startup; redundant entries and the invalid webui URL are removed. The pinned HA app linter is now in CI. No custom AppArmor profile or speculative breaking_versions entry is added.

Store assets are byte-for-byte copies of the existing LiteLLM assets in `WOOWTECH/Woow_ha_mcp_addons`, asset commit `26e0f299699d978df9c6d4111509313cb56fdf88`. Existing interface branding remains unchanged. English and zh-Hant nested env fields are documented. zh-TW docs clarify CLI names, 16 MiB Ingress limit, shared-IP throttling, 4h/24h session defaults, zero-cost subscription limits, backup responsibilities, support and account/token cloning restrictions. Long-term independent OAuth sign-in coexistence remains unverified.

Build workflows pass UTC BUILD_DATE and override inherited OCI author/URL/date. Manual candidate image builds must also pass BUILD_DATE for `tools/ci-image-facts.sh` validation. PostgreSQL explicitly uses UTC and suppresses routine checkpoint messages.

## Integration boundaries

The three synced plugin Python files and their lock are untouched. Original Responses fixes are retained. No HA/SSH/runtime action, image push, merge or deploy is part of this patch. Parent review must cover the new startup supervisor and run the exact CI image build, offline protocol regressions, image-facts check and complete container smoke before releasing. Live lifecycle results must not be inferred from local source tests.

## Follow-up: disabled subscription startup (base bdb5739 / 0.1.2)

This follow-up does not bump the release version. The parent owns release integration.
LiteLLM 1.104.0 calls ChatGPT authentication while initializing Router deployments;
without the plugin's import hook, a retained subscription model can enter device login
before HTTP startup. This is verified upstream behavior, not an upstream disable feature.

HA init now writes `/run/woow/chatgpt-subscription` atomically, root-owned mode 0644
under the existing root-owned 0711 tmpfs directory. Only the exact bytes `enabled\n` (LF, not CRLF) permit ChatGPT
deployments. Disabled, missing, unreadable or invalid policy excludes them from the
in-memory Router before provider construction. The pinned HA build patch covers
`set_model_list`, `add_deployment`, and `upsert_deployment` (initial DB loading,
periodic DB updates and direct additions). Provider identity gives a resolved, nonempty explicit `custom_llm_provider` precedence
over the underlying `model` prefix; only absent/empty explicit providers use that
prefix. Both fields support environment references without calling the authenticating resolver;
a public display name alone is not a provider identity.

There is no SQL mutation, model deletion, credential deletion, token refresh or
plugin import in the gate. With the option off, no PYTHONPATH, CHATGPT_TOKEN_DIR,
WOOW_* variables or plugin routes are added. Other-provider deployments remain
available. Options take effect after restart; re-enabling reloads retained models.
This patch prevents startup/model-loading authentication, not every possible direct
upstream API invocation with a raw provider name. The canonical plugin is untouched.

`tests/protocol/check_responses.py`, already used by CI and publish, runs the existing
44 SSE cases and then the separate subscription Router cases after removing the
SSE authentication stubs. The Router cases use real installed methods and synthetic
authentication boundaries; they are not a built-container or live-HA receipt.

Changelog headings use plain `## VERSION`; dates belong on separate lines. The
exact `SupervisorAddonUpdateEntity.async_release_notes` extractor from HA Core
2026.7.2 was executed locally: bracketed/date headings fall back to the whole log,
while plain latest/installed headings return only the release interval. The new
behavioral contract covers both cases, replacing the incorrect bracket-format test.
