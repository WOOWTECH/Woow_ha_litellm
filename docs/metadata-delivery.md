# Sparse ChatGPT metadata delivery

The canonical fix is chart commit `bdae33eab7107680fa31ffc24c5853ca44b891d2`, stacked on chart PR184. Router startup can create a responses-only cost-map entry before the delayed plugin sync. The fix fills missing/None fields when the entry has no explicit `max_input_tokens`, preserving False, zero, prices, endpoints and mode. Entries with an explicit context, including zero, are unchanged.

The chart's CI runs `test_m4_metadata.py` in its own network-denied pytest process: actual LiteLLM 1.104.0 Router registration and `/model/info` endpoint serialization, including explicit deployment overrides. It must not share a process with plugin tests that substitute `proxy_server`.

This HA change only adds a local delivery gate. The plugin copies and lock are unchanged. Do not sync or release from the unmerged chart feature branch. After Eugene merges the chart changes, select the actual reviewed main commit (which may differ after squash/rebase), then in an isolated HA checkout:

```sh
# charts must be the canonical woow-paas/woow-paas-charts clone.
git -C "$charts" fetch origin main
git -C "$charts" merge-base --is-ancestor "$reviewed_chart_commit" origin/main
# Only after that check succeeds and the resulting source is reviewed:
tools/sync-woow-plugin.sh "$reviewed_chart_commit" "$charts"
tools/verify-woow-plugin.sh "$charts"
tools/check-metadata-delivery.sh "$charts" "$reviewed_chart_commit" "$pytest_python"
```

`pytest_python` must contain the chart's pinned plugin-ci requirements. The gate verifies the selected commit is on fetched `origin/main`, validates the HA lock and all three canonical byte copies, and runs the committed canonical test against the HA core file under seccomp. It does not fetch or sync automatically. A locally stale remote ref cannot establish a new merge; fetch and review first.

Before release, also run existing HA configuration tests and built-image gates; record the actual built image digest and installed plugin hashes. No image was built or deployed for this draft. PaaS consumes the same canonical core through the chart ConfigMap, so this defect does not require an image-code patch; existing image PR292/300 dependencies remain separate.

This is a bounded sparse-entry repair, not complete PLG16.S06 parity. It does not rewrite explicit DB nulls, normalize existing 272000 vs bundled 1050000 contexts, overwrite the observed HA 500000 context, or establish fresh HA/PaaS live parity. An entry with an explicit context but missing capabilities remains outside this repair. No catalog, OAuth or inference operation is part of the gate.
