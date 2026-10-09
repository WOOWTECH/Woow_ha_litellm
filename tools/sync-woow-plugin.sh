#!/bin/sh
# Copy the Woow plugin (single source: woow-paas-charts charts/litellm/files/) into the add-on,
# byte for byte, and rewrite litellm/woow-plugin.lock.json (DESIGN v3.2 §5.9).
#
#   tools/sync-woow-plugin.sh <commit> [<woow-paas-charts clone>]
#
# Needs a local clone that already has <commit> (read-only use: `git show`, nothing is checked
# out or changed there). Default clone: $WOOW_CHARTS_DIR or
# /data/pi-agent/home/work/litellm-chatgpt-sub/charts. Never edit the copies in this repo: change
# woow-paas-charts first, then run this again.
set -eu

commit="${1:?usage: $0 <commit> [<woow-paas-charts clone>]}"
charts="${2:-${WOOW_CHARTS_DIR:-/data/pi-agent/home/work/litellm-chatgpt-sub/charts}}"
repo="$(cd "$(dirname "$0")/.." && pwd)"
dest="$repo/litellm/rootfs/usr/share/woow-litellm/py"
lock="$repo/litellm/woow-plugin.lock.json"
src_path="charts/litellm/files"
files="sitecustomize.py woow_chatgpt.py woow_chatgpt_plugin.py"

full="$(git -C "$charts" rev-parse --verify "$commit^{commit}")"
chart_version="$(git -C "$charts" show "$full:charts/litellm/Chart.yaml" | sed -n 's/^version:[[:space:]]*//p' | head -n1)"
branch="$(git -C "$charts" branch -a --contains "$full" --format='%(refname:short)' 2>/dev/null | head -n1)"
on_main=false
if git -C "$charts" merge-base --is-ancestor "$full" origin/main 2>/dev/null; then on_main=true; fi

mkdir -p "$dest"
for f in $files; do
  git -C "$charts" show "$full:$src_path/$f" > "$dest/$f.tmp"
  mv "$dest/$f.tmp" "$dest/$f"
  chmod 0644 "$dest/$f"
done

WOOW_DEST="$dest" WOOW_FILES="$files" WOOW_COMMIT="$full" WOOW_CHART="$chart_version" \
WOOW_BRANCH="$branch" WOOW_ON_MAIN="$on_main" WOOW_SRC_PATH="$src_path/" WOOW_CHARTS="$charts" \
python3 - "$lock" <<'PY'
import hashlib, json, os, subprocess, sys, time

dest = os.environ["WOOW_DEST"]
files = {}
for name in os.environ["WOOW_FILES"].split():
    path = os.path.join(dest, name)
    data = open(path, "rb").read()
    blob = subprocess.run(["git", "hash-object", path], capture_output=True, text=True, check=True).stdout.strip()
    expected = subprocess.run(
        ["git", "-C", os.environ["WOOW_CHARTS"], "rev-parse", f"{os.environ['WOOW_COMMIT']}:{os.environ['WOOW_SRC_PATH']}{name}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if blob != expected:
        sys.exit(f"{name}: copy blob {blob} != source blob {expected}")
    files[name] = {"git_blob": blob, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
lock = {
    "_comment": "Woow plugin copy (DESIGN v3.2 §5.9). Written by tools/sync-woow-plugin.sh; checked by tools/verify-woow-plugin.sh and tests/test_config_contract.py. Do not edit the copies.",
    "license": "MIT",
    "source": {
        "repository": "woow-paas/woow-paas-charts",
        "url": "https://git-prod.woowtech.io/woow-paas/woow-paas-charts",
        "path": os.environ["WOOW_SRC_PATH"],
        "commit": os.environ["WOOW_COMMIT"],
        "branch": os.environ["WOOW_BRANCH"] or None,
        "chart_version": os.environ["WOOW_CHART"],
        "merged_to_main": os.environ["WOOW_ON_MAIN"] == "true",
    },
    "destination": "litellm/rootfs/usr/share/woow-litellm/py/",
    "synced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "files": files,
}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(lock, f, indent=2)
    f.write("\n")
print(f"synced {len(files)} files from {lock['source']['chart_version']}@{lock['source']['commit'][:7]}")
PY
