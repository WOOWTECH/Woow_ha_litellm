#!/usr/bin/env python3
"""DESIGN §5.1 check 10: scan every layer of the final image, not just the merged view.

Fails when
* any layer has a forbidden path (enterprise, pgbouncer, apko.json, chainguard keys,
  var/lib/db/sbom) or a file whose sha256 is in the enterprise hash set;
* any layer has a whiteout for a forbidden path (it would mean a lower layer still
  ships it), or the first layer (built FROM scratch) has any whiteout at all;
* merged /etc/apk/repositories is not exactly the lock's Wolfi repository line;
* /etc/apk/keys/ holds anything but wolfi-signing.rsa.pub with the lock's sha256;
* a config label is dev.chainguard.* or has a value mentioning Chainguard, or
  org.opencontainers.image.vendor is not WOOWTECH;
* the layer count differs from --expect-layers, or a layer diffID equals one of the
  upstream layers in the lock.

usage: layer_scan.py IMAGE.oci.tar --lock upstream.lock.json --hashes enterprise-hashes.txt
                     --expect-layers N [--summary out.json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import oci  # noqa: E402
from rules import forbidden_path, whiteout_hides_forbidden  # noqa: E402

# diffID of an empty tar (1024 zero bytes): what BuildKit emits for `WORKDIR` on a
# directory that already exists. Its gzip blob sha256:4f4fb700... is shared by every
# image that has one, so it costs a 32-byte download once per host.
EMPTY_TAR_DIFF_ID = "sha256:5f70bf18a086007016e948b04aed3b82103a36bea41755b6cddfaf10ace3c6ef"
KEYS_DIR = "etc/apk/keys"
REPOS = "etc/apk/repositories"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--lock", required=True)
    ap.add_argument("--hashes", required=True)
    ap.add_argument("--expect-layers", type=int, required=True)
    ap.add_argument("--vendor", default="WOOWTECH")
    ap.add_argument("--summary")
    ap.add_argument("--platform", default="linux/amd64")
    a = ap.parse_args()
    with open(a.lock, encoding="utf-8") as fh:
        lock = json.load(fh)
    with open(a.hashes, encoding="utf-8") as fh:
        hashes = {line.strip() for line in fh if line.strip()}
    if not hashes:
        raise SystemExit("empty enterprise hash set: refusing to pass vacuously")
    upstream_diff_ids = set(lock["platforms"][a.platform]["layer_diff_ids"])

    img = oci.Image(a.image, a.platform)
    failures: list[str] = []
    per_layer = []
    forbidden, whiteouts, content_hits = [], [], []

    def on_entry(i, e):
        while len(per_layer) <= i:
            per_layer.append({"entries": 0, "whiteouts": 0})
        per_layer[i]["entries"] += 1
        if oci.is_whiteout(e.path):
            per_layer[i]["whiteouts"] += 1
            target, opaque = oci.whiteout_target(e.path)
            why = whiteout_hides_forbidden(target, opaque)
            whiteouts.append({"layer": i, "path": e.path, "target": target, "opaque": opaque, "forbidden": why})
            return
        why = forbidden_path(e.path)
        if why:
            forbidden.append({"layer": i, "path": e.path, "rule": why})
        if e.type == "f" and e.sha256 in hashes:
            content_hits.append({"layer": i, "path": e.path, "sha256": e.sha256})

    fs = img.merged(hash_files=True, on_entry=on_entry,
                    capture=lambda p: p == REPOS or p.startswith(KEYS_DIR + "/"))

    if forbidden:
        failures.append(f"{len(forbidden)} forbidden path(s) in layers")
    if content_hits:
        failures.append(f"{len(content_hits)} enterprise content hash hit(s) in layers")
    bad_wh = [w for w in whiteouts if w["forbidden"]]
    if bad_wh:
        failures.append(f"{len(bad_wh)} whiteout(s) pointing at forbidden paths")
    if any(w["layer"] == 0 for w in whiteouts):
        failures.append("whiteouts in layer 0 (FROM scratch layer must have none)")

    want_repos = (lock["wolfi_repository"] + "\n").encode()
    repos = fs.get(REPOS)
    repos_ok = repos is not None and repos.data == want_repos
    if not repos_ok:
        failures.append(f"/etc/apk/repositories is {repos.data if repos else None!r}, want {want_repos!r}")
    keys = {p[len(KEYS_DIR) + 1:]: e for p, e in fs.items() if p.startswith(KEYS_DIR + "/")}
    key_names = sorted(keys)
    wolfi = keys.get("wolfi-signing.rsa.pub")
    wolfi_sha = hashlib.sha256(wolfi.data).hexdigest() if wolfi is not None and wolfi.data is not None else None
    if key_names != ["wolfi-signing.rsa.pub"] or wolfi_sha != lock["wolfi_signing_key_sha256"]:
        failures.append(f"/etc/apk/keys = {key_names}, wolfi key sha256 {wolfi_sha} (lock {lock['wolfi_signing_key_sha256']})")

    labels = img.config.get("config", {}).get("Labels") or {}
    bad_labels = {k: v for k, v in labels.items() if k.startswith("dev.chainguard.") or "chainguard" in str(v).lower() or "chainguard" in k.lower()}
    if bad_labels:
        failures.append(f"Chainguard labels: {bad_labels}")
    if labels.get("org.opencontainers.image.vendor") != a.vendor:
        failures.append(f"vendor label {labels.get('org.opencontainers.image.vendor')!r} != {a.vendor!r}")

    if len(img.layers) != a.expect_layers:
        failures.append(f"{len(img.layers)} layers, expected {a.expect_layers}")
    reused = [d for d in img.diff_ids if d in upstream_diff_ids]
    if reused:
        failures.append(f"layers identical to upstream layers: {reused}")

    layers = []
    for i, (desc, did) in enumerate(zip(img.layers, img.diff_ids)):
        st = img.layer_stats[i]
        layers.append({"index": i, "digest": desc["digest"], "size": desc["size"], "diff_id": did,
                       "uncompressed_size": st["uncompressed_size"], **(per_layer[i] if i < len(per_layer) else {})})

    summary = {
        "check": "layer-scan (DESIGN 5.1 #10)",
        "result": "FAIL" if failures else "PASS",
        "failures": failures,
        "manifest_digest": img.manifest_digest,
        "layers": layers,
        "empty_layers": [i for i, d in enumerate(img.diff_ids) if d == EMPTY_TAR_DIFF_ID],
        "forbidden": forbidden[:200],
        "whiteouts": whiteouts[:200],
        "content_hits": content_hits[:200],
        "repositories": repos.data.decode("utf-8", "replace") if repos is not None and repos.data is not None else None,
        "apk_keys": key_names,
        "wolfi_signing_key_sha256": wolfi_sha,
        "labels": labels,
    }
    if a.summary:
        with open(a.summary, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=1)
    print(f"layer-scan: {summary['result']}  layers={len(img.layers)} (empty: {summary['empty_layers']}) entries={[l.get('entries') for l in layers]} "
          f"whiteouts={len(whiteouts)} forbidden={len(forbidden)} content_hits={len(content_hits)} keys={key_names}")
    for f in failures:
        print("  FAIL:", f)
    for x in (forbidden + content_hits + whiteouts)[:50]:
        print("  ", x)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
