#!/usr/bin/env python3
"""Describe a built image for SP4 (size, layers, digests) and dump its file listings.

Writes
  OUT/<name>.json               manifest/config digests, created, per-layer digest, size,
                                diffID, uncompressed size, entry count, totals
  OUT/<name>-layers.tsv.gz      every tar entry of every layer, in tar order
  OUT/<name>-inventory.tsv.gz   merged file system (one row per path)
The listings hold paths, modes, owners, times and sha256 only (no file content), so
they are safe to upload; they are what repro_compare.py diffs when digests differ.

usage: image_report.py IMAGE.oci.tar --out DIR --name build-a [--epoch N]
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import oci  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--out", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--epoch", type=int)
    ap.add_argument("--platform", default="linux/amd64")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    img = oci.Image(a.image, a.platform)
    index = img.layout.index()
    counts: dict[int, int] = {}
    newer = []
    lpath = os.path.join(a.out, f"{a.name}-layers.tsv.gz")
    with gzip.open(lpath, "wt", encoding="utf-8") as lf:
        lf.write("layer\torder\t" + oci.TSV_HEADER + "\n")

        def on_entry(i, e):
            counts[i] = counts.get(i, 0) + 1
            lf.write(f"{i}\t{e.order}\t{e.tsv()}\n")
            if a.epoch is not None and e.mtime_dec() > Decimal(a.epoch):
                newer.append(e.path)

        fs = img.merged(hash_files=True, on_entry=on_entry)
    oci.write_inventory(fs, os.path.join(a.out, f"{a.name}-inventory.tsv.gz"))

    layers = []
    for i, (desc, did) in enumerate(zip(img.layers, img.diff_ids)):
        st = img.layer_stats[i]
        layers.append({"index": i, "mediaType": desc.get("mediaType"), "digest": desc["digest"], "size": desc["size"],
                       "diff_id": did, "uncompressed_size": st["uncompressed_size"], "entries": counts.get(i, 0)})
    regular = [e for e in fs.values() if e.type == "f"]
    report = {
        "name": a.name,
        "index_manifests": [{k: d.get(k) for k in ("mediaType", "digest", "size", "annotations")} for d in index.get("manifests", [])],
        "manifest_digest": img.manifest_digest,
        "manifest_media_type": img.manifest.get("mediaType"),
        "config_digest": img.manifest["config"]["digest"],
        "created": img.config.get("created"),
        "history": img.config.get("history"),
        "layer_count": len(layers),
        "layers": layers,
        "compressed_bytes": sum(l["size"] for l in layers),
        "uncompressed_bytes": sum(l["uncompressed_size"] for l in layers),
        "merged_entries": len(fs),
        "merged_regular_files": len(regular),
        "merged_regular_bytes": sum(e.size for e in regular),
        "entries_newer_than_epoch": newer[:50],
        "entries_newer_than_epoch_count": len(newer),
    }
    with open(os.path.join(a.out, f"{a.name}.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)
    mib = 1024 * 1024
    print(f"{a.name}: manifest {img.manifest_digest}  config {report['config_digest']}  created {report['created']}")
    for l in layers:
        print(f"  layer {l['index']}: {l['digest']}  {l['size']} B ({l['size'] / mib:.1f} MiB) compressed, "
              f"{l['uncompressed_size']} B ({l['uncompressed_size'] / mib:.1f} MiB) uncompressed, diffID {l['diff_id']}, {l['entries']} entries")
    print(f"  total {report['compressed_bytes']} B compressed; merged {len(fs)} entries, {len(regular)} files, {report['merged_regular_bytes']} B")
    if a.epoch is not None:
        print(f"  entries newer than SOURCE_DATE_EPOCH {a.epoch}: {len(newer)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
