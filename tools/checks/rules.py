"""Path rules shared by the SP1 checks (DESIGN §5.1 checks 1 and 10).

Paths are normalized tar paths: no leading '/' or './', no trailing '/'.
"""

from __future__ import annotations

import posixpath
import re

PY = "python3.13"
SP = f"app/.venv/lib/{PY}/site-packages"

# Files whose *content* defines enterprise code (hash set source, upstream only).
ENTERPRISE_SOURCE = re.compile(
    rf"^(app/enterprise/|{re.escape(SP)}/litellm_enterprise/|{re.escape(SP)}/litellm_enterprise-[^/]*\.dist-info/)"
)
# Build-tool generated dist-info files: identical across unrelated packages (for
# example the 80-byte WHEEL that litellm_proxy_extras shares byte for byte), so they
# say nothing about enterprise content and are left out of the hash set.
GENERATED_DIST_INFO = {"INSTALLER", "REQUESTED", "WHEEL", "direct_url.json", "uv_build.json", "uv_cache.json"}
SMALL_FILE_BYTES = 64


def enterprise_source_hashable(path: str, size: int) -> str:
    """'hash' if this upstream file goes into the enterprise hash set, else the reason it does not."""
    if not ENTERPRISE_SOURCE.match(path):
        return "not-enterprise"
    if size == 0:
        return "empty"
    if size <= SMALL_FILE_BYTES:
        return "small"
    if ".dist-info/" in path and posixpath.basename(path) in GENERATED_DIST_INFO:
        return "generated-dist-info"
    return "hash"


def enterprise_path_violation(path: str) -> str | None:
    """Reason string when `path` is an enterprise path that must not exist in the final image."""
    if path == "app/enterprise" or path.startswith("app/enterprise/"):
        return "app/enterprise"
    parts = path.split("/")
    for p in parts:
        if p.startswith("litellm_enterprise"):
            return "litellm_enterprise*"
        if p == "enterprise_hooks":
            return "enterprise_hooks"
    if path.startswith(SP + "/litellm/"):
        rest = path[len(SP) + len("/litellm/"):].split("/")
        if "enterprise" in rest:  # exact component; enterprise_billing is MIT and allowed
            return "site-packages/litellm/**/enterprise"
    return None


_FORBIDDEN = [
    (re.compile(r"^usr/local/bin/pgbouncer$"), "pgbouncer"),
    (re.compile(r"^etc/apko\.json$"), "apko.json"),
    (re.compile(r"^etc/apk/keys/chainguard-[^/]*$"), "chainguard key"),
    (re.compile(r"^var/lib/db/sbom(/|$)"), "var/lib/db/sbom"),
]


def forbidden_path(path: str) -> str | None:
    """Reason when `path` must not appear in any layer of the final image (check 10)."""
    r = enterprise_path_violation(path)
    if r:
        return r
    for rx, why in _FORBIDDEN:
        if rx.match(path):
            return why
    return None


# What the pruned stage deletes (used to classify differences, not to decide them).
REMOVED_ROOTS = [
    "app/enterprise",
    f"{SP}/litellm_enterprise",
    "usr/local/bin/pgbouncer",
    "etc/apko.json",
    "var/lib/db/sbom",
]
REMOVED_RX = [
    re.compile(rf"^{re.escape(SP)}/litellm_enterprise-[^/]*\.dist-info(/|$)"),
    re.compile(r"^etc/apk/keys/chainguard-[^/]*\.rsa\.pub$"),
]
REWRITTEN_FILES = {"etc/apk/repositories"}


def removed_by_prune(path: str) -> bool:
    for r in REMOVED_ROOTS:
        if path == r or path.startswith(r + "/"):
            return True
    return any(rx.match(path) for rx in REMOVED_RX)


def whiteout_hides_forbidden(target: str, opaque: bool) -> str | None:
    """Reason when a whiteout for `target` (or an opaque marker in it) could hide a forbidden path below."""
    why = forbidden_path(target)
    if why:
        return why
    anchors = REMOVED_ROOTS + ["etc/apk/keys/chainguard-", f"{SP}/litellm_enterprise-"]
    prefix = target + "/" if target else ""
    for a in anchors:
        if a.startswith(prefix):
            return f"ancestor of {a}"
    return None
