# M4 HA candidate contracts (2026-10-13)

Base: `ddffc45`, add-on 0.1.1, LiteLLM 1.104.0. This candidate is not released; the parent chooses the release version and integrates the separately owned plugin update after review.

## Startup and cold backup

The six short s6 oneshots end at `init-db`. `litellm` depends on `init-db` and is a longrun without readiness notification. Its supervised Python startup runs predump (600 s), migration (1500 s), UI-session revocation (60 s), and subscription restore (60 s), then execs LiteLLM. HTTP readiness is checked by Docker, not the s6-rc transition. `woow-spendlogs-indexes` is a separate longrun depending on `litellm`; it waits for HTTP liveliness, builds once and sleeps until stopped. Ten services replace the old thirteen-service graph.

Stop/timeout forwards SIGTERM to each startup process group, waits at most five seconds and kills survivors. s6 stops dependents before PostgreSQL. Index worker shutdown is bounded at ten seconds, LiteLLM at thirty, PostgreSQL at sixty, plus the existing ten-second container grace. The 120-second Supervisor timeout is retained. This budget and actual PostgreSQL clean shutdown must be verified in the built-image smoke test, including interruption while predump/migration is running. Local process tests and graph compilation do not prove container stop behavior.

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
