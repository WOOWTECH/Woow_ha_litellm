#!/usr/bin/env python3
"""SP4: compare two independent builds of the same Dockerfile.

Inputs are the outputs of image_report.py for each build. Equal compressed layer
digests and diffIDs mean HA hosts reuse the layer across releases. When they differ,
the per-entry listings (tar order included) are diffed column by column to show why:
entry order, mtime, owner, mode, xattrs/PAX keys, content.

usage: repro_compare.py DIR_A NAME_A DIR_B NAME_B [--summary out.json]
"""

from __future__ import annotations

import argparse
import collections
import gzip
import json
import sys


def load(d: str, name: str):
    with open(f"{d}/{name}.json", encoding="utf-8") as fh:
        return json.load(fh)


def rows(d: str, name: str):
    with gzip.open(f"{d}/{name}-layers.tsv.gz", "rt", encoding="utf-8") as fh:
        cols = fh.readline().rstrip("\n").split("\t")
        return cols, [line.rstrip("\n").split("\t") for line in fh]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir_a")
    ap.add_argument("name_a")
    ap.add_argument("dir_b")
    ap.add_argument("name_b")
    ap.add_argument("--summary")
    a = ap.parse_args()
    A, B = load(a.dir_a, a.name_a), load(a.dir_b, a.name_b)

    same = {
        "manifest_digest": A["manifest_digest"] == B["manifest_digest"],
        "config_digest": A["config_digest"] == B["config_digest"],
        "layer_count": A["layer_count"] == B["layer_count"],
    }
    layers = []
    for la, lb in zip(A["layers"], B["layers"]):
        layers.append({
            "index": la["index"],
            "digest_equal": la["digest"] == lb["digest"],
            "diff_id_equal": la["diff_id"] == lb["diff_id"],
            "a": {k: la[k] for k in ("digest", "diff_id", "size", "uncompressed_size", "entries")},
            "b": {k: lb[k] for k in ("digest", "diff_id", "size", "uncompressed_size", "entries")},
        })
    all_layers_equal = same["layer_count"] and all(l["digest_equal"] and l["diff_id_equal"] for l in layers)

    why = {}
    if not all_layers_equal:
        ca, ra = rows(a.dir_a, a.name_a)
        cb, rb = rows(a.dir_b, a.name_b)
        col_diffs = collections.Counter()
        examples = collections.defaultdict(list)
        first = None
        for i, (x, y) in enumerate(zip(ra, rb)):
            if x != y:
                if first is None:
                    first = {"row": i, "a": x, "b": y}
                for c, vx, vy in zip(ca, x, y):
                    if vx != vy:
                        col_diffs[c] += 1
                        if len(examples[c]) < 10:
                            examples[c].append({"path_a": x[2], "path_b": y[2], "a": vx, "b": vy})
        why = {"rows_a": len(ra), "rows_b": len(rb), "differing_columns": dict(col_diffs),
               "first_difference": first, "examples": dict(examples)}

    result = "PASS" if all_layers_equal else "FAIL"
    summary = {"check": "SP4 reproducibility", "result": result, "same": same, "layers": layers,
               "a_created": A.get("created"), "b_created": B.get("created"), "why_different": why}
    if a.summary:
        with open(a.summary, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=1)
    print(f"reproducibility: {result}  manifest equal={same['manifest_digest']} config equal={same['config_digest']}")
    for l in layers:
        print(f"  layer {l['index']}: digest equal={l['digest_equal']} diffID equal={l['diff_id_equal']}  "
              f"A {l['a']['digest']} {l['a']['size']} B / B {l['b']['digest']} {l['b']['size']} B")
    if why:
        print("  differing columns:", why["differing_columns"])
        print("  first difference:", why["first_difference"])
    return 0 if all_layers_equal else 1


if __name__ == "__main__":
    sys.exit(main())
