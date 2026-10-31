"""Phase 4: ProjectContextIndexer + ArchitectureModel.

Walks a project directory, detects the git root, manifests and entry
points, groups code into components (top-level modules/packages) and
derives connections between components from import statements.
Every element keeps a link back to its source files (TZ 4.2).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".idea",
    ".vscode", "dist", "build", "target", ".dart_tool", ".next",
    "vendor", "Pods", ".muse", "data", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", "htmlcov",
}
CODE_EXT = {
    ".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".js": "JavaScript", ".jsx": "JavaScript", ".rs": "Rust",
    ".go": "Go", ".dart": "Dart", ".java": "Java", ".kt": "Kotlin",
    ".c": "C", ".h": "C", ".cpp": "C++", ".md": "Docs",
    ".toml": "Config", ".json": "Config", ".yaml": "Config", ".yml": "Config",
}
MANIFESTS = [
    "package.json", "pyproject.toml", "Cargo.toml", "pubspec.yaml",
    "go.mod", "requirements.txt", "pom.xml", "build.gradle",
]
ENTRY_CANDIDATES = [
    "main.py", "src/main.py", "index.ts", "src/index.ts", "src/main.ts",
    "main.rs", "src/main.rs", "main.go", "lib/main.dart", "app.py",
    "src/app.py", "server.py", "main.js", "src/main.js",
]

IMPORT_PY = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", re.M)
IMPORT_JS = re.compile(r"""(?:from\s+|require\(|import\s*\()\s*['"]([^'"]+)['"]""")


def _git_root(path: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


def _read(path: str, limit: int = 4000) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except Exception:
        return ""


def _readme_summary(root: str) -> str:
    for name in ("README.md", "README.rst", "README.txt", "README"):
        p = os.path.join(root, name)
        if os.path.isfile(p):
            text = _read(p, 3000)
            lines = [ln.strip("# ").strip() for ln in text.splitlines()]
            lines = [ln for ln in lines if ln and not ln.startswith("![")]
            # Avoid "Pulseboard Pulseboard — ..." when the heading is
            # repeated at the start of the first paragraph.
            if len(lines) > 1 and lines[1].startswith(lines[0]):
                lines = lines[1:]
            return " ".join(lines[:3])[:400]
    return ""


def _docstring_of(dirpath: str) -> str:
    """First module docstring in the component: entry files first,
    then any other source file — descriptions should read as sentences,
    not as file statistics."""
    if not os.path.isdir(dirpath):
        return ""
    names = sorted(os.listdir(dirpath))
    names.sort(key=lambda n: 0 if n == "__init__.py"
               or n in ENTRY_CANDIDATES else 1)
    for fname in names:
        fpath = os.path.join(dirpath, fname)
        if not os.path.isfile(fpath):
            continue
        text = _read(fpath, 2500)
        m = re.search(r'"""(.+?)"""', text, re.S)
        if m:
            return " ".join(m.group(1).split())[:170]
        m = re.search(r"//\s*(.+)", text)
        if m and fname.endswith((".ts", ".js", ".rs", ".go")):
            return m.group(1).strip()[:160]
    return ""


def index_project(path: str) -> dict:
    root = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(root):
        raise FileNotFoundError(f"not a directory: {root}")
    git_root = _git_root(root) or root

    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.startswith(".") and fn not in MANIFESTS:
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), root)
            files.append(rel)
        if len(files) > 8000:
            break

    languages: dict[str, int] = {}
    for rel in files:
        lang = CODE_EXT.get(os.path.splitext(rel)[1].lower())
        if lang:
            languages[lang] = languages.get(lang, 0) + 1

    # Components = top-level directories containing code (or loose root files).
    comp_files: dict[str, list[str]] = {}
    for rel in files:
        parts = rel.split(os.sep)
        comp = parts[0] if len(parts) > 1 else "(root)"
        if os.path.splitext(rel)[1].lower() in CODE_EXT:
            comp_files.setdefault(comp, []).append(rel)

    # "(root)" only counts as a component when it holds real code,
    # not just README/config files.
    if "(root)" in comp_files:
        real = [f for f in comp_files["(root)"]
                if CODE_EXT.get(os.path.splitext(f)[1].lower())
                not in ("Docs", "Config", None)]
        if not real:
            del comp_files["(root)"]

    components = []
    name_to_id: dict[str, str] = {}
    for name in sorted(comp_files):
        cid = "c_" + hashlib.sha1(name.encode()).hexdigest()[:8]
        name_to_id[name] = cid
        fl = sorted(comp_files[name])
        langs: dict[str, int] = {}
        for rel in fl:
            lang = CODE_EXT.get(os.path.splitext(rel)[1].lower())
            if lang:
                langs[lang] = langs.get(lang, 0) + 1
        cpath = os.path.join(root, name) if name != "(root)" else root
        desc = _docstring_of(cpath) if name != "(root)" else ""
        if not desc:
            top = ", ".join(k for k, _ in sorted(langs.items(), key=lambda x: -x[1])[:2])
            desc = f"{len(fl)} files" + (f" · {top}" if top else "")
        components.append({
            "id": cid, "name": name, "path": name,
            "files": fl[:60], "file_count": len(fl),
            "languages": langs, "description": desc,
            "connections": [],
        })

    # Connections from imports between components.
    edges: set[tuple[str, str]] = set()
    for comp in components:
        src_names = {comp["name"]}
        for rel in comp_files[comp["name"]]:
            text = _read(os.path.join(root, rel), 20000)
            targets: set[str] = set()
            for mod in IMPORT_PY.findall(text):
                targets.add(mod.split(".")[0])
            for imp in IMPORT_JS.findall(text):
                if imp.startswith("."):
                    resolved = os.path.normpath(
                        os.path.join(os.path.dirname(rel), imp)).split(os.sep)
                    if resolved and resolved[0] != "..":
                        targets.add(resolved[0])
                else:
                    targets.add(imp.split("/")[0])
            for t in targets:
                if t in name_to_id and t not in src_names:
                    edges.add((comp["name"], t))
    for a, b in sorted(edges):
        for comp in components:
            if comp["name"] == a and name_to_id[b] not in comp["connections"]:
                comp["connections"].append(name_to_id[b])

    file_to_component = {}
    for comp in components:
        for rel in comp_files[comp["name"]]:
            file_to_component[rel] = comp["id"]

    manifests = [m for m in MANIFESTS if os.path.isfile(os.path.join(root, m))]
    manifest_data = {}
    for m in manifests:
        if m == "package.json":
            try:
                manifest_data[m] = json.loads(_read(os.path.join(root, m), 8000))
            except Exception:
                pass

    entries = [e for e in ENTRY_CANDIDATES
               if os.path.isfile(os.path.join(root, e))]

    proj_name = manifest_data.get("package.json", {}).get("name")
    if not proj_name and os.path.isfile(os.path.join(root, "pyproject.toml")):
        m = re.search(r'^\s*name\s*=\s*"([^"]+)"',
                      _read(os.path.join(root, "pyproject.toml"), 4000), re.M)
        proj_name = m.group(1) if m else None
    if not proj_name and os.path.isfile(os.path.join(root, "Cargo.toml")):
        m = re.search(r'^\s*name\s*=\s*"([^"]+)"',
                      _read(os.path.join(root, "Cargo.toml"), 4000), re.M)
        proj_name = m.group(1) if m else None

    return {
        "id": hashlib.sha1(root.encode()).hexdigest()[:12],
        "root": root, "git_root": git_root,
        "name": proj_name or os.path.basename(root),
        "summary": _readme_summary(root),
        "languages": languages, "total_files": len(files),
        "components": components, "file_to_component": file_to_component,
        "manifests": manifests, "entry_points": entries,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "project_indexer",
    }


def component_for_file(project: dict | None, file_path: str) -> dict | None:
    if not project or not file_path:
        return None
    root = project["root"]
    rel = file_path
    if os.path.isabs(file_path):
        try:
            rel = os.path.relpath(file_path, root)
        except Exception:
            return None
    cid = project.get("file_to_component", {}).get(rel)
    if not cid and rel.split(os.sep):
        top = rel.split(os.sep)[0]
        for comp in project["components"]:
            if comp["name"] == top:
                cid = comp["id"]
    for comp in project["components"]:
        if comp["id"] == cid:
            return comp
    return None
