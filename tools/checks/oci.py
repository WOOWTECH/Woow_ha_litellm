"""Minimal, dependency-free reader for OCI image layouts (directory or tar).

Used by the SP1/SP4 checks. Everything is streamed: a layer is decompressed once,
its compressed digest and diffID are verified while its tar entries are walked.
Python stdlib only; run with ``python3 -I``.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import posixpath
import tarfile
from dataclasses import dataclass, field
from decimal import Decimal

LAYER_GZIP = {
    "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.docker.image.rootfs.diff.tar.gzip",
}
LAYER_TAR = {
    "application/vnd.oci.image.layer.v1.tar",
    "application/vnd.docker.image.rootfs.diff.tar",
}
INDEX_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
CHUNK = 1 << 20


class DigestMismatch(Exception):
    pass


class _HashReader(io.RawIOBase):
    """Wrap a binary stream; hash and count everything read through it."""

    def __init__(self, f):
        self.f = f
        self.h = hashlib.sha256()
        self.n = 0

    def readable(self):
        return True

    def readinto(self, b):
        data = self.f.read(len(b))
        n = len(data)
        b[:n] = data
        self.h.update(data)
        self.n += n
        return n

    def read(self, size=-1):
        data = self.f.read(size)
        self.h.update(data)
        self.n += len(data)
        return data

    def drain(self):
        while self.read(CHUNK):
            pass


class Layout:
    """An OCI image layout, either a directory or a tar of one (buildx type=oci)."""

    def __init__(self, path: str):
        self.path = path
        self._tar = None
        if os.path.isfile(path):
            self._tar = tarfile.open(path, "r:")
            self._members = {m.name.lstrip("./"): m for m in self._tar.getmembers()}

    def open(self, rel: str):
        if self._tar is not None:
            m = self._members.get(rel)
            if m is None:
                raise FileNotFoundError(rel)
            return self._tar.extractfile(m)
        return open(os.path.join(self.path, rel), "rb")

    def blob_path(self, digest: str) -> str:
        algo, hexd = digest.split(":", 1)
        return f"blobs/{algo}/{hexd}"

    def read_blob(self, digest: str, verify: bool = True) -> bytes:
        with self.open(self.blob_path(digest)) as fh:
            data = fh.read()
        if verify and "sha256:" + hashlib.sha256(data).hexdigest() != digest:
            raise DigestMismatch(f"blob {digest} does not match its content")
        return data

    def index(self) -> dict:
        with self.open("index.json") as fh:
            return json.load(fh)

    def image_manifest(self, platform: str = "linux/amd64") -> tuple[str, dict]:
        """Return (digest, manifest) of the single-platform manifest for `platform`."""
        osname, arch = platform.split("/")
        todo = list(self.index().get("manifests", []))
        while todo:
            d = todo.pop(0)
            mt = d.get("mediaType", "")
            raw = self.read_blob(d["digest"])
            doc = json.loads(raw)
            if mt in INDEX_TYPES or doc.get("manifests"):
                todo.extend(doc.get("manifests", []))
                continue
            p = d.get("platform")
            if p and (p.get("os"), p.get("architecture")) != (osname, arch):
                continue
            cfg = json.loads(self.read_blob(doc["config"]["digest"]))
            if (cfg.get("os"), cfg.get("architecture")) != (osname, arch):
                continue
            return d["digest"], doc
        raise LookupError(f"no {platform} manifest in {self.path}")

    def config(self, manifest: dict) -> dict:
        return json.loads(self.read_blob(manifest["config"]["digest"]))


@dataclass
class Entry:
    path: str  # normalized: no leading './' or '/', no trailing '/'; '' is the root
    type: str  # f d l h c b p o
    mode: int
    uid: int
    gid: int
    size: int
    mtime: str  # exact text: PAX 'mtime' record if present, else the integer header field
    linkname: str = ""
    sha256: str = ""
    devmajor: int = 0
    devminor: int = 0
    uname: str = ""
    gname: str = ""
    xattrs: dict = field(default_factory=dict)
    pax_keys: tuple = ()
    order: int = 0  # position inside its layer tar
    data: bytes | None = None  # content, only for paths selected by `capture`

    def mtime_dec(self) -> Decimal:
        return Decimal(self.mtime)

    def tsv(self) -> str:
        x = ";".join(f"{k}={v}" for k, v in sorted(self.xattrs.items()))
        return "\t".join(
            [
                self.path or ".",
                self.type,
                "%o" % self.mode,
                str(self.uid),
                str(self.gid),
                str(self.size),
                self.mtime,
                self.linkname,
                self.sha256,
                f"{self.devmajor},{self.devminor}" if self.type in "cb" else "",
                x,
                ",".join(self.pax_keys),
                self.uname,
                self.gname,
            ]
        )


TSV_HEADER = "path\ttype\tmode\tuid\tgid\tsize\tmtime\tlinkname\tsha256\tdev\txattrs\tpax_keys\tuname\tgname"


def norm(p: str) -> str:
    while p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/").rstrip("/")
    return "" if p == "." else p


def _type(m: tarfile.TarInfo) -> str:
    if m.isreg():
        return "f"
    if m.isdir():
        return "d"
    if m.issym():
        return "l"
    if m.islnk():
        return "h"
    if m.ischr():
        return "c"
    if m.isblk():
        return "b"
    if m.isfifo():
        return "p"
    return "o"


_PAX_SKIP = {"path", "linkpath", "size", "uid", "gid", "uname", "gname", "mtime"}


def iter_layer(layout: Layout, desc: dict, diff_id: str | None, hash_files: bool = True, stats: dict | None = None,
               capture=None):
    """Yield Entry for every member of a layer, in tar order.

    Verifies the compressed digest, size and diffID once the layer is exhausted and
    records the measured sizes in `stats` (compressed_size, uncompressed_size, diff_id).
    `capture(path) -> bool` selects regular files (<= 1 MiB) whose content is kept in Entry.data.
    """
    mt = desc.get("mediaType", "")
    raw = layout.open(layout.blob_path(desc["digest"]))
    comp = _HashReader(raw)
    if mt in LAYER_GZIP:
        unc = _HashReader(gzip.GzipFile(fileobj=comp, mode="rb"))
    elif mt in LAYER_TAR:
        unc = comp
    else:
        raise ValueError(f"unsupported layer media type {mt!r}")
    with tarfile.open(fileobj=unc, mode="r|") as tf:
        for i, m in enumerate(tf):
            t = _type(m)
            sha = ""
            data = None
            path = norm(m.name)
            keep = t == "f" and capture is not None and m.size <= CHUNK and capture(path)
            if t == "f" and (hash_files or keep):
                h = hashlib.sha256()
                fo = tf.extractfile(m)
                buf = [] if keep else None
                while True:
                    b = fo.read(CHUNK)
                    if not b:
                        break
                    h.update(b)
                    if buf is not None:
                        buf.append(b)
                sha = h.hexdigest()
                data = b"".join(buf) if buf is not None else None
            pax = m.pax_headers or {}
            xattrs = {}
            for k, v in pax.items():
                for pre in ("SCHILY.xattr.", "LIBARCHIVE.xattr."):
                    if k.startswith(pre):
                        xattrs[k[len(pre):]] = v.encode("utf-8", "surrogateescape").hex() if isinstance(v, str) else str(v)
            yield Entry(
                path=path,
                type=t,
                mode=m.mode,
                uid=m.uid,
                gid=m.gid,
                size=m.size if t == "f" else 0,
                mtime=str(pax["mtime"]) if "mtime" in pax else str(int(m.mtime)),
                linkname=m.linkname if t in ("l", "h") else "",
                sha256=sha,
                devmajor=m.devmajor if t in ("c", "b") else 0,
                devminor=m.devminor if t in ("c", "b") else 0,
                uname=m.uname or "",
                gname=m.gname or "",
                xattrs=xattrs,
                pax_keys=tuple(sorted(k for k in pax if k not in _PAX_SKIP and not k.startswith(("SCHILY.xattr.", "LIBARCHIVE.xattr.")))),
                order=i,
                data=data,
            )
    unc.drain()
    if unc is not comp:
        comp.drain()
    got = "sha256:" + comp.h.hexdigest()
    if got != desc["digest"]:
        raise DigestMismatch(f"layer {desc['digest']}: content hashes to {got}")
    if comp.n != desc.get("size", comp.n):
        raise DigestMismatch(f"layer {desc['digest']}: size {comp.n} != {desc['size']}")
    if diff_id is not None:
        got_diff = "sha256:" + unc.h.hexdigest()
        if got_diff != diff_id:
            raise DigestMismatch(f"layer {desc['digest']}: diffID {got_diff} != config {diff_id}")
    if stats is not None:
        stats.update(compressed_size=comp.n, uncompressed_size=unc.n, diff_id="sha256:" + unc.h.hexdigest())


def is_whiteout(path: str) -> bool:
    return posixpath.basename(path).startswith(".wh.")


def whiteout_target(path: str) -> tuple[str, bool]:
    """('dir/name', False) for '.wh.name'; ('dir', True) for the opaque marker '.wh..wh..opq'."""
    d, b = posixpath.split(path)
    if b == ".wh..wh..opq":
        return d, True
    return posixpath.join(d, b[len(".wh."):]) if d else b[len(".wh."):], False


class Image:
    """One platform image inside a layout, with layer iteration and a merged view."""

    def __init__(self, layout_path: str, platform: str = "linux/amd64"):
        self.layout = Layout(layout_path)
        self.manifest_digest, self.manifest = self.layout.image_manifest(platform)
        self.config = self.layout.config(self.manifest)
        self.layers = self.manifest["layers"]
        self.diff_ids = self.config.get("rootfs", {}).get("diff_ids", [])
        if len(self.diff_ids) != len(self.layers):
            raise ValueError("manifest layer count != config diff_ids count")

    def iter_layers(self, hash_files: bool = True, capture=None):
        """Yield (index, descriptor, diff_id, entries_iterator, stats); stats is filled once entries are exhausted."""
        for i, (desc, did) in enumerate(zip(self.layers, self.diff_ids)):
            stats: dict = {}
            yield i, desc, did, iter_layer(self.layout, desc, did, hash_files, stats, capture), stats

    def merged(self, hash_files: bool = True, on_entry=None, capture=None) -> dict[str, Entry]:
        """Apply layers in order (OCI whiteout semantics) and return path -> Entry.

        `on_entry(layer_index, entry)` is called for every raw entry, whiteouts included,
        so a single pass can serve per-layer checks as well.
        """
        fs: dict[str, Entry] = {}
        self.layer_stats = []
        for i, desc, did, entries, stats in self.iter_layers(hash_files, capture):
            self.layer_stats.append(stats)
            layer_entries = []
            for e in entries:
                if on_entry is not None:
                    on_entry(i, e)
                layer_entries.append(e)
            for e in layer_entries:
                if not is_whiteout(e.path):
                    continue
                target, opaque = whiteout_target(e.path)
                prefix = target + "/" if target else ""
                doomed = [p for p in fs if p.startswith(prefix)] if opaque else [p for p in fs if p == target or p.startswith(prefix)]
                for p in doomed:
                    del fs[p]
            for e in layer_entries:
                if is_whiteout(e.path):
                    continue
                if e.type == "h" and not e.sha256:
                    tgt = fs.get(norm(e.linkname))
                    if tgt is not None:
                        e.sha256 = tgt.sha256
                if e.type != "d" and e.path in fs and fs[e.path].type == "d":
                    pre = e.path + "/"
                    for p in [p for p in fs if p.startswith(pre)]:
                        del fs[p]
                fs[e.path] = e
        return fs


def write_inventory(fs: dict[str, Entry], path: str) -> None:
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "wt", encoding="utf-8") as fh:
        fh.write(TSV_HEADER + "\n")
        for p in sorted(fs):
            fh.write(fs[p].tsv() + "\n")


def read_inventory(path: str) -> dict[str, dict]:
    opener = gzip.open if path.endswith(".gz") else open
    out = {}
    with opener(path, "rt", encoding="utf-8") as fh:
        cols = fh.readline().rstrip("\n").split("\t")
        for line in fh:
            vals = line.rstrip("\n").split("\t")
            row = dict(zip(cols, vals))
            out["" if row["path"] == "." else row["path"]] = row
    return out
