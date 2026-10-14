#!/bin/sh
# Shared mandatory gate for CI and publish: test the already-built image, offline.
set -eu
IMG="${1:?usage: tools/ci-api-contracts.sh <built-image>}"
REPO=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
API_TMP=$(mktemp -d)
trap 'rm -rf "$API_TMP"' EXIT HUP INT TERM
# Use an immutable local image ID; no pull, push or deployment in this helper.
IMAGE_ID=$(docker image inspect --format '{{.Id}}' "$IMG")
printf 'API contracts image: %s\n' "$IMAGE_ID"
mkdir -p "$API_TMP/harness/wheels"
cp "$REPO/tests/api_contract_harness/"*.py "$REPO/tests/api_contract_harness/"*.json "$API_TMP/harness/"
cp "$REPO/litellm/patches/apply_api_compat.py" "$REPO/litellm/patches/apply_routes_auth.py" \
  "$REPO/litellm/patches/apply_api_errors.py" "$API_TMP/harness/"
python3 -m venv "$API_TMP/downloader"
"$API_TMP/downloader/bin/python" -m pip download --disable-pip-version-check \
  --index-url https://pypi.org/simple --only-binary=:all: --no-deps --require-hashes \
  --dest "$API_TMP/harness/wheels" --requirement "$REPO/tests/api_contract_harness/requirements.lock"
# Only test sources and four locked pytest wheels are mounted. Runtime LiteLLM and
# HTTP/ASGI dependencies come from IMAGE_ID; only the named pytest dependency overlay
# is added temporarily. No LiteLLM source overlay or package install in the image.
docker run --rm --pull never --network none --read-only \
  --tmpfs /tmp:rw,nosuid,nodev --entrypoint python --workdir /tmp \
  --mount "type=bind,source=$API_TMP/harness,target=/harness,readonly" \
  --mount "type=bind,source=$REPO/tests/api_compat,target=/contracts,readonly" \
  "$IMAGE_ID" -I -B /harness/run_image_contracts.py
