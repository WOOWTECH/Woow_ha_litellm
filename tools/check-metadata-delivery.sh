#!/bin/sh
# Offline gate after the canonical chart fix is merged and sync-woow-plugin.sh runs.
# No sync, build, pull, push, deployment, login or catalog operation is performed here.
set -eu
charts=${1:?usage: $0 <charts-clone> <merged-chart-commit> <pytest-python>}
commit=${2:?provide the reviewed merged chart commit}
python=${3:?provide a Python with litellm[proxy]==1.104.0 and pytest}
repo=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
full=$(git -C "$charts" rev-parse --verify "$commit^{commit}")
if ! git -C "$charts" merge-base --is-ancestor "$full" origin/main; then
    echo 'BLOCKED: selected chart commit is not on fetched origin/main; do not sync.' >&2
    exit 2
fi
# Verify both recorded hashes and actual canonical bytes before running the test.
"$repo/tools/verify-woow-plugin.sh" --offline
"$python" - "$repo" "$charts" "$full" <<'PY'
import json, pathlib, subprocess, sys
repo, charts, commit = sys.argv[1:]
lock = json.loads((pathlib.Path(repo)/'litellm/woow-plugin.lock.json').read_text())
if lock['source']['commit'] != commit:
    raise SystemExit('BLOCKED: HA lock does not select the reviewed merged commit')
for name in ('sitecustomize.py', 'woow_chatgpt.py', 'woow_chatgpt_plugin.py'):
    actual = (pathlib.Path(repo)/lock['destination']/name).read_bytes()
    canonical = subprocess.check_output(['git', '-C', charts, 'show', commit+':charts/litellm/files/'+name])
    if actual != canonical:
        raise SystemExit('BLOCKED: HA plugin differs from canonical chart bytes')
PY
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT HUP INT TERM
git -C "$charts" show "$full:charts/litellm/tests/test_m4_metadata.py" > "$scratch/test_m4_metadata.py"
git -C "$charts" show "$full:charts/litellm/tests/plugin-ci/offline.py" > "$scratch/offline.py"
WOOW_METADATA_CORE="$repo/litellm/rootfs/usr/share/woow-litellm/py/woow_chatgpt.py" \
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 \
    "$python" "$scratch/offline.py" -- "$python" -m pytest -q -p no:cacheprovider "$scratch/test_m4_metadata.py"
