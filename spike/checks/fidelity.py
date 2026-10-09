#!/usr/bin/env python3
"""Prove that pruning + FROM scratch flattening changed nothing it should not.

Compares the merged upstream file system with the merged final one, path by path, on
type, mode, uid, gid, size, sha256, link target, device numbers, xattrs and mtime.
Allowed differences:
  * paths the pruned stage deletes (rules.removed_by_prune) are absent;
  * /etc/apk/repositories is rewritten (content, size, mtime);
  * a directory that directly contained a deleted path may only change its mtime,
    and then to exactly SOURCE_DATE_EPOCH (rewrite-timestamp);
  * tar uname/gname and PAX bookkeeping keys are reported but never fail.
Anything else fails: a lost file, a changed owner or mode, a new file, a deleted path
that is still there, a file newer than the epoch.

usage: fidelity.py --upstream upstream-inventory.tsv.gz --final final-inventory.tsv.gz
                   --epoch N [--summary out.json]
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import posixpath
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import oci  # noqa: E402
from rules import REMOVED_ROOTS, REWRITTEN_FILES, removed_by_prune  # noqa: E402

STRICT = ("type", "mode", "uid", "gid", "size", "mtime", "linkname", "sha256", "dev", "xattrs")
INFO = ("uname", "gname", "pax_keys")


def bucket(path: str) -> str:
    for r in REMOVED_ROOTS:
        if path == r or path.startswith(r + "/"):
            return r
    if "/litellm_enterprise-" in path:
        return "site-packages/litellm_enterprise-*.dist-info"
    if path.startswith("etc/apk/keys/chainguard-"):
        return "etc/apk/keys/chainguard-*.rsa.pub"
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", required=True)
    ap.add_argument("--final", required=True)
    ap.add_argument("--epoch", type=int, required=True)
    ap.add_argument("--summary")
    a = ap.parse_args()
    up = oci.read_inventory(a.upstream)
    fin = oci.read_inventory(a.final)
    epoch = Decimal(a.epoch)

    removed_ok = collections.Counter()
    parents = {posixpath.dirname(p) for p in up if removed_by_prune(p) and not removed_by_prune(posixpath.dirname(p))}
    failures = collections.defaultdict(list)
    info = collections.defaultdict(list)
    touched_dirs = []

    for p, u in up.items():
        f = fin.get(p)
        if removed_by_prune(p):
            if f is None:
                removed_ok[bucket(p)] += 1
            else:
                failures["still_present_after_prune"].append(p)
            continue
        if f is None:
            failures["missing_in_final"].append(p)
            continue
        diff = [k for k in STRICT if u.get(k, "") != f.get(k, "")]
        if p in REWRITTEN_FILES:
            diff = [k for k in diff if k not in ("size", "sha256", "mtime")]
        elif p in parents and diff == ["mtime"] and Decimal(f["mtime"]) == epoch:
            touched_dirs.append(p)
            diff = []
        if diff:
            failures["changed"].append({"path": p, **{k: [u.get(k), f.get(k)] for k in diff}})
        idiff = [k for k in INFO if u.get(k, "") != f.get(k, "")]
        if idiff:
            info["tar_bookkeeping_changed"].append({"path": p, **{k: [u.get(k), f.get(k)] for k in idiff}})

    for p, f in fin.items():
        if p not in up:
            failures["added_in_final"].append({"path": p, "type": f["type"], "mode": f["mode"], "uid": f["uid"], "gid": f["gid"], "mtime": f["mtime"]})
        if Decimal(f["mtime"]) > epoch:
            failures["newer_than_epoch"].append(p)

    ok = not failures
    summary = {
        "check": "flatten fidelity (upstream merged vs final merged)",
        "result": "PASS" if ok else "FAIL",
        "upstream_entries": len(up),
        "final_entries": len(fin),
        "removed_as_expected": dict(sorted(removed_ok.items())),
        "removed_total": sum(removed_ok.values()),
        "rewritten": sorted(REWRITTEN_FILES),
        "dirs_with_mtime_set_to_epoch": sorted(touched_dirs),
        "failures": {k: v[:200] for k, v in failures.items()},
        "failure_counts": {k: len(v) for k, v in failures.items()},
        "info": {k: v[:50] for k, v in info.items()},
        "info_counts": {k: len(v) for k, v in info.items()},
    }
    if a.summary:
        with open(a.summary, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=1)
    print(f"fidelity: {summary['result']}  upstream={len(up)} final={len(fin)} removed={summary['removed_total']} "
          f"{dict(removed_ok)} epoch-dirs={touched_dirs}")
    for k, v in failures.items():
        print(f"  FAIL {k}: {len(v)}")
        for x in v[:15]:
            print("     ", x)
    for k, v in info.items():
        print(f"  info {k}: {len(v)} (first: {v[:3]})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
