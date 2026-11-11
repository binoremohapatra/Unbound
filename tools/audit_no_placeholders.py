"""audit_no_placeholders.py - scan production sources for placeholder markers.

Python files: AST walk flags functions whose body is ONLY `pass`, `...`, or
`raise NotImplementedError` (bare stubs). Non-Python files: regex for
TODO/FIXME/XXX/NotImplemented/lorem markers.

  python tools/audit_no_placeholders.py [--root DIR]
Exit 0 = clean, 1 = violations found.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCAN_DIRS = [
    "host", "firmware", "protocol", "tools",
    "adapters", "core", "handsfree", "dashboard",
]
SCAN_FILES = [
    "main.py", "gateway.py", "state_machine.py", "watchdog.py", "doctor.py",
    "elevation.py", "single_instance.py", "logsetup.py", "tray.py",
    "config.py", "version.py", "omniband.py",
]
SKIP_DIRS = {"tests", "test", "__pycache__", "build", "node_modules",
             ".git", "my_project(Unbound)"}

RE_NONPY = re.compile(r"TODO|FIXME|XXX|NotImplemented|lorem", re.IGNORECASE)
RE_PY_COMMENT = re.compile(r"TODO|FIXME|XXX|lorem", re.IGNORECASE)
SELF = "tools/audit_no_placeholders.py"


def is_stub_body(stmts: list) -> bool:
    if len(stmts) != 1:
        return False
    s = stmts[0]
    if isinstance(s, ast.Pass):
        return True
    if isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) \
            and s.value.value is Ellipsis:
        return True
    if isinstance(s, ast.Raise) and s.exc is not None:
        name = ""
        if isinstance(s.exc, ast.Name):
            name = s.exc.id
        elif isinstance(s.exc, ast.Call) and isinstance(s.exc.func, ast.Name):
            name = s.exc.func.id
        return "NotImplemented" in name
    return False


def scan_py(path: Path) -> list[str]:
    hits = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return hits
    protocol_methods: set[int] = set()
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        bases = []
        for b in cls.bases:
            if isinstance(b, ast.Name):
                bases.append(b.id)
            elif isinstance(b, ast.Attribute):
                bases.append(b.attr)
        if any(x in ("Protocol", "ABC") for x in bases):
            for item in cls.body:
                if isinstance(item, (ast.FunctionDef,
                                     ast.AsyncFunctionDef)):
                    protocol_methods.add(id(item))
    for node in ast.walk(tree):
        if id(node) in protocol_methods:
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(isinstance(d, ast.Name) and d.id == "abstractmethod"
                   for d in node.decorator_list):
                continue
            if is_stub_body(node.body):
                hits.append(f"{node.lineno}: stub function '{node.name}'")
        elif isinstance(node, ast.ClassDef) and is_stub_body(node.body):
            hits.append(f"{node.lineno}: stub class '{node.name}'")
    text = path.read_text(encoding="utf-8", errors="replace")
    for i, line in enumerate(text.splitlines(), 1):
        if RE_PY_COMMENT.search(line):
            hits.append(f"{i}: {line.strip()[:100]}")
    return hits


def scan_text(path: Path) -> list[str]:
    hits = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for i, line in enumerate(text.splitlines(), 1):
        if RE_NONPY.search(line):
            hits.append(f"{i}: {line.strip()[:100]}")
    return hits


def iter_files(root: Path):
    for d in SCAN_DIRS:
        p = root / d
        if p.is_dir():
            for f in p.rglob("*"):
                if f.is_file() and not any(part in SKIP_DIRS
                                           for part in f.parts):
                    yield f
    for name in SCAN_FILES:
        f = root / name
        if f.is_file():
            yield f


def main() -> int:
    root = ROOT
    if "--root" in sys.argv:
        root = Path(sys.argv[sys.argv.index("--root") + 1])
    violations = []
    for f in iter_files(root):
        rel = f.relative_to(root).as_posix()
        if rel == SELF or f.suffix not in {".py", ".h", ".cpp", ".ino",
                                           ".md", ".json"}:
            continue
        hits = scan_py(f) if f.suffix == ".py" else scan_text(f)
        for h in hits:
            violations.append(f"{rel}:{h}")
    if violations:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(f"PLACEHOLDER AUDIT FAILED - {len(violations)} hit(s):")
        for v in violations:
            print(" ", v)
        return 1
    print("placeholder audit clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
