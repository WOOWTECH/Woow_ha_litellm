# Woow LiteLLM add-on image. Prebuilt by CI; never built on Home Assistant.
#
# Owner decision 2026-10-09: a PRIVATE image for WOOWTECH's own Home Assistant hosts (private GHCR
# package, pulled by the Supervisor with registry credentials), built like the WOOW PaaS image:
# directly on ghcr.io/berriai/litellm-non_root:1.104.0 (the very image the PaaS runs), enterprise
# code kept, nothing pruned or flattened. On top: PostgreSQL 18, nginx, curl and jq from public
# Wolfi (the whole dependency closure pinned, SP2), s6-overlay, and the add-on's rootfs.
#
# Every pin here must equal litellm/upstream.lock.json (tests/test_config_contract.py).
# The ARG is not called BUILD_FROM on purpose: the Supervisor overrides BUILD_FROM.
ARG LITELLM_IMAGE=ghcr.io/berriai/litellm-non_root:1.104.0@sha256:37066242e6b8327fdd627682a66a9ae2a599553b4d5a84c905c6d8b9eb35c01e

# s6-overlay v3.2.3.2 tarballs, checked by sha256 (lock: s6_overlay). Only bind-mounted below,
# so the tarballs themselves never become part of a layer.
FROM scratch AS s6-overlay
ADD --checksum=sha256:5379750ed30a84bbd2e2dd74847ba6b5bd29cd0b2e3ea2ec58049b57eb2eda12 \
    https://github.com/just-containers/s6-overlay/releases/download/v3.2.3.2/s6-overlay-noarch.tar.xz /s6-overlay-noarch.tar.xz
ADD --checksum=sha256:e6befcc96a437a3831386ecfc51808c5d3e939dc5fe3c02ae9284599e8aa2408 \
    https://github.com/just-containers/s6-overlay/releases/download/v3.2.3.2/s6-overlay-x86_64.tar.xz /s6-overlay-x86_64.tar.xz

FROM ${LITELLM_IMAGE} AS runtime
# buildx --platform linux/amd64 resolves the index to sha256:843a35c72a832e9c062b66111b1aa54e6588fb32c3ab400d3e20ea9c5fa656a0.
# Upstream ends with USER 65534; s6-overlay's /init must run as root (it drops privileges per service).
USER root
SHELL ["/bin/sh", "-eu", "-c"]
ARG WOLFI_REPOSITORY=https://packages.wolfi.dev/os
ARG WOLFI_SIGNING_PUB_SHA256=f0031424cf46f7db780ce63a45f0fd6aa6f85f601e6bb3b7a91fe3d4d5b7d2cc

# PostgreSQL 18, nginx, curl, jq — the whole dependency closure pinned (38 name=version, lock
# wolfi_apk.apk_add; SP2 2026-10-09), taken from public Wolfi only: the upstream image's own
# /etc/apk/repositories (apk.cgr.dev) is left as it is and ignored for this install. The Wolfi key
# is the one the upstream image already carries (wolfi-keys package), checked, never downloaded.
# Fixed accounts: postgres 70, woow-nginx 101; LiteLLM keeps the upstream uid 65534 (nobody), like
# the PaaS. The nginx workers must not share uid 65534 (they could read LiteLLM's 0600 files).
RUN echo "$WOLFI_SIGNING_PUB_SHA256  /etc/apk/keys/wolfi-signing.rsa.pub" | sha256sum -c - \
 && apk add --no-cache --repositories-file /dev/null --repository "$WOLFI_REPOSITORY" \
      postgresql-18=18.6-r5 \
      postgresql-18-client=18.6-r5 \
      nginx-mainline=1.31.6-r3 \
      curl=8.22.0-r4 \
      jq=1.8.2-r3 \
      nginx-mainline-config=1.31.6-r3 \
      libLLVM-19=19.1.7-r28 \
      libldap-2.7=2.7.1-r2 \
      libxml2-16=2.15.4-r0 \
      cyrus-sasl-heimdal-libs=2.1.28-r58 \
      ecpg-18=18.6-r5 \
      heimdal-libs=7.8.0-r52 \
      keyutils-libs=1.6.3-r40 \
      krb5-conf=1.0-r10 \
      krb5-libs=1.22.2-r5 \
      libcom_err=1.47.4-r2 \
      libcurl-openssl4=8.22.0-r4 \
      libedit=3.1-r19 \
      libidn2=2.3.8-r9 \
      liblz4-1=1.10.0-r10 \
      libpcre2-8-0=10.49-r1 \
      libpq-18=18.6-r5 \
      libpsl=0.23.3-r1 \
      libselinux=3.11-r2 \
      libsepol=3.11-r3 \
      libunistring=1.4.2-r3 \
      libverto=0.3.2-r8 \
      libzstd1=1.5.7-r10 \
      linux-pam=1.7.3-r0 \
      nghttp3=1.18.0-r1 \
      nginx-mainline-fastcgi-params=1.31.6-r3 \
      nginx-mainline-mime-types=1.31.6-r3 \
      nginx-mainline-scgi-params=1.31.6-r3 \
      nginx-mainline-uwsgi-params=1.31.6-r3 \
      ngtcp2=1.25.0-r5 \
      oniguruma=6.9.10-r5 \
      postgresql-18-base=18.6-r5 \
      postgresql-18-client-base=18.6-r5 \
 && addgroup -S -g 70 postgres \
 && adduser -S -D -H -u 70 -G postgres -h /data/postgres -s /sbin/nologin -g "PostgreSQL" postgres \
 && addgroup -S -g 101 woow-nginx \
 && adduser -S -D -H -u 101 -G woow-nginx -h /nonexistent -s /sbin/nologin -g "nginx workers" woow-nginx \
 && test "$(id -u postgres):$(id -g postgres)" = "70:70" \
 && test "$(id -u woow-nginx):$(id -g woow-nginx)" = "101:101" \
 && test "$(id -u nobody)" = "65534" \
 && postgres --version | grep -q ' 18\.' \
 && nginx -V 2>&1 | grep -q -- '--with-http_realip_module'

# s6-overlay (static binaries; /init is PID 1, `init: false` in config.yaml).
RUN --mount=type=bind,from=s6-overlay,source=/,target=/tmp/s6 \
    xzcat /tmp/s6/s6-overlay-noarch.tar.xz | tar -C / -xpf - \
 && xzcat /tmp/s6/s6-overlay-x86_64.tar.xz | tar -C / -xpf - \
 && test -x /init && test -x /command/s6-rc

# The upstream Env (PATH, HOME=/app, PRISMA_*, …) and WORKDIR /app are inherited. s6 hands no
# container environment to the services (S6_KEEP_ENV=0): init-woow-env writes the upstream Env into
# their env dirs from /usr/share/woow-litellm/upstream.env (= lock "env").
ENV CHECKPOINT_DISABLE=1 \
    S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    S6_CMD_WAIT_FOR_SERVICES_MAXTIME=0 \
    S6_KILL_GRACETIME=10000

# The add-on (s6 services, init, nginx, help page, Woow plugin copy).
COPY rootfs/ /

ARG BUILD_VERSION=0.1.0
ARG BUILD_REF=unknown
ARG BUILD_ARCH=amd64
LABEL org.opencontainers.image.title="Woow LiteLLM" \
      org.opencontainers.image.description="LiteLLM AI gateway (OpenAI-compatible) with built-in PostgreSQL, packaged by WOOWTECH for its own Home Assistant hosts (private image)" \
      org.opencontainers.image.vendor=WOOWTECH \
      org.opencontainers.image.source=https://github.com/WOOWTECH/Woow_ha_litellm \
      org.opencontainers.image.revision=${BUILD_REF} \
      org.opencontainers.image.version=${BUILD_VERSION} \
      io.hass.type=addon \
      io.hass.arch=${BUILD_ARCH} \
      io.hass.version=${BUILD_VERSION} \
      io.woowtech.litellm.version=1.104.0 \
      io.woowtech.litellm.digest=sha256:843a35c72a832e9c062b66111b1aa54e6588fb32c3ab400d3e20ea9c5fa656a0 \
      io.woowtech.woow-plugin.source=0.3.0@dcd929edddbae4e4b284c9447568c6e10aff2c34

# start-period 45 min ≥ Supervisor retry 1 + pre-upgrade dump 10 + migration 25 + LiteLLM 5 (§7.6).
HEALTHCHECK --start-period=45m --start-interval=10s --interval=60s --timeout=10s --retries=3 \
  CMD ["/usr/local/bin/woow-healthcheck"]
# The upstream CMD (--port 4000) would become an s6 "main program": cleared.
ENTRYPOINT ["/init"]
CMD []
