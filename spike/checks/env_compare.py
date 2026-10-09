#!/usr/bin/env python3
"""DESIGN §5.1 check 9: the final image config carries the upstream Env.

Every key of the lock's upstream Env must be in the final config with the same value,
except keys listed in lock["env_overrides"] (key -> expected final value). Keys the
final image adds are reported, not failed. With --upstream-config-facts the other
runtime config fields (User, WorkingDir, ExposedPorts, Entrypoint, Cmd) are compared
too; differences there fail only with --strict-config (the spike base keeps them; the
M1 runtime image deliberately replaces Entrypoint/Cmd with /init).

usage: env_compare.py IMAGE.oci.tar --lock upstream.lock.json
                      [--upstream-config-facts upstream-facts.json [--strict-config]] [--summary out.json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import oci  # noqa: E402

FIELDS = ("User", "WorkingDir", "ExposedPorts", "Entrypoint", "Cmd")


def env_map(env: list[str]) -> dict[str, str]:
    out = {}
    for kv in env:
        k, _, v = kv.partition("=")
        out[k] = v
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--lock", required=True)
    ap.add_argument("--upstream-config-facts")
    ap.add_argument("--strict-config", action="store_true")
    ap.add_argument("--summary")
    ap.add_argument("--platform", default="linux/amd64")
    a = ap.parse_args()
    with open(a.lock, encoding="utf-8") as fh:
        lock = json.load(fh)
    img = oci.Image(a.image, a.platform)
    cfg = img.config.get("config", {})
    final = env_map(cfg.get("Env", []))
    upstream = env_map(lock["env"])
    overrides = lock.get("env_overrides", {})

    failures, missing, changed = [], [], []
    for k, v in upstream.items():
        want = overrides.get(k, v)
        if k not in final:
            missing.append(k)
        elif final[k] != want:
            changed.append({"key": k, "final": final[k], "expected": want})
    if missing:
        failures.append(f"missing upstream Env keys: {missing}")
    if changed:
        failures.append(f"Env values differ: {changed}")
    added = sorted(set(final) - set(upstream))

    config_diff = {}
    if a.upstream_config_facts:
        with open(a.upstream_config_facts, encoding="utf-8") as fh:
            ucfg = json.load(fh)["config"]
        for f in FIELDS:
            if ucfg.get(f) != cfg.get(f):
                config_diff[f] = {"upstream": ucfg.get(f), "final": cfg.get(f)}
        if config_diff and a.strict_config:
            failures.append(f"config fields differ: {config_diff}")

    summary = {
        "check": "env (DESIGN 5.1 #9)",
        "result": "FAIL" if failures else "PASS",
        "failures": failures,
        "upstream_keys": len(upstream),
        "final_env": cfg.get("Env", []),
        "added_keys": added,
        "overrides": overrides,
        "config_field_differences": config_diff,
    }
    if a.summary:
        with open(a.summary, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=1)
    print(f"env: {summary['result']}  upstream keys={len(upstream)} final keys={len(final)} added={added} config_diff={sorted(config_diff)}")
    for f in failures:
        print("  FAIL:", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
