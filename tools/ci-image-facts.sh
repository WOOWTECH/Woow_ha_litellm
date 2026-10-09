#!/bin/sh
# Image facts shared by ci.yml and publish-litellm-addon-images.yml: tool versions, accounts,
# labels, entrypoint, the pinned apk closure (lock) and the upstream Env.
#   tools/ci-image-facts.sh <image>
set -eu
IMG="${1:?usage: tools/ci-image-facts.sh <image>}"
docker run --rm --entrypoint sh "$IMG" -c '
  set -e
  postgres --version; nginx -v; curl --version | head -n1; jq --version
  id postgres; id woow-nginx; id nobody
  test -d /app/enterprise && echo "enterprise code present (private image, owner decision 2026-10-09)"'
docker image inspect -f '{{json .Config.Labels}}' "$IMG" | python3 -m json.tool
docker image inspect -f 'Entrypoint={{json .Config.Entrypoint}} Cmd={{json .Config.Cmd}} User={{json .Config.User}} WorkingDir={{.Config.WorkingDir}}' "$IMG"
IMG="$IMG" python3 - <<'PY'
import json, os, subprocess
img = os.environ["IMG"]
lock = json.load(open("litellm/upstream.lock.json"))
out = subprocess.run(["docker", "run", "--rm", "--entrypoint", "apk", img, "info", "-v"],
                     capture_output=True, text=True, check=True).stdout.split()
missing = [f"{p['name']}-{p['version']}" for p in lock["wolfi_apk"]["apk_add"] if f"{p['name']}-{p['version']}" not in out]
print(f"{len(out)} apk packages installed; pinned closure missing: {missing or 'none'}")
assert not missing
cfg = json.loads(subprocess.run(["docker", "image", "inspect", "-f", "{{json .Config}}", img],
                                capture_output=True, text=True, check=True).stdout)
assert cfg["Labels"]["org.opencontainers.image.vendor"] == "WOOWTECH"
assert cfg["Entrypoint"] == ["/init"] and not cfg.get("Cmd"), (cfg["Entrypoint"], cfg.get("Cmd"))
assert cfg.get("User") in ("root", "0", ""), cfg.get("User")
env = dict(kv.split("=", 1) for kv in cfg["Env"])
for kv in lock["env"]:
    k, v = kv.split("=", 1)
    assert env.get(k) == v, k
print("image facts: OK")
PY
