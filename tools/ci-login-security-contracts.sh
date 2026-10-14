#!/bin/sh
# Check the installed login module in the built image; no provider/network access.
set -eu
IMG="${1:?usage: tools/ci-login-security-contracts.sh <built-image>}"
REPO=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
IMAGE_ID=$(docker image inspect --format '{{.Id}}' "$IMG")
docker run --rm --pull never --network none --read-only \
  --tmpfs '/tmp:rw,nosuid,nodev,size=128m' --cap-drop ALL --security-opt no-new-privileges \
  --mount "type=bind,source=$REPO/tests/login_security,target=/login-security,readonly" \
  --entrypoint python "$IMAGE_ID" -I -B /login-security/run_image_login_security.py
