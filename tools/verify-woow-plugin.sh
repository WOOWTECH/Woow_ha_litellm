#!/bin/sh
# Check the Woow plugin copies (DESIGN v3.2 §5.9), fail-closed:
#   1. each copy's git blob id and sha256 equal litellm/woow-plugin.lock.json;
#   2. unless --offline: each copy is byte-for-byte the file at the lock's commit in a local
#      woow-paas-charts clone ($WOOW_CHARTS_DIR, default the pi-agent clone), and say whether that
#      commit is on origin/main yet (required before a release, §5.9 M3).
#
#   tools/verify-woow-plugin.sh [--offline] [<woow-paas-charts clone>]
set -eu

offline=false
if [ "${1:-}" = "--offline" ]; then offline=true; shift; fi
charts="${1:-${WOOW_CHARTS_DIR:-/data/pi-agent/home/work/litellm-chatgpt-sub/charts}}"
repo="$(cd "$(dirname "$0")/.." && pwd)"

WOOW_REPO="$repo" WOOW_CHARTS="$charts" WOOW_OFFLINE="$offline" python3 - <<'PY'
import hashlib, json, os, subprocess, sys

repo = os.environ["WOOW_REPO"]
lock = json.load(open(os.path.join(repo, "litellm/woow-plugin.lock.json"), encoding="utf-8"))
dest = os.path.join(repo, lock["destination"])
commit = lock["source"]["commit"]
src_path = lock["source"]["path"]
bad = []
for name, want in sorted(lock["files"].items()):
    path = os.path.join(dest, name)
    data = open(path, "rb").read()
    blob = subprocess.run(["git", "hash-object", path], capture_output=True, text=True, check=True).stdout.strip()
    sha = hashlib.sha256(data).hexdigest()
    if blob != want["git_blob"] or sha != want["sha256"]:
        bad.append(f"{name}: copy differs from the lock (blob {blob}, sha256 {sha})")
        continue
    if os.environ["WOOW_OFFLINE"] == "true":
        continue
    src = subprocess.run(
        ["git", "-C", os.environ["WOOW_CHARTS"], "show", f"{commit}:{src_path}{name}"], capture_output=True
    )
    if src.returncode != 0:
        bad.append(f"{name}: cannot read {commit}:{src_path}{name} from {os.environ['WOOW_CHARTS']}")
    elif src.stdout != data:
        bad.append(f"{name}: copy is NOT byte-for-byte the source file at {commit}")
extra = sorted(set(os.listdir(dest)) - set(lock["files"]))
if extra:
    bad.append(f"unexpected files next to the copies: {extra}")
if bad:
    print("\n".join(bad), file=sys.stderr)
    sys.exit(1)
print(f"ok: {len(lock['files'])} plugin files match the lock ({lock['source']['chart_version']}@{commit[:7]})")
if os.environ["WOOW_OFFLINE"] != "true":
    on_main = subprocess.run(
        ["git", "-C", os.environ["WOOW_CHARTS"], "merge-base", "--is-ancestor", commit, "origin/main"]
    ).returncode == 0
    print(f"source commit on woow-paas-charts origin/main: {'yes' if on_main else 'NO (not releasable yet, DESIGN §5.9)'}")
PY
