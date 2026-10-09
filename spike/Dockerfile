# M0 spike SP1/SP4 (DESIGN §5.1): upstream -> pruned -> base.
# Only `base` is ever exported, and only to the runner's disk: nothing here is pushed.
# The runtime stage (apk add PG18/nginx, s6, rootfs) is SP2/M1 and is not built here.
#
# Build (see .github/workflows/spike-m0-sp1.yml):
#   SOURCE_DATE_EPOCH=<lock> docker buildx build --platform linux/amd64 --target base \
#     --provenance=false --sbom=false --no-cache \
#     --output type=oci,dest=final.oci.tar,rewrite-timestamp=true,compression=gzip,compatibility-version=30,oci-mediatypes=true \
#     -f spike/Dockerfile <empty dir>

ARG LITELLM_IMAGE=ghcr.io/berriai/litellm-non_root:1.104.0@sha256:37066242e6b8327fdd627682a66a9ae2a599553b4d5a84c905c6d8b9eb35c01e

FROM ${LITELLM_IMAGE} AS upstream
# buildx --platform linux/amd64 resolves the index to sha256:843a35c72a832e9c062b66111b1aa54e6588fb32c3ab400d3e20ea9c5fa656a0.

FROM upstream AS pruned
# Lock value; the CI also recomputes it from the upstream layers (upstream_facts.py).
ARG WOLFI_SIGNING_KEY_SHA256=f0031424cf46f7db780ce63a45f0fd6aa6f85f601e6bb3b7a91fe3d4d5b7d2cc
ARG WOLFI_REPOSITORY=https://packages.wolfi.dev/os
# Upstream ends with USER 65534; deleting needs root. This stage is never exported.
USER root
# Fail if the upstream layout moved (a silent no-op rm would ship enterprise code),
# then delete enterprise code, pgbouncer and the Chainguard packaging, and point apk
# at public Wolfi with only the Wolfi key (the one shipped by the wolfi-keys package).
RUN set -eu; \
    sp=/app/.venv/lib/python3.13/site-packages; \
    for p in /app/enterprise "$sp/litellm_enterprise" /usr/local/bin/pgbouncer /etc/apko.json /var/lib/db/sbom; do \
      test -e "$p" || { echo "pruned: expected $p in the upstream image" >&2; exit 1; }; \
    done; \
    ls -d "$sp"/litellm_enterprise-*.dist-info /etc/apk/keys/chainguard-*.rsa.pub >/dev/null; \
    rm -rf /app/enterprise "$sp/litellm_enterprise" "$sp"/litellm_enterprise-*.dist-info /var/lib/db/sbom; \
    rm -f /usr/local/bin/pgbouncer /etc/apko.json /etc/apk/keys/chainguard-*.rsa.pub; \
    printf '%s\n' "$WOLFI_REPOSITORY" > /etc/apk/repositories; \
    echo "$WOLFI_SIGNING_KEY_SHA256  /etc/apk/keys/wolfi-signing.rsa.pub" | sha256sum -c -; \
    test "$(ls -A /etc/apk/keys)" = "wolfi-signing.rsa.pub"

FROM scratch AS base
# One layer: the whole pruned file system. Upstream layers (and with them the deleted
# files) are not part of this image. Reproducible with SOURCE_DATE_EPOCH + rewrite-timestamp.
COPY --from=pruned / /
# FROM scratch inherits nothing: restate the upstream non_root 1.104.0 config
# (Env order and values as in the upstream config; checked by env_compare.py).
ENV PATH=/app/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/bin:/usr/sbin:/sbin:/bin \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    PRISMA_BINARY_CACHE_DIR=/opt/prisma/binaries \
    PRISMA_CLI_PATH=/opt/prisma/binaries/node_modules/.bin/prisma \
    PRISMA_CLI_QUERY_ENGINE_TYPE=binary \
    HOME=/app \
    LITELLM_NON_ROOT=true \
    PRISMA_SKIP_POSTINSTALL_GENERATE=1 \
    PRISMA_HIDE_UPDATE_MESSAGE=1 \
    PRISMA_ENGINES_CHECKSUM_IGNORE_MISSING=1 \
    PRISMA_OFFLINE_MODE=true
WORKDIR /app
USER 65534
EXPOSE 4000/tcp
ENTRYPOINT ["/app/docker/prod_entrypoint.sh"]
CMD ["--port", "4000"]
ARG BUILD_REF=unknown
LABEL org.opencontainers.image.vendor=WOOWTECH \
      org.opencontainers.image.source=https://github.com/WOOWTECH/Woow_ha_litellm \
      org.opencontainers.image.revision=${BUILD_REF} \
      org.opencontainers.image.title="Woow LiteLLM M0 spike base" \
      io.woowtech.litellm.version=1.104.0 \
      io.woowtech.litellm.digest=sha256:843a35c72a832e9c062b66111b1aa54e6588fb32c3ab400d3e20ea9c5fa656a0
