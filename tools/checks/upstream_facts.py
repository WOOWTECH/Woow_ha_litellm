#!/usr/bin/env python3
"""Measure the pinned upstream image and check it against the lock.

Reads the OCI layout written by fetch_upstream.py and produces:
  OUT/upstream-facts.json          public: digests, layers, Env, epoch, key hash, counts
  PRIVATE/upstream-inventory.tsv.gz merged file system of the upstream (lists enterprise paths)
  PRIVATE/enterprise-hashes.txt     sha256 of every enterprise file > 64 bytes, any layer
PRIVATE must stay on the runner (never uploaded).

Exit 1 when the image disagrees with the lock (digests, diffIDs, Env, Wolfi key,
SOURCE_DATE_EPOCH when the lock has one).

usage: upstream_facts.py LAYOUT --lock upstream.lock.json --out DIR --private DIR
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import oci  # noqa: E402
from rules import SMALL_FILE_BYTES, SP, enterprise_source_hashable  # noqa: E402

WOLFI_KEY = "etc/apk/keys/wolfi-signing.rsa.pub"
EXPECTED_REMOVALS = {
    "app/enterprise": "dir",
    f"{SP}/litellm_enterprise": "dir",
    "usr/local/bin/pgbouncer": "file",
    "etc/apko.json": "file",
    "var/lib/db/sbom": "dir",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("layout")
    ap.add_argument("--lock", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--private", required=True)
    ap.add_argument("--platform", default="linux/amd64")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    os.makedirs(a.private, exist_ok=True)
    with open(a.lock, encoding="utf-8") as fh:
        lock = json.load(fh)
    plock = lock["platforms"][a.platform]

    img = oci.Image(a.layout, a.platform)
    ent_hashes: dict[str, int] = {}
    ent_files = {"total": 0, "empty": 0, "small": 0, "generated-dist-info": 0, "hash": 0}
    layers = []
    max_mtime_any = Decimal(0)
    per_layer = {}

    def on_entry(i, e):
        nonlocal max_mtime_any
        pl = per_layer.setdefault(i, {"entries": 0, "whiteouts": 0, "xattr_entries": 0, "special": 0})
        pl["entries"] += 1
        if oci.is_whiteout(e.path):
            pl["whiteouts"] += 1
        if e.xattrs:
            pl["xattr_entries"] += 1
        if e.type in "cbpo":
            pl["special"] += 1
        max_mtime_any = max(max_mtime_any, e.mtime_dec())
        if e.type == "f":
            why = enterprise_source_hashable(e.path, e.size)
            if why != "not-enterprise":
                ent_files["total"] += 1
                ent_files[why] += 1
                if why == "hash":
                    ent_hashes[e.sha256] = e.size

    fs = img.merged(hash_files=True, on_entry=on_entry)
    for i, (desc, did) in enumerate(zip(img.layers, img.diff_ids)):
        st = img.layer_stats[i]
        layers.append({"index": i, "digest": desc["digest"], "size": desc["size"], "diff_id": did,
                       "uncompressed_size": st["uncompressed_size"], **per_layer.get(i, {})})

    max_mtime = max(e.mtime_dec() for e in fs.values())
    newest = sorted(fs.values(), key=lambda e: e.mtime_dec(), reverse=True)[:5]
    epoch = math.ceil(max_mtime)
    created = img.config.get("created", "")
    env = img.config.get("config", {}).get("Env", [])
    wolfi = fs.get(WOLFI_KEY)
    keys = sorted(p for p in fs if p.startswith("etc/apk/keys/") and fs[p].type == "f")
    regular = [e for e in fs.values() if e.type == "f"]
    facts = {
        "repo_index_digest": lock["index_digest"],
        "manifest_digest": img.manifest_digest,
        "config_digest": img.manifest["config"]["digest"],
        "created": created,
        "layer_count": len(img.layers),
        "compressed_bytes": sum(l["size"] for l in layers),
        "uncompressed_bytes": sum(l["uncompressed_size"] for l in layers),
        "layers": layers,
        "env": env,
        "config": {k: v for k, v in img.config.get("config", {}).items() if k != "Env"},
        "merged": {
            "entries": len(fs),
            "regular_files": len(regular),
            "regular_bytes": sum(e.size for e in regular),
            "by_type": {t: sum(1 for e in fs.values() if e.type == t) for t in sorted({e.type for e in fs.values()})},
            "special_files": sorted(f"{e.path} {e.type} {e.devmajor},{e.devminor} {e.mode:o}" for e in fs.values() if e.type in "cbpo"),
            "xattr_entries": sorted(f"{e.path} {sorted(e.xattrs)}" for e in fs.values() if e.xattrs),
            "max_mtime": str(max_mtime),
            "newest_entries": [f"{e.mtime} {e.path}" for e in newest],
            "max_mtime_any_layer": str(max_mtime_any),
        },
        "source_date_epoch": epoch,
        "wolfi_signing_key_sha256": wolfi.sha256 if wolfi else None,
        "apk_keys": [os.path.basename(k) for k in keys],
        "expected_removals_present": {p: (p in fs) for p in EXPECTED_REMOVALS},
        "chainguard_keys": [os.path.basename(k) for k in keys if os.path.basename(k).startswith("chainguard-")],
        "litellm_enterprise_dist_info": sorted(p for p in fs if re.match(rf"^{re.escape(SP)}/litellm_enterprise-[^/]*\.dist-info$", p)),
        "enterprise_content": {"files_all_layers": ent_files["total"], "empty": ent_files["empty"],
                               f"small_le_{SMALL_FILE_BYTES}": ent_files["small"],
                               "generated_dist_info": ent_files["generated-dist-info"],
                               "hashed": ent_files["hash"], "hashed_unique": len(ent_hashes)},
        "apk_db_sha256": fs["usr/lib/apk/db/installed"].sha256 if "usr/lib/apk/db/installed" in fs else None,
    }

    problems = []
    if lock["index_digest"] and facts["manifest_digest"] != plock["manifest_digest"]:
        problems.append(f"manifest {facts['manifest_digest']} != lock {plock['manifest_digest']}")
    if plock.get("config_digest") and facts["config_digest"] != plock["config_digest"]:
        problems.append(f"config {facts['config_digest']} != lock {plock['config_digest']}")
    if plock.get("layer_diff_ids") and img.diff_ids != plock["layer_diff_ids"]:
        problems.append("diffIDs differ from lock")
    if lock.get("env") and env != lock["env"]:
        problems.append(f"Env differs from lock: {sorted(set(env) ^ set(lock['env']))}")
    if facts["wolfi_signing_key_sha256"] != lock["wolfi_signing_key_sha256"]:
        problems.append(f"wolfi-signing.rsa.pub sha256 {facts['wolfi_signing_key_sha256']} != lock {lock['wolfi_signing_key_sha256']}")
    if lock.get("source_date_epoch") is not None and epoch != lock["source_date_epoch"]:
        problems.append(f"SOURCE_DATE_EPOCH {epoch} != lock {lock['source_date_epoch']}")
    missing = [p for p, there in facts["expected_removals_present"].items() if not there]
    if missing:
        problems.append(f"paths the pruned stage removes are missing upstream: {missing}")
    facts["problems"] = problems

    with open(os.path.join(a.out, "upstream-facts.json"), "w", encoding="utf-8") as fh:
        json.dump(facts, fh, indent=1)
    oci.write_inventory(fs, os.path.join(a.private, "upstream-inventory.tsv.gz"))
    with open(os.path.join(a.private, "enterprise-hashes.txt"), "w", encoding="utf-8") as fh:
        for h in sorted(ent_hashes):
            fh.write(h + "\n")

    print(f"manifest {facts['manifest_digest']}  layers {facts['layer_count']}  compressed {facts['compressed_bytes']}  uncompressed {facts['uncompressed_bytes']}")
    print(f"merged entries {len(fs)}  regular {len(regular)} ({facts['merged']['regular_bytes']} B)  types {facts['merged']['by_type']}")
    print(f"max mtime merged {max_mtime}  any layer {max_mtime_any}  -> SOURCE_DATE_EPOCH {epoch}  (config created {created})")
    print(f"wolfi-signing.rsa.pub sha256 {facts['wolfi_signing_key_sha256']}")
    print(f"enterprise files (all layers) {ent_files}  unique hashes: {len(ent_hashes)}")
    print(f"special files {facts['merged']['special_files']}")
    print(f"xattr entries {len(facts['merged']['xattr_entries'])}")
    for p in problems:
        print("PROBLEM:", p)
    if os.environ.get("GITHUB_ENV"):
        with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as fh:
            fh.write(f"SOURCE_DATE_EPOCH={epoch}\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
