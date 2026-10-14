"""Report restore write-through failures in the pinned Woow plugin.

Keep the canonical copy/lock intact in git. Apply this addon-local delta after
COPY rootfs, just like the existing pinned Responses compatibility patch.
"""
import hashlib
from pathlib import Path
import sys

SOURCE_SHA256 = "388f5d8618790eff55f5ab5aa32b84b02594445616491e6b2c00ddd0ca1e1af6"
EDITS = (
    (
        '''    kept, restored = [], []
    try:
        stored = db_accounts()
''',
        '''    kept, restored = [], []
    persistence_failed = False
    try:
        stored = db_accounts()
''',
    ),
    (
        '''        elif _has_tokens(record):
            _db_save_logged(account, record)
            kept.append(account)
''',
        '''        elif _has_tokens(record):
            try:
                db_save(account, record)
            except Exception:  # noqa: BLE001 — keep the newest local tokens on failure
                persistence_failed = True
                _log("WARNING ChatGPT restore persistence failed: db_save_failed")
            # Kept means the local file is preserved, even if its DB copy failed.
            kept.append(account)
''',
    ),
    (
        '''    state = "restored" if restored else ("kept" if kept else "none")
    return {"ok": True, "state": state, "connected": bool(kept or restored), "kept": kept, "restored": restored}
''',
        '''    if persistence_failed:
        return {"ok": False, "state": "error", "error": "db_save_failed",
                "connected": bool(kept or restored), "kept": kept, "restored": restored}
    state = "restored" if restored else ("kept" if kept else "none")
    return {"ok": True, "state": state, "connected": bool(kept or restored), "kept": kept, "restored": restored}
''',
    ),
)


def apply(root: Path) -> str:
    path = root / "woow_chatgpt.py"
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA256:
        raise SystemExit("Refusing changed Woow restore source")
    result = raw.decode("utf-8")
    for before, after in EDITS:
        if result.count(before) != 1:
            raise SystemExit("Refusing changed Woow restore context")
        result = result.replace(before, after)
    compile(result, str(path), "exec")
    output = result.encode("utf-8")
    path.write_bytes(output)
    return hashlib.sha256(output).hexdigest()


if __name__ == "__main__":
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/usr/share/woow-litellm/py")
    print("woow_chatgpt.py: " + apply(root))
