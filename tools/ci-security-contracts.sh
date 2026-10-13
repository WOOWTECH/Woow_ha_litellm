#!/bin/sh
# Validate installed image middleware, never a source overlay.
set -eu
IMG="${1:?usage: tools/ci-security-contracts.sh <built-image>}"
REPO=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
IMAGE_ID=$(docker image inspect --format '{{.Id}}' "$IMG")
docker run --rm --pull never --network none --read-only \
  --tmpfs '/tmp:rw,nosuid,nodev,size=128m' --cap-drop ALL --security-opt no-new-privileges \
  --mount "type=bind,source=$REPO/tests/security_headers,target=/security-contracts,readonly" \
  --entrypoint python "$IMAGE_ID" -I -B /security-contracts/run_image_security.py
