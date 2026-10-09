#!/usr/bin/env python3
"""Download one platform of a pinned upstream image into an OCI layout directory.

Anonymous registry API only (no docker daemon). Every blob is verified against its
digest while streaming. The layout's index.json lists just the selected platform
manifest; the full upstream index and the attestation manifests that point at the
selected manifest (plus their layers, which are small in-toto statements) are kept
as blobs so later steps can read them.

The output contains upstream files: keep it on the runner, never upload it.

usage: fetch_upstream.py --repo ghcr.io/berriai/litellm-non_root \
         --index-digest sha256:... --manifest-digest sha256:... --out DIR
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(NoRedirect)


def _open(url: str, headers: dict, tries: int = 4):
    for attempt in range(tries):
        req = urllib.request.Request(url, headers=headers)
        try:
            return _opener.open(req, timeout=120)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = urllib.parse.urljoin(url, e.headers["Location"])
                # The blob store URL is pre-signed; do not forward the registry token there.
                return urllib.request.urlopen(urllib.request.Request(loc), timeout=120)
            if e.code in (429, 500, 502, 503, 504) and attempt + 1 < tries:
                time.sleep(2 ** attempt)
                continue
            raise
        except urllib.error.URLError:
            if attempt + 1 < tries:
                time.sleep(2 ** attempt)
                continue
            raise


class Registry:
    def __init__(self, ref: str):
        host, _, repo = ref.partition("/")
        self.host, self.repo = host, repo
        q = urllib.parse.urlencode({"scope": f"repository:{repo}:pull", "service": host})
        with urllib.request.urlopen(f"https://{host}/token?{q}", timeout=60) as r:
            self.token = json.load(r)["token"]

    def _h(self, extra=None):
        h = {"Authorization": f"Bearer {self.token}"}
        h.update(extra or {})
        return h

    def manifest(self, digest: str) -> bytes:
        with _open(f"https://{self.host}/v2/{self.repo}/manifests/{digest}", self._h({"Accept": ACCEPT})) as r:
            data = r.read()
        check(data, digest)
        return data

    def blob_to(self, digest: str, path: str, size: int | None = None) -> int:
        h = hashlib.sha256()
        n = 0
        tmp = path + ".part"
        with _open(f"https://{self.host}/v2/{self.repo}/blobs/{digest}", self._h()) as r, open(tmp, "wb") as out:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                h.update(b)
                n += len(b)
                out.write(b)
        if "sha256:" + h.hexdigest() != digest:
            os.unlink(tmp)
            raise SystemExit(f"blob {digest}: content hashes to sha256:{h.hexdigest()}")
        if size is not None and n != size:
            os.unlink(tmp)
            raise SystemExit(f"blob {digest}: {n} bytes, descriptor says {size}")
        os.replace(tmp, path)
        return n


def check(data: bytes, digest: str) -> None:
    got = "sha256:" + hashlib.sha256(data).hexdigest()
    if got != digest:
        raise SystemExit(f"manifest {digest}: content hashes to {got}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--index-digest", required=True)
    ap.add_argument("--manifest-digest", required=True)
    ap.add_argument("--platform", default="linux/amd64")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    osname, arch = a.platform.split("/")
    blobs = os.path.join(a.out, "blobs", "sha256")
    os.makedirs(blobs, exist_ok=True)

    def put(digest: str, data: bytes) -> None:
        with open(os.path.join(blobs, digest.split(":", 1)[1]), "wb") as fh:
            fh.write(data)

    reg = Registry(a.repo)
    index_raw = reg.manifest(a.index_digest)
    put(a.index_digest, index_raw)
    index = json.loads(index_raw)
    plat = [
        d for d in index["manifests"]
        if d.get("platform", {}).get("os") == osname and d.get("platform", {}).get("architecture") == arch
    ]
    if [d["digest"] for d in plat] != [a.manifest_digest]:
        raise SystemExit(f"index {a.index_digest} lists {[d['digest'] for d in plat]} for {a.platform}, expected {a.manifest_digest}")
    desc = plat[0]

    man_raw = reg.manifest(a.manifest_digest)
    put(a.manifest_digest, man_raw)
    man = json.loads(man_raw)
    total = reg.blob_to(man["config"]["digest"], os.path.join(blobs, man["config"]["digest"].split(":", 1)[1]), man["config"]["size"])
    for i, layer in enumerate(man["layers"]):
        n = reg.blob_to(layer["digest"], os.path.join(blobs, layer["digest"].split(":", 1)[1]), layer["size"])
        total += n
        print(f"layer {i:02d} {layer['digest']} {n} bytes OK", flush=True)

    attest = []
    for d in index["manifests"]:
        ann = d.get("annotations", {})
        if ann.get("vnd.docker.reference.type") == "attestation-manifest" and ann.get("vnd.docker.reference.digest") == a.manifest_digest:
            raw = reg.manifest(d["digest"])
            put(d["digest"], raw)
            am = json.loads(raw)
            for layer in am.get("layers", []):
                reg.blob_to(layer["digest"], os.path.join(blobs, layer["digest"].split(":", 1)[1]), layer["size"])
            attest.append(d["digest"])

    with open(os.path.join(a.out, "oci-layout"), "w") as fh:
        json.dump({"imageLayoutVersion": "1.0.0"}, fh)
    with open(os.path.join(a.out, "index.json"), "w") as fh:
        json.dump({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [desc]}, fh)
    with open(os.path.join(a.out, "fetch.json"), "w") as fh:
        json.dump({"repo": a.repo, "index_digest": a.index_digest, "manifest_digest": a.manifest_digest,
                   "attestation_manifests": attest, "layer_count": len(man["layers"]), "bytes": total}, fh, indent=1)
    print(f"fetched {a.repo}@{a.manifest_digest}: {len(man['layers'])} layers, {total} bytes; attestations {attest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
