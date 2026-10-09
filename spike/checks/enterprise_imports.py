#!/usr/bin/env python3
"""Find enterprise imports that are not guarded by ImportError (DESIGN §5.1 check 2).

An import is an *enterprise import* when the module it loads is
``litellm_enterprise``, ``enterprise`` or ``litellm.proxy.enterprise`` (the
upstream symlink to ``enterprise/``), or anything below them. String-literal
``importlib.import_module()`` / ``__import__()`` calls count too.

It is *guarded* when the innermost ``try`` whose body contains it has a handler
that names ``ImportError`` or ``ModuleNotFoundError``. ``except Exception``,
a bare ``except`` and no ``try`` at all are *unguarded*: after the enterprise
code is removed such an import is either caught by a broad handler (the feature
silently degrades) or becomes an error. Imports under ``if TYPE_CHECKING:``
never run and are reported separately.

Usage:
  enterprise_imports.py scan  --root LABEL=DIR [--root ...] [--exclude DIR ...]
  enterprise_imports.py check --root LABEL=DIR [...] --fixture FILE [--summary FILE]

``scan`` prints one tab-separated line per unguarded import:
  path  function  target  handler
``check`` compares those lines with the first four columns of the fixture
(``#`` comments and blank lines ignored; the fifth column is the human-assigned
category) and exits 1 on any difference. Line numbers are deliberately not
recorded: they move with every upstream release.
"""

from __future__ import annotations

import argparse
import ast
import collections
import json
import os
import re
import sys

ENTERPRISE_ROOTS = ("litellm_enterprise", "enterprise", "litellm.proxy.enterprise")
GUARD_NAMES = {"ImportError", "ModuleNotFoundError"}
# Cheap textual pre-filter: a file without this byte string cannot contain a
# matching import (``enterprise`` is a substring of every root above).
NEEDLE = b"enterprise"
# Unparsable files are tolerated only if they show no sign of an enterprise import.
SUSPICIOUS = re.compile(rb"(^|\W)(import|from)\s+(litellm_enterprise|enterprise)\b|litellm\.proxy\.enterprise\b|litellm\.proxy\s+import\s+enterprise\b")


def is_enterprise(module: str) -> bool:
    return any(module == r or module.startswith(r + ".") for r in ENTERPRISE_ROOTS)


def resolve_relative(pkg: str, module: str | None, level: int) -> str:
    parts = pkg.split(".") if pkg else []
    if level > 1:
        parts = parts[: len(parts) - (level - 1)]
    base = ".".join(parts)
    if module:
        return f"{base}.{module}" if base else module
    return base


def handler_names(handler: ast.ExceptHandler) -> list[str]:
    t = handler.type
    if t is None:
        return ["<bare>"]
    elts = t.elts if isinstance(t, ast.Tuple) else [t]
    out = []
    for e in elts:
        if isinstance(e, ast.Name):
            out.append(e.id)
        elif isinstance(e, ast.Attribute):
            out.append(e.attr)
        else:
            out.append(ast.dump(e))
    return out


def is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def scan_tree(tree: ast.AST, pkg: str):
    """Yield (kind, function, target, handler, guarded, lineno) for each enterprise import."""
    results = []

    def visit(node, scope, trys, type_only):
        # trys: innermost-last list of Try nodes whose *body* contains `node`.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for d in node.decorator_list:
                visit(d, scope, trys, type_only)
            inner = scope + [node.name]
            for child in node.body:
                visit(child, inner, trys, type_only)
            return
        if isinstance(node, (ast.Try, getattr(ast, "TryStar", ast.Try))):
            for child in node.body:
                visit(child, scope, trys + [node], type_only)
            for h in node.handlers:
                for child in h.body:
                    visit(child, scope, trys, type_only)
            for child in node.orelse + node.finalbody:
                visit(child, scope, trys, type_only)
            return
        if isinstance(node, ast.If) and is_type_checking(node.test):
            for child in node.body:
                visit(child, scope, trys, True)
            for child in node.orelse:
                visit(child, scope, trys, type_only)
            return

        targets = []
        if isinstance(node, ast.Import):
            targets = [a.name for a in node.names if is_enterprise(a.name)]
        elif isinstance(node, ast.ImportFrom):
            mod = resolve_relative(pkg, node.module, node.level) if node.level else (node.module or "")
            if is_enterprise(mod):
                targets = [mod]
            else:
                # `from litellm.proxy import enterprise`
                targets = [f"{mod}.{a.name}" for a in node.names if is_enterprise(f"{mod}.{a.name}")]
        elif isinstance(node, ast.Call):
            f = node.func
            fname = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
            if fname in ("import_module", "__import__") and node.args:
                a0 = node.args[0]
                if isinstance(a0, ast.Constant) and isinstance(a0.value, str) and is_enterprise(a0.value):
                    targets = [a0.value]

        for t in targets:
            if trys:
                names = sorted({n for h in trys[-1].handlers for n in handler_names(h)})
                guarded = bool(GUARD_NAMES.intersection(names))
                handler = "+".join(names) if names else "<finally-only>"
            else:
                guarded, handler = False, "<none>"
            kind = "dynamic" if isinstance(node, ast.Call) else "import"
            results.append((kind, ".".join(scope) or "<module>", t, handler, guarded, type_only, node.lineno))

        for child in ast.iter_child_nodes(node):
            visit(child, scope, trys, type_only)

    for child in tree.body if isinstance(tree, ast.Module) else [tree]:
        visit(child, [], [], False)
    return results


def iter_py(root: str, excludes: list[str]):
    ex = [os.path.realpath(e) for e in excludes]
    for dirpath, dirnames, filenames in os.walk(root):
        real = os.path.realpath(dirpath)
        if any(real == e or real.startswith(e + os.sep) for e in ex):
            dirnames[:] = []
            continue
        dirnames.sort()
        for fn in sorted(filenames):
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def module_package(relpath: str) -> str:
    """Package that relative imports in this file resolve against (a/b/c.py and a/b/__init__.py -> a.b)."""
    return ".".join(relpath[:-3].split("/")[:-1])


def scan(roots: list[tuple[str, str]], excludes: list[str]):
    rows, stats = [], collections.Counter()
    problems = []
    for label, root in roots:
        for path in iter_py(root, excludes):
            stats["py_files"] += 1
            with open(path, "rb") as fh:
                src = fh.read()
            if NEEDLE not in src:
                continue
            stats["py_files_with_needle"] += 1
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            try:
                tree = ast.parse(src, filename=path)
            except (SyntaxError, ValueError) as e:
                stats["unparsable"] += 1
                if SUSPICIOUS.search(src):
                    problems.append(f"unparsable file with an enterprise import: {label}/{rel}: {e}")
                continue
            for kind, func, target, handler, guarded, type_only, lineno in scan_tree(tree, module_package(rel)):
                stats["enterprise_imports"] += 1
                if type_only:
                    stats["type_checking_only"] += 1
                elif guarded:
                    stats["guarded_importerror"] += 1
                else:
                    stats["unguarded"] += 1
                    rows.append((f"{label}/{rel}", func, target, handler, kind, lineno))
    rows.sort()
    return rows, stats, problems


def read_fixture(path: str):
    out = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            cols = line.split("\t")
            if len(cols) < 5 or cols[4] not in ("degrade", "error"):
                raise SystemExit(f"bad fixture line (need 5 tab-separated columns, category degrade|error): {line!r}")
            out.append(tuple(cols[:5]))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["scan", "check"])
    ap.add_argument("--root", action="append", required=True, help="LABEL=DIR")
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--fixture")
    ap.add_argument("--summary", help="write a JSON summary here")
    a = ap.parse_args(argv)
    roots = []
    for r in a.root:
        label, _, d = r.partition("=")
        if not d or not os.path.isdir(d):
            raise SystemExit(f"--root {r!r}: not LABEL=DIR")
        roots.append((label, d))

    rows, stats, problems = scan(roots, a.exclude)
    found = [r[:4] for r in rows]
    summary = {"stats": dict(stats), "unguarded": [dict(zip(("path", "function", "target", "handler", "kind", "line"), r)) for r in rows], "problems": problems}

    rc = 0
    if a.mode == "scan":
        for r in rows:
            print("\t".join(map(str, r[:4])) + f"\t# {r[4]} line {r[5]}")
    else:
        if not a.fixture:
            raise SystemExit("check needs --fixture")
        fx = read_fixture(a.fixture)
        want = collections.Counter(f[:4] for f in fx)
        got = collections.Counter(found)
        missing = sorted((want - got).elements())
        extra = sorted((got - want).elements())
        summary["fixture_entries"] = len(fx)
        summary["fixture_categories"] = dict(collections.Counter(f[4] for f in fx))
        summary["missing_vs_fixture"] = [list(m) for m in missing]
        summary["new_vs_fixture"] = [list(e) for e in extra]
        for m in missing:
            print("MISSING (in fixture, not found):", "\t".join(m))
        for e in extra:
            print("NEW (found, not in fixture):    ", "\t".join(e))
        if missing or extra:
            rc = 1
        print(f"unguarded found={len(found)} fixture={len(fx)} -> {'PASS' if rc == 0 else 'FAIL'}")
    for p in problems:
        print("PROBLEM:", p)
        rc = 1
    print("stats:", json.dumps(dict(stats), sort_keys=True), file=sys.stderr)
    if a.summary:
        with open(a.summary, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=1, sort_keys=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
