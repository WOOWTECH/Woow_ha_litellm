#!/usr/bin/env python3
"""DESIGN §5.1 check 1: the final image contains no enterprise code, judged by path and by content.

* path: no /app/enterprise, no litellm_enterprise*, no enterprise_hooks, no component
  named exactly `enterprise` under site-packages/litellm/ (enterprise_billing is MIT);
  checked on every layer entry and on the merged file system.
* content: no regular file in any layer has a sha256 from ENTERPRISE_HASHES (built by
  upstream_facts.py from the upstream's enterprise files, minus empty files, files of
  <= 64 bytes and build-tool generated dist-info files).
* metadata: no installed distribution declares LicenseRef-Proprietary or names
  litellm-enterprise (dist-info METADATA / PKG-INFO). Stand-in for the syft check until
  M1 adds syft.

usage: enterprise_content.py IMAGE.oci.tar --hashes enterprise-hashes.txt [--summary out.json]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import oci  # noqa: E402
from rules import enterprise_path_violation  # noqa: E402

META = re.compile(r"\.(dist-info/METADATA|egg-info/PKG-INFO)$")
PROPRIETARY = re.compile(rb"LicenseRef-Proprietary|^Name:\s*litellm[-_]enterprise\s*$", re.I | re.M)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--hashes", required=True)
    ap.add_argument("--summary")
    ap.add_argument("--platform", default="linux/amd64")
    a = ap.parse_args()
    with open(a.hashes, encoding="utf-8") as fh:
        hashes = {line.strip() for line in fh if line.strip()}
    if not hashes:
        raise SystemExit("empty enterprise hash set: refusing to pass vacuously")

    img = oci.Image(a.image, a.platform)
    path_hits, content_hits, scanned = [], [], {"entries": 0, "regular_files": 0}

    def on_entry(i, e):
        scanned["entries"] += 1
        why = enterprise_path_violation(e.path)
        if why:
            path_hits.append({"layer": i, "path": e.path, "rule": why})
        if e.type == "f":
            scanned["regular_files"] += 1
            if e.sha256 in hashes:
                content_hits.append({"layer": i, "path": e.path, "sha256": e.sha256, "size": e.size})

    fs = img.merged(hash_files=True, on_entry=on_entry, capture=lambda p: bool(META.search(p)))
    merged_path_hits = sorted(p for p in fs if enterprise_path_violation(p))
    meta_hits = []
    for p, e in sorted(fs.items()):
        if e.data is not None and META.search(p) and PROPRIETARY.search(e.data):
            meta_hits.append({"path": p, "match": PROPRIETARY.search(e.data).group(0).decode("utf-8", "replace")})
    dist_infos = sum(1 for p in fs if p.endswith(".dist-info/METADATA"))

    ok = not (path_hits or content_hits or merged_path_hits or meta_hits)
    summary = {
        "check": "enterprise-content (DESIGN 5.1 #1)",
        "result": "PASS" if ok else "FAIL",
        "enterprise_hashes": len(hashes),
        "layers": len(img.layers),
        "scanned": scanned,
        "metadata_files_checked": dist_infos,
        "path_hits_any_layer": path_hits[:200],
        "path_hits_merged": merged_path_hits[:200],
        "content_hits": content_hits[:200],
        "license_metadata_hits": meta_hits,
    }
    if a.summary:
        with open(a.summary, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=1)
    print(f"enterprise-content: {summary['result']}  hashes={len(hashes)} layers={len(img.layers)} "
          f"entries={scanned['entries']} files={scanned['regular_files']} dist-info={dist_infos} "
          f"path_hits={len(path_hits)} merged_path_hits={len(merged_path_hits)} content_hits={len(content_hits)} "
          f"license_hits={len(meta_hits)}")
    for h in (path_hits + content_hits)[:50]:
        print("  HIT", h)
    for h in meta_hits:
        print("  LICENSE", h)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
