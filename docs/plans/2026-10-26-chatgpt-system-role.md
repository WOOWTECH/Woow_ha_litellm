# ChatGPT system input compatibility plan

**Goal:** Preserve caller instructions when Anthropic/Responses requests reach the
ChatGPT provider, whose backend rejects system-role input messages.

**Architecture:** Add a hash-guarded build patch in the existing shared patch owner
repository. Normalize only message input items from `system` to `developer` after
the real parent Responses transformation. Preserve item order, content, existing
instructions and every non-message item. Do not change the generic OpenAI adapter.

**Tech stack:** LiteLLM 1.104.0, Python 3.13, pytest, existing offline image contracts.

## Provenance and scope

- Deployed PaaS factory: `woow-paas/paas-odoo-ci` at
  `0ab8d11fa7116f2d25fb2b6ad4f9dcfa566f42d2` (approval issue 400).
- Deployed image: `20261024.0943`, digest
  `sha256:87900caf08de32ee80858d98428ee6fb4aed369482d6327109dde1f7c6d6395b`.
- Shared patch owner: `WOOWTECH/Woow_ha_litellm`; deployed API bundle source
  `2f7c34d60c9fb7863391ebbf82740863bbaf11b1`.
- This isolated checkout starts at main
  `7799383e6af147e04503f36dab3a7a3cff40e185`.
- No deployment, shared route edit, tenant key copy, or live credential use.

## Steps

1. Run `tests/api_compat/test_chatgpt_system_role.py` against unmodified request
   serialization; require the exact system/developer mismatch (RED), not an import error.
2. Add `litellm/patches/apply_chatgpt_system_role.py`, accepting only the deployed
   provider hash and producing a pinned output hash. One-shot, fail closed on drift.
3. Re-run that test (GREEN). Add real Anthropic bridge, mixed/empty input,
   instructions, tool/reasoning replay and native Codex/Pi-shaped regression cases.
4. Register the patch in the Docker build and installed-image hash verification;
   run existing provider/protocol/API tests and the lean installer suite.
5. Open a draft source PR. Let native PR CI build/test without publishing. Document
   exact subsequent PaaS vendoring/build gates for main review; do not trigger release.

## Acceptance boundary

Offline tests must execute installed LiteLLM provider and bridge code; only outbound
HTTP/authentication may use synthetic fixtures. Passing those tests is not a live
ChatGPT conversation or permission to update any production image.

## Regression evidence and review notes

The deployed provider hash is
`f20de0053f82f2fc1826b4bef778c02d8235fe02c0fca50dc560bea98a4eb258`.
The normalized provider hash is
`664413197293c7a59f34184db9d7ec68e80531ad6f727e8c350c9f09e901763c`.
The behavioral change is twelve lines in the provider; no additional proxy exists.

The HTTP regression invokes the real async Anthropic messages handler, completion
bridge, Responses adapter and ChatGPT provider. The synthetic HTTP boundary returns
the observed 400 for system input. Removing only this patch makes both block-system
cases fail (streaming and buffered); restoring it passes all 17 role contracts.
Plain Anthropic system strings already become top-level instructions and remain
unchanged. Native Responses cases exercise GPT-6 Luna request shapes; the Anthropic
bridge uses GPT-5.4 because the pinned offline model registry marks it Responses-native.
No fetched registry or mocked routing decision is used.

Both HTTP transports are intercepted; DNS, socket connections and auth-file reads
fail immediately. Requests target `offline.invalid`. Each case has a 20-second
deadline; async operations have a 15-second timeout. Authentication and response
content are synthetic. Nonempty output and exactly one outgoing request are required.

The runtime regression is collected by the existing built-image API gate. The
installer is copied to that gate and the installed provider hash is verified before
tests. The native PR workflow only builds/tests; publication is a separate manual
workflow. No release version, image tag, gateway configuration or credentials change.

References: [LiteLLM ChatGPT provider](https://docs.litellm.ai/docs/providers/chatgpt)
documents its native Responses path; [OpenAI's agent-loop description](https://openai.com/index/unrolling-the-codex-agent-loop/)
describes client instructions and developer input in native Codex requests. The
specific rejected system-role behavior comes from the deployment evidence, not an
assumption that every Responses backend rejects that role.
