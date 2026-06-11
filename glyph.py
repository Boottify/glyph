#!/usr/bin/env python3
"""Glyph — fast incremental codebase knowledge graph indexer.

tree-sitter AST extraction + SQLite storage + inotify-based dirty tracking.
One DB, many projects. Zero LLM cost. Sub-100ms incremental updates.
"""

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

VERSION = "1.1.0"

from tree_sitter import Language, Parser, Node

# ═══════════════════════════════════════════════════════════════════
# LANGUAGE LOADING
# ═══════════════════════════════════════════════════════════════════

# tree-sitter 0.21+ language bindings
try:
    import tree_sitter_typescript as tst
    TS_LANG = (Language(tst.language_typescript()), Language(tst.language_tsx()))
except Exception:
    TS_LANG = (None, None)

try:
    import tree_sitter_python as tsp
    PY_LANG = Language(tsp.language())
except Exception:
    PY_LANG = None

try:
    import tree_sitter_go as tsg
    GO_LANG = Language(tsg.language())
except Exception:
    GO_LANG = None

try:
    import tree_sitter_bash as tsb
    BASH_LANG = Language(tsb.language())
except Exception:
    BASH_LANG = None

EXT_LANG = {}
if TS_LANG[0]:
    EXT_LANG.update({".ts": ("typescript", TS_LANG[0]), ".tsx": ("tsx", TS_LANG[1])})
if PY_LANG:
    EXT_LANG[".py"] = ("python", PY_LANG)
if GO_LANG:
    EXT_LANG[".go"] = ("go", GO_LANG)
if BASH_LANG:
    EXT_LANG[".sh"] = ("bash", BASH_LANG)

# We also parse .js/.jsx/.mjs with TS parser (close enough)
if TS_LANG[0]:
    EXT_LANG.update({".js": ("javascript", TS_LANG[0]), ".jsx": ("jsx", TS_LANG[1]),
                     ".mjs": ("javascript", TS_LANG[0]), ".cjs": ("javascript", TS_LANG[0])})

IGNORE_PATTERNS = [
    "node_modules", ".next", "dist", "build", ".git", "__pycache__",
    ".venv", "venv", ".turbo", "coverage", ".cache", "public/assets",
    "generated", ".generated", "graphify-out",
]
IGNORE_EXTS = {".png", ".jpg", ".svg", ".ico", ".woff", ".woff2",
               ".ttf", ".eot", ".lock", ".map", ".d.ts", ".min.js",
               ".min.css", ".sqlite", ".sqlite3", ".db", ".bin"}


def should_ignore(path: str) -> bool:
    parts = Path(path).parts
    for p in parts:
        if p in IGNORE_PATTERNS:
            return True
    ext = Path(path).suffix.lower()
    return ext in IGNORE_EXTS


# ═══════════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════════

DB_PATH = os.path.expanduser("~/.glyph/glyph.db")


def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=5000")
    return db


def init_schema():
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        path TEXT UNIQUE NOT NULL,
        last_scan INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        path TEXT NOT NULL,
        hash TEXT,
        last_parsed INTEGER DEFAULT 0,
        FOREIGN KEY (project_id) REFERENCES projects(id),
        UNIQUE(project_id, path)
    );
    CREATE TABLE IF NOT EXISTS symbols (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        file_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        kind TEXT NOT NULL,
        line INTEGER DEFAULT 0,
        exported INTEGER DEFAULT 0,
        FOREIGN KEY (project_id) REFERENCES projects(id),
        FOREIGN KEY (file_id) REFERENCES files(id)
    );
    CREATE TABLE IF NOT EXISTS edges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER,
        target_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        project_id INTEGER NOT NULL,
        FOREIGN KEY (target_id) REFERENCES symbols(id),
        FOREIGN KEY (project_id) REFERENCES projects(id)
    );
    CREATE INDEX IF NOT EXISTS idx_symbols_lookup ON symbols(project_id, name, kind);
    CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_id);
    CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(project_id, kind);
    CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(source_id);
    CREATE INDEX IF NOT EXISTS idx_edges_tgt ON edges(target_id);
    CREATE INDEX IF NOT EXISTS idx_files_project ON files(project_id);

    -- v1.1: Knowledge graph extensions
    CREATE TABLE IF NOT EXISTS file_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id INTEGER NOT NULL,
        project_id INTEGER NOT NULL,
        old_hash TEXT,
        new_hash TEXT NOT NULL,
        commit_hash TEXT NOT NULL,
        commit_msg TEXT,
        author TEXT,
        committed_at INTEGER NOT NULL,
        change_type TEXT,
        lines_added INTEGER,
        lines_removed INTEGER,
        summary TEXT,
        FOREIGN KEY (file_id) REFERENCES files(id),
        FOREIGN KEY (project_id) REFERENCES projects(id)
    );
    CREATE INDEX IF NOT EXISTS idx_file_history_file
        ON file_history(file_id, committed_at);

    CREATE TABLE IF NOT EXISTS descriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_type TEXT NOT NULL,
        target_id INTEGER NOT NULL,
        project_id INTEGER NOT NULL,
        description TEXT NOT NULL,
        detail TEXT,
        generated_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
        model TEXT,
        source_session_id TEXT,
        confidence REAL DEFAULT 1.0,
        version_hash TEXT,
        FOREIGN KEY (project_id) REFERENCES projects(id)
    );
    CREATE INDEX IF NOT EXISTS idx_descriptions_target
        ON descriptions(target_type, target_id);

    CREATE TABLE IF NOT EXISTS session_refs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        project_id INTEGER NOT NULL,
        file_id INTEGER,
        symbol_id INTEGER,
        ref_type TEXT NOT NULL,
        summary TEXT,
        message_count INTEGER,
        first_at REAL,
        last_at REAL,
        FOREIGN KEY (project_id) REFERENCES projects(id)
    );
    CREATE INDEX IF NOT EXISTS idx_session_refs_file ON session_refs(file_id);
    CREATE INDEX IF NOT EXISTS idx_session_refs_symbol ON session_refs(symbol_id);
    """)
    db.commit()
    db.close()


# ═══════════════════════════════════════════════════════════════════
# PARSERS
# ═══════════════════════════════════════════════════════════════════

EXPORT_PATTERN = re.compile(r'\bexport\s+(default\s+)?')

# ── TypeScript/JavaScript ─────────────────────────────────────────

def parse_ts(file_id: int, project_id: int, source: bytes, lang: str,
             symbols: list, edges: list):
    """Extract symbols and edges from a TypeScript/JavaScript AST."""
    try:
        parser = Parser(TS_LANG[1] if lang in ("tsx", "jsx") else TS_LANG[0])
        tree = parser.parse(source)
        code = source.decode("utf-8", errors="replace")
        _walk_ts(tree.root_node, file_id, project_id, code, symbols, edges)
    except Exception:
        pass  # Skip files that crash the parser


def _text(node: Node, code: str) -> str:
    return code[node.start_byte:node.end_byte]


def _is_exported(node: Node, code: str) -> bool:
    """Check if a node has export modifier or parent is export_statement."""
    if node.type == "export_statement":
        return True
    for child in node.children:
        if child.type == "export":
            return True
    # Check parent for export_statement
    parent = node.parent
    if parent and parent.type == "export_statement":
        return True
    # Check if declaration is preceded by 'export' keyword
    text_before = code[max(0, node.start_byte - 30):node.start_byte]
    return bool(EXPORT_PATTERN.search(text_before))


def _walk_ts(node: Node, file_id: int, project_id: int, code: str,
             symbols: list, edges: list):
    """Recursively walk TypeScript AST extracting symbols and edges."""
    # Symbol declarations
    if node.type in ("function_declaration", "generator_function_declaration"):
        name_node = node.child_by_field_name("name")
        if name_node:
            name = _text(name_node, code)
            exported = 1 if _is_exported(node, code) else 0
            symbols.append((project_id, file_id, name, "function",
                          node.start_point[0] + 1, exported))

    elif node.type == "class_declaration":
        name_node = node.child_by_field_name("name")
        if name_node:
            name = _text(name_node, code)
            exported = 1 if _is_exported(node, code) else 0
            symbols.append((project_id, file_id, name, "class",
                          node.start_point[0] + 1, exported))

    elif node.type == "method_definition":
        name_node = node.child_by_field_name("name")
        if name_node:
            name = _text(name_node, code)
            symbols.append((project_id, file_id, name, "method",
                          node.start_point[0] + 1, 0))

    elif node.type in ("variable_declaration", "lexical_declaration"):
        exported = 1 if _is_exported(node, code) else 0
        for child in node.children:
            if child.type == "variable_declarator":
                name_node = child.child_by_field_name("name")
                if name_node:
                    decl_name = _text(name_node, code)
                    # Determine kind
                    parent_text = _text(node, code)
                    if parent_text.startswith("const"):
                        kind = "const"
                    elif parent_text.startswith("let"):
                        kind = "let"
                    elif parent_text.startswith("var"):
                        kind = "var"
                    else:
                        kind = "const"
                    # Detect React components (capital first letter + returns JSX)
                    if kind == "const" and decl_name[0].isupper():
                        value_node = child.child_by_field_name("value")
                        if value_node and ("<" in _text(value_node, code)[:80] or
                            (_text(value_node, code)[:10] in ("React.", "forward"))):
                            kind = "component"
                    symbols.append((project_id, file_id, decl_name, kind,
                                  node.start_point[0] + 1, exported))

    elif node.type == "type_alias_declaration":
        name_node = node.child_by_field_name("name")
        if name_node:
            symbols.append((project_id, file_id, _text(name_node, code), "type",
                          node.start_point[0] + 1, 1))

    elif node.type == "interface_declaration":
        name_node = node.child_by_field_name("name")
        if name_node:
            symbols.append((project_id, file_id, _text(name_node, code), "interface",
                          node.start_point[0] + 1, 1))

    elif node.type == "enum_declaration":
        name_node = node.child_by_field_name("name")
        if name_node:
            symbols.append((project_id, file_id, _text(name_node, code), "enum",
                          node.start_point[0] + 1, 1))

    # Import edges
    elif node.type == "import_statement":
        source_node = node.child_by_field_name("source")
        if source_node:
            module_path = _text(source_node, code).strip("'\"")
            # Extract imported names
            clause = node.child_by_field_name("clause")
            if clause:
                _extract_ts_imports(clause, project_id, module_path, code, edges)

    # Call expressions
    elif node.type == "call_expression":
        func = node.child_by_field_name("function")
        if func:
            if func.type == "identifier":
                callee = _text(func, code)
                edges.append((file_id, project_id, callee, "call"))

    # JSX elements (component usage)
    elif node.type == "jsx_self_closing_element" or node.type == "jsx_opening_element":
        name_el = node.child_by_field_name("name")
        if name_el:
            tag = _text(name_el, code)
            if tag and tag[0].isupper():  # Likely a React component
                edges.append((file_id, project_id, tag, "jsx_use"))

    # Recursively walk children
    for child in node.children:
        _walk_ts(child, file_id, project_id, code, symbols, edges)


def _extract_ts_imports(clause: Node, project_id: int, module_path: str,
                         code: str, edges: list):
    """Extract individual imports from an import clause."""
    if clause.type == "named_imports":
        for spec in clause.children:
            if spec.type == "import_specifier":
                name_node = spec.child_by_field_name("name")
                if name_node:
                    edges.append((0, project_id, _text(name_node, code), "import"))
    elif clause.type == "identifier":
        edges.append((0, project_id, _text(clause, code), "import"))


# ── Python ─────────────────────────────────────────────────────────

def parse_py(file_id: int, project_id: int, source: bytes,
             symbols: list, edges: list):
    try:
        parser = Parser(PY_LANG)
        tree = parser.parse(source)
        code = source.decode("utf-8", errors="replace")
        _walk_py(tree.root_node, file_id, project_id, code, symbols, edges)
    except Exception:
        pass


def _walk_py(node: Node, file_id: int, project_id: int, code: str,
             symbols: list, edges: list):
    if node.type == "function_definition":
        name_node = node.child_by_field_name("name")
        if name_node:
            symbols.append((project_id, file_id, _text(name_node, code),
                          "function", node.start_point[0] + 1, 0))
    elif node.type == "class_definition":
        name_node = node.child_by_field_name("name")
        if name_node:
            symbols.append((project_id, file_id, _text(name_node, code),
                          "class", node.start_point[0] + 1, 0))
    elif node.type == "import_statement" or node.type == "import_from_statement":
        _extract_py_imports(node, project_id, code, edges)
    elif node.type == "call":
        func = node.child_by_field_name("function")
        if func and func.type == "identifier":
            edges.append((file_id, project_id, _text(func, code), "call"))
    for child in node.children:
        _walk_py(child, file_id, project_id, code, symbols, edges)


def _extract_py_imports(node: Node, project_id: int, code: str, edges: list):
    names = node.child_by_field_name("name")
    if names:
        if names.type == "dotted_name":
            parts = _text(names, code).split(".")
            if parts:
                edges.append((0, project_id, parts[0], "import"))
        elif names.type == "aliased_import":
            name_node = names.child_by_field_name("name")
            if name_node:
                edges.append((0, project_id, _text(name_node, code), "import"))


# ── Go ─────────────────────────────────────────────────────────────

def parse_go(file_id: int, project_id: int, source: bytes,
             symbols: list, edges: list):
    try:
        parser = Parser(GO_LANG)
        tree = parser.parse(source)
        code = source.decode("utf-8", errors="replace")
        _walk_go(tree.root_node, file_id, project_id, code, symbols, edges)
    except Exception:
        pass


def _walk_go(node: Node, file_id: int, project_id: int, code: str,
             symbols: list, edges: list):
    if node.type == "function_declaration":
        name_node = node.child_by_field_name("name")
        if name_node:
            symbols.append((project_id, file_id, _text(name_node, code),
                          "function", node.start_point[0] + 1, 0))
    elif node.type == "method_declaration":
        name_node = node.child_by_field_name("name")
        if name_node:
            symbols.append((project_id, file_id, _text(name_node, code),
                          "method", node.start_point[0] + 1, 0))
    elif node.type == "type_declaration":
        for child in node.children:
            if child.type == "type_spec":
                n = child.child_by_field_name("name")
                if n:
                    symbols.append((project_id, file_id, _text(n, code),
                                  "type", child.start_point[0] + 1, 0))
    elif node.type == "import_declaration":
        for child in node.children:
            if child.type == "import_spec":
                p = child.child_by_field_name("path")
                if p:
                    path_val = _text(p, code).strip('"')
                    mod = path_val.rsplit("/", 1)[-1]
                    edges.append((0, project_id, mod, "import"))
    elif node.type == "call_expression":
        func = node.child_by_field_name("function")
        if func and func.type == "identifier":
            edges.append((file_id, project_id, _text(func, code), "call"))
    for child in node.children:
        _walk_go(child, file_id, project_id, code, symbols, edges)


# ── Bash ───────────────────────────────────────────────────────────

def parse_bash(file_id: int, project_id: int, source: bytes,
               symbols: list, edges: list):
    try:
        parser = Parser(BASH_LANG)
        tree = parser.parse(source)
        code = source.decode("utf-8", errors="replace")
        _walk_bash(tree.root_node, file_id, project_id, code, symbols, edges)
    except Exception:
        pass


def _walk_bash(node: Node, file_id: int, project_id: int, code: str,
               symbols: list, edges: list):
    if node.type == "function_definition":
        name_node = node.child_by_field_name("name")
        if name_node:
            symbols.append((project_id, file_id, _text(name_node, code),
                          "function", node.start_point[0] + 1, 0))
    for child in node.children:
        _walk_bash(child, file_id, project_id, code, symbols, edges)


# ═══════════════════════════════════════════════════════════════════
# INDEXER
# ═══════════════════════════════════════════════════════════════════

def hash_file(path: str) -> str:
    """Fast MD5 hash of file contents."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_project(name: str, root: str, full: bool = False):
    """Index all source files in a project directory.

    Args:
        name: Project name (unique key)
        root: Root directory path
        full: If True, re-parse all files even if hash unchanged
    """
    init_schema()
    db = get_db()
    cur = db.cursor()

    # Register or get project
    cur.execute("INSERT OR IGNORE INTO projects (name, path) VALUES (?, ?)",
                (name, root))
    cur.execute("SELECT id FROM projects WHERE name = ?", (name,))
    project_id = cur.fetchone()[0]

    # Walk filesystem
    files_found = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Filter ignored directories in-place
        rel_dir = os.path.relpath(dirpath, root)
        if should_ignore(rel_dir):
            dirnames.clear()
            continue
        # Prune ignored dirs
        dirnames[:] = [d for d in dirnames
                       if d not in IGNORE_PATTERNS and not d.startswith(".")]

        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            rel = os.path.relpath(fpath, root)
            ext = os.path.splitext(fname)[1].lower()
            if ext in IGNORE_EXTS or should_ignore(rel):
                continue
            lang_info = EXT_LANG.get(ext)
            if lang_info:
                files_found.append((rel, fpath, lang_info[0], lang_info[1]))

    # Get existing files for diff
    cur.execute("SELECT path, hash FROM files WHERE project_id = ?", (project_id,))
    existing = {row[0]: row[1] for row in cur.fetchall()}

    # Separate new/changed vs unchanged
    to_parse = []
    skipped = 0
    for rel, abspath, lang_name, lang in files_found:
        h = hash_file(abspath)
        existing_hash = existing.get(rel)
        if not full and existing_hash == h:
            skipped += 1
            continue
        to_parse.append((rel, abspath, lang_name, lang, h))

    if not to_parse:
        print(f"[glyph] {name}: {skipped} files unchanged, nothing to parse")
        cur.execute("UPDATE projects SET last_scan = ? WHERE id = ?",
                   (int(time.time()), project_id))
        db.commit()
        db.close()
        return {"project": name, "total": skipped + len(files_found),
                "parsed": 0, "skipped": skipped, "symbols": 0, "edges": 0}

    # Remove stale files
    current_paths = {rel for rel, _, _, _, _ in to_parse}
    # Also keep existing files that are still on disk
    for rel in existing:
        if rel in {r for r, _, _, _ in files_found}:
            current_paths.add(rel)

    # Remove stale files — clean edges/symbols first to avoid FK violations
    placeholders = ",".join("?" for _ in current_paths)
    if not current_paths:
        # No files found — wipe everything for this project
        cur.execute("DELETE FROM edges WHERE project_id = ?", (project_id,))
        cur.execute("DELETE FROM symbols WHERE project_id = ?", (project_id,))
        cur.execute("DELETE FROM files WHERE project_id = ?", (project_id,))
    else:
        # Delete edges referencing symbols in stale files (target_id FK)
        cur.execute(f"""DELETE FROM edges WHERE target_id IN
            (SELECT s.id FROM symbols s JOIN files f ON s.file_id = f.id
             WHERE f.project_id = ? AND f.path NOT IN ({placeholders}))
        """, [project_id, *current_paths])
        # Delete edges where source is in stale files
        cur.execute(f"""DELETE FROM edges WHERE source_id IN
            (SELECT s.id FROM symbols s JOIN files f ON s.file_id = f.id
             WHERE f.project_id = ? AND f.path NOT IN ({placeholders}))
        """, [project_id, *current_paths])
        # Now delete symbols, then files
        cur.execute(f"""DELETE FROM symbols WHERE file_id IN
            (SELECT id FROM files WHERE project_id = ? AND path NOT IN ({placeholders}))
        """, [project_id, *current_paths])
        cur.execute(f"""DELETE FROM files WHERE project_id = ? AND path NOT IN ({placeholders})""",
                    [project_id, *current_paths])

    # Parse files
    all_symbols = []
    all_edges = []
    parsed_count = 0
    new_count = 0

    for rel, abspath, lang_name, lang, fhash in to_parse:
        try:
            with open(abspath, "rb") as f:
                source = f.read()
            if not source.strip():
                continue
        except Exception:
            continue

        # Upsert file record
        cur.execute("""INSERT INTO files (project_id, path, hash, last_parsed)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(project_id, path) DO UPDATE SET
                       hash=excluded.hash, last_parsed=excluded.last_parsed""",
                   (project_id, rel, fhash, int(time.time())))

        cur.execute("SELECT id FROM files WHERE project_id=? AND path=?",
                   (project_id, rel))
        file_id = cur.fetchone()[0]

        # Remove old symbols/edges for this file (both source and target FKs)
        cur.execute("""DELETE FROM edges WHERE target_id IN
            (SELECT id FROM symbols WHERE file_id=?)""", (file_id,))
        cur.execute("""DELETE FROM edges WHERE source_id IN
            (SELECT id FROM symbols WHERE file_id=?)""", (file_id,))
        cur.execute("DELETE FROM symbols WHERE file_id=?", (file_id,))

        # Parse
        if lang_name in ("typescript", "tsx", "javascript", "jsx"):
            parse_ts(file_id, project_id, source, lang_name, all_symbols, all_edges)
        elif lang_name == "python":
            parse_py(file_id, project_id, source, all_symbols, all_edges)
        elif lang_name == "go":
            parse_go(file_id, project_id, source, all_symbols, all_edges)
        elif lang_name == "bash":
            parse_bash(file_id, project_id, source, all_symbols, all_edges)

        parsed_count += 1
        if parsed_count % 200 == 0:
            print(f"  ... {parsed_count} files parsed ...")

    # Batch insert symbols
    if all_symbols:
        cur.executemany(
            "INSERT INTO symbols (project_id, file_id, name, kind, line, exported) "
            "VALUES (?, ?, ?, ?, ?, ?)", all_symbols)

    # Resolve edges (link import/call/jsx_use edges to symbol IDs)
    resolved_edges = _resolve_edges(cur, project_id, all_edges)

    if resolved_edges:
        cur.executemany(
            "INSERT OR IGNORE INTO edges (source_id, target_id, kind, project_id) "
            "VALUES (?, ?, ?, ?)", resolved_edges)

    # Update timestamp
    cur.execute("UPDATE projects SET last_scan = ? WHERE id = ?",
               (int(time.time()), project_id))
    db.commit()

    # Stats
    cur.execute("SELECT COUNT(*) FROM symbols WHERE project_id=?", (project_id,))
    sym_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM edges WHERE project_id=?", (project_id,))
    edge_count = cur.fetchone()[0]
    db.close()

    status = "  full reindex" if full else "  incremental"
    print(f"[glyph] {name}: {parsed_count} parsed, {skipped} skipped — "
          f"{sym_count} symbols, {edge_count} edges{status}")
    return {"project": name, "total": len(files_found), "parsed": parsed_count,
            "skipped": skipped, "symbols": sym_count, "edges": edge_count}


def _resolve_edges(cur, project_id: int, raw_edges: list) -> list:
    """Resolve edge targets (name → symbol ID) using batch lookup."""
    if not raw_edges:
        return []

    # Build name→id map
    cur.execute("SELECT name, id FROM symbols WHERE project_id=?", (project_id,))
    name_to_ids = defaultdict(list)
    for name, sid in cur.fetchall():
        name_to_ids[name].append(sid)

    resolved = []
    for src_file_id, pid, target_name, kind in raw_edges:
        ids = name_to_ids.get(target_name, [])
        for tid in ids:
            resolved.append((None, tid, kind, project_id))
            break  # One edge per target name max for performance
    return resolved


# ═══════════════════════════════════════════════════════════════════
# QUERIES
# ═══════════════════════════════════════════════════════════════════

def find_symbol(project: str, name: str):
    """Find where a symbol is defined."""
    db = get_db()
    cur = db.execute("SELECT id FROM projects WHERE name=?", (project,))
    pid = cur.fetchone()
    if not pid:
        print(f"Project '{project}' not indexed. Run: glyph scan {project} <path>")
        db.close()
        return
    pid = pid[0]

    rows = cur.execute("""
        SELECT s.name, s.kind, s.line, f.path, s.exported
        FROM symbols s JOIN files f ON s.file_id = f.id
        WHERE s.project_id=? AND s.name=?
        ORDER BY s.exported DESC, s.line
        LIMIT 20
    """, (pid, name)).fetchall()

    if not rows:
        print(f"'{name}' not found in {project}")
        # Try substring
        rows = cur.execute("""
            SELECT s.name, s.kind, s.line, f.path, s.exported
            FROM symbols s JOIN files f ON s.file_id = f.id
            WHERE s.project_id=? AND s.name LIKE ?
            ORDER BY s.name, s.line LIMIT 20
        """, (pid, f"%{name}%")).fetchall()
        if rows:
            print(f"  (substring matches for '{name}'):")
        else:
            db.close()
            return

    for sym_name, kind, line, path, exported in rows:
        exp = " [exported]" if exported else ""
        print(f"  {sym_name} ({kind}) → {path}:{line}{exp}")

    db.close()


def deps(project: str, name: str, direction: str = "callers"):
    """Show what calls this symbol or what this symbol calls."""
    db = get_db()
    cur = db.execute("SELECT id FROM projects WHERE name=?", (project,))
    pid = cur.fetchone()
    if not pid:
        print(f"Project '{project}' not found")
        db.close()
        return
    pid = pid[0]

    # Find symbol IDs
    sym_rows = cur.execute(
        "SELECT id, name, kind FROM symbols WHERE project_id=? AND name=?",
        (pid, name)).fetchall()
    if not sym_rows:
        print(f"'{name}' not found in {project}")
        db.close()
        return

    for sym_id, sym_name, kind in sym_rows:
        if direction in ("callers", "both"):
            rows = cur.execute("""
                SELECT e.kind, s.name, s.kind, f.path, s.line
                FROM edges e
                JOIN symbols s ON e.source_id = s.id
                JOIN files f ON s.file_id = f.id
                WHERE e.target_id=? AND e.project_id=?
                LIMIT 50
            """, (sym_id, pid)).fetchall()
            if rows:
                print(f"\n  ↑ {sym_name} ({kind}) is called by:")
                for ek, sn, sk, fp, sl in rows:
                    print(f"    {sn} ({sk}) → {fp}:{sl}")

        if direction in ("callees", "both"):
            rows = cur.execute("""
                SELECT e.kind, s.name, s.kind, f.path, s.line
                FROM edges e
                JOIN symbols s ON e.target_id = s.id
                JOIN files f ON s.file_id = f.id
                WHERE e.source_id=? AND e.project_id=?
                LIMIT 50
            """, (sym_id, pid)).fetchall()
            if rows:
                print(f"\n  ↓ {sym_name} ({kind}) calls:")
                for ek, sn, sk, fp, sl in rows:
                    print(f"    {sn} ({sk}) → {fp}:{sl}")

    db.close()


def godnodes(project: str, limit: int = 15):
    """Show most-connected symbols."""
    db = get_db()
    cur = db.execute("SELECT id FROM projects WHERE name=?", (project,))
    pid = cur.fetchone()
    if not pid:
        print(f"Project '{project}' not found")
        db.close()
        return
    pid = pid[0]

    rows = cur.execute("""
        SELECT s.name, s.kind, s.line, f.path,
               (SELECT COUNT(*) FROM edges e
                WHERE (e.target_id = s.id) AND e.project_id = ?) as edge_count
        FROM symbols s
        JOIN files f ON s.file_id = f.id
        WHERE s.project_id = ?
        ORDER BY edge_count DESC
        LIMIT ?
    """, (pid, pid, limit)).fetchall()

    print(f"\n  Top {limit} most-connected symbols in {project}:")
    print(f"  {'Rank':<5} {'Symbol':<30} {'Kind':<12} {'Edges':<8} {'Location'}")
    print(f"  {'─'*5} {'─'*30} {'─'*12} {'─'*8} {'─'*50}")
    for i, (name, kind, line, path, count) in enumerate(rows, 1):
        loc = f"{path}:{line}"
        print(f"  {i:<5} {name:<30} {kind:<12} {count:<8} {loc}")

    db.close()


def bridges(project: str, limit: int = 20):
    """Find symbols that connect multiple communities (high betweenness proxy)."""
    db = get_db()
    cur = db.execute("SELECT id FROM projects WHERE name=?", (project,))
    pid = cur.fetchone()
    if not pid:
        print(f"Project '{project}' not found")
        db.close()
        return
    pid = pid[0]

    # Approximate bridges: symbols that are called from many different files
    rows = cur.execute("""
        SELECT s.name, s.kind, s.line, f.path,
               COUNT(DISTINCT sf.path) as caller_files
        FROM symbols s
        JOIN files f ON s.file_id = f.id
        JOIN edges e ON e.target_id = s.id AND e.project_id = ?
        JOIN symbols ss ON e.source_id = ss.id
        JOIN files sf ON ss.file_id = sf.id
        WHERE s.project_id = ? AND sf.path != f.path
        GROUP BY s.id
        ORDER BY caller_files DESC
        LIMIT ?
    """, (pid, pid, limit)).fetchall()

    print(f"\n  Cross-file bridges in {project}:")
    print(f"  {'Symbol':<30} {'Kind':<12} {'Files':<8} {'Location'}")
    print(f"  {'─'*30} {'─'*12} {'─'*8} {'─'*50}")
    for name, kind, line, path, cfiles in rows:
        print(f"  {name:<30} {kind:<12} {cfiles:<8} {path}:{line}")

    db.close()


def orphans(project: str, limit: int = 30):
    """Find exported symbols with no incoming edges."""
    db = get_db()
    cur = db.execute("SELECT id FROM projects WHERE name=?", (project,))
    pid = cur.fetchone()
    if not pid:
        print(f"Project '{project}' not found")
        db.close()
        return
    pid = pid[0]

    rows = cur.execute("""
        SELECT s.name, s.kind, s.line, f.path
        FROM symbols s
        JOIN files f ON s.file_id = f.id
        WHERE s.project_id = ? AND s.exported = 1
        AND s.id NOT IN (
            SELECT target_id FROM edges WHERE project_id = ?
        )
        AND s.id NOT IN (
            SELECT source_id FROM edges WHERE project_id = ?
            AND kind = 'import'
        )
        ORDER BY s.name
        LIMIT ?
    """, (pid, pid, pid, limit)).fetchall()

    print(f"\n  Potentially unused exports in {project}:")
    for name, kind, line, path in rows:
        print(f"  {name} ({kind}) → {path}:{line}")

    db.close()


def path_between(project: str, src: str, dst: str, max_depth: int = 4):
    """BFS to find a call path between two symbols."""
    db = get_db()
    cur = db.execute("SELECT id FROM projects WHERE name=?", (project,))
    pid = cur.fetchone()
    if not pid:
        print(f"Project '{project}' not found")
        db.close()
        return
    pid = pid[0]

    # Get start/end IDs
    src_ids = [r[0] for r in cur.execute(
        "SELECT id FROM symbols WHERE project_id=? AND name=?", (pid, src))]
    dst_ids = [r[0] for r in cur.execute(
        "SELECT id FROM symbols WHERE project_id=? AND name=?", (pid, dst))]

    if not src_ids:
        print(f"'{src}' not found")
        db.close()
        return
    if not dst_ids:
        print(f"'{dst}' not found")
        db.close()
        return

    # Get all edges (skip imports where source_id is NULL)
    edges = defaultdict(list)
    for edge_src, edge_dst in cur.execute(
        "SELECT source_id, target_id FROM edges WHERE project_id=? AND source_id IS NOT NULL",
        (pid,)):
        edges[edge_src].append(edge_dst)

    # Get symbol names
    names = {}
    for sid, name, kind in cur.execute(
        "SELECT id, name, kind FROM symbols WHERE project_id=?", (pid,)):
        names[sid] = f"{name}"

    # BFS from all src_ids
    visited = set()
    parents = {}
    queue = list(src_ids)
    for sid in src_ids:
        visited.add(sid)
        parents[sid] = None

    found = None
    while queue and not found:
        current = queue.pop(0)
        for neighbor in edges.get(current, []):
            if neighbor in visited:
                continue
            visited.add(neighbor)
            parents[neighbor] = current
            if neighbor in dst_ids:
                found = neighbor
                break
            queue.append(neighbor)

    if not found:
        print(f"No path found from '{src}' to '{dst}' within {max_depth} hops")
        db.close()
        return

    # Reconstruct path
    path_syms = []
    node = found
    while node is not None:
        path_syms.append(names.get(node, f"#{node}"))
        node = parents.get(node)
    path_syms.reverse()

    print(f"  Path: {' → '.join(path_syms)} ({len(path_syms)-1} hops)")

    db.close()


def list_projects():
    """List all indexed projects."""
    db = get_db()
    rows = db.execute("SELECT name, path, last_scan FROM projects ORDER BY name").fetchall()
    if not rows:
        print("No projects indexed. Run: glyph scan <name> <path>")
    else:
        print(f"  {'Project':<25} {'Path':<50} {'Last Scan'}")
        print(f"  {'─'*25} {'─'*50} {'─'*20}")
        for name, path, ts in rows:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "never"
            print(f"  {name:<25} {path:<50} {when}")
    db.close()


def stats(project: str = None):
    """Show indexing stats."""
    db = get_db()
    if project:
        cur = db.execute("SELECT id FROM projects WHERE name=?", (project,))
        pid = cur.fetchone()
        if not pid:
            print(f"Project '{project}' not found")
            db.close()
            return
        pid = pid[0]
        proj_filter = "WHERE project_id = ?"
        params = (pid, pid, pid)
    else:
        proj_filter = ""
        params = ()

    fc = db.execute(f"SELECT COUNT(*) FROM files {proj_filter}", params[:1]).fetchone()[0]
    sc = db.execute(f"SELECT COUNT(*) FROM symbols {proj_filter}", params[:1]).fetchone()[0]
    ec = db.execute(f"SELECT COUNT(*) FROM edges {proj_filter}", params[:1]).fetchone()[0]

    if project:
        print(f"\n  {project}: {fc} files, {sc} symbols, {ec} edges")
    else:
        for name, in db.execute("SELECT name FROM projects ORDER BY name"):
            pid2 = db.execute("SELECT id FROM projects WHERE name=?", (name,)).fetchone()[0]
            fc2 = db.execute("SELECT COUNT(*) FROM files WHERE project_id=?", (pid2,)).fetchone()[0]
            sc2 = db.execute("SELECT COUNT(*) FROM symbols WHERE project_id=?", (pid2,)).fetchone()[0]
            ec2 = db.execute("SELECT COUNT(*) FROM edges WHERE project_id=?", (pid2,)).fetchone()[0]
            print(f"  {name:<25} {fc2:>5} files  {sc2:>6} symbols  {ec2:>6} edges")

    # Kind breakdown
    if project:
        proj_filter = "WHERE project_id = ?"
        params = (pid,)
    else:
        proj_filter = ""
        params = ()
    rows = db.execute(
        f"SELECT kind, COUNT(*) FROM symbols {proj_filter} GROUP BY kind ORDER BY COUNT(*) DESC",
        params).fetchall()
    if rows:
        print(f"\n  Symbol kinds:")
        for kind, count in rows:
            print(f"    {kind:<15} {count:>6}")

    db.close()


# ═══════════════════════════════════════════════════════════════════
# WATCH MODE (optional — uses polling for simplicity)
# ═══════════════════════════════════════════════════════════════════

def watch_project(name: str, interval: int = 10):
    """Poll for changes and re-index incrementally."""
    print(f"[glyph] Watching {name} (every {interval}s)... Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(interval)
            scan_project(name, "", full=False)
    except KeyboardInterrupt:
        print("\n[glyph] Watch stopped.")


# ═══════════════════════════════════════════════════════════════════
# PROJECT MAP GENERATOR
# ═══════════════════════════════════════════════════════════════════

def generate_map(project: str):
    """Generate a PROJECT_MAP.md for the indexed project."""
    db = get_db()
    cur = db.execute("SELECT id, path FROM projects WHERE name=?", (project,))
    row = cur.fetchone()
    if not row:
        print(f"Project '{project}' not indexed")
        db.close()
        return
    pid, root = row

    # Get directory structure from files
    dirs = defaultdict(list)
    for fpath, in cur.execute(
        "SELECT path FROM files WHERE project_id=? ORDER BY path", (pid,)):
        d = os.path.dirname(fpath) or "."
        dirs[d].append(os.path.basename(fpath))

    # Get top symbols
    top = cur.execute("""
        SELECT s.name, s.kind, f.path, s.line, s.exported
        FROM symbols s JOIN files f ON s.file_id = f.id
        WHERE s.project_id=?
        ORDER BY s.name
    """, (pid,)).fetchall()

    # God nodes
    gods = cur.execute("""
        SELECT s.name, s.kind,
               (SELECT COUNT(*) FROM edges e
                WHERE e.target_id = s.id AND e.project_id = ?) as edge_count
        FROM symbols s
        WHERE s.project_id = ?
        ORDER BY edge_count DESC LIMIT 15
    """, (pid, pid)).fetchall()

    # Bridges
    bridge_rows = cur.execute("""
        SELECT s.name, s.kind, COUNT(DISTINCT sf.path) as caller_files
        FROM symbols s
        JOIN edges e ON e.target_id=s.id AND e.project_id=?
        JOIN symbols ss ON e.source_id=ss.id
        JOIN files sf ON ss.file_id=sf.id
        JOIN files f ON s.file_id=f.id
        WHERE s.project_id=? AND sf.path != f.path
        GROUP BY s.id ORDER BY caller_files DESC LIMIT 10
    """, (pid, pid)).fetchall()

    db.close()

    # Build output
    lines = []
    lines.append(f"# {project} — Codebase Map\n")
    lines.append(f"*Auto-generated by codex on {time.strftime('%Y-%m-%d %H:%M')}*\n")
    lines.append(f"**Root:** `{root}`\n")

    # Directory structure
    lines.append("## Directory Structure\n")
    lines.append("```")
    for d in sorted(dirs):
        depth = d.count(os.sep) + 1
        indent = "  " * depth
        name = os.path.basename(d) if d != "." else project
        lines.append(f"{indent}{name}/")
        for f in sorted(dirs[d])[:8]:  # Limit files per dir
            lines.append(f"{indent}  ├── {f}")
        if len(dirs[d]) > 8:
            lines.append(f"{indent}  └── ... ({len(dirs[d])} files total)")
    lines.append("```\n")

    # God nodes
    lines.append("## Most-Connected Symbols\n")
    lines.append("| Symbol | Kind | Connections |")
    lines.append("|--------|------|-------------|")
    for name, kind, count in gods:
        lines.append(f"| `{name}` | {kind} | {count} |")
    lines.append("")

    # Cross-file bridges
    if bridge_rows:
        lines.append("## Cross-File Bridges\n")
        lines.append("| Symbol | Kind | Caller Files |")
        lines.append("|--------|------|-------------|")
        for name, kind, cfiles in bridge_rows:
            lines.append(f"| `{name}` | {kind} | {cfiles} |")
        lines.append("")

    # Key symbols by kind
    lines.append("## Key Symbols\n")
    by_kind = defaultdict(list)
    for name, kind, fpath, line, exported in top:
        exp = "⚡" if exported else ""
        by_kind[kind].append(f"  - `{name}` → `{fpath}:{line}` {exp}")

    for kind in sorted(by_kind, key=lambda k: len(by_kind[k]), reverse=True):
        lines.append(f"\n### {kind.title()}s")
        lines.extend(by_kind[kind][:20])
        if len(by_kind[kind]) > 20:
            lines.append(f"  - ... and {len(by_kind[kind])-20} more")

    map_path = os.path.join(root, "PROJECT_MAP.md")
    with open(map_path, "w") as f:
        f.write("\n".join(lines))
    print(f"[glyph] Wrote PROJECT_MAP.md → {map_path}")


# ═══════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════
# v1.1 — KNOWLEDGE GRAPH EXTENSIONS
# ═══════════════════════════════════════════════════════════════════

def history_project(name: str):
    """Backfill git change history for a project into file_history table."""
    init_schema()
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id, path FROM projects WHERE name = ?", (name,))
    row = cur.fetchone()
    if not row:
        print(f"Project '{name}' not found. Run 'glyph scan {name} <path>' first.")
        db.close()
        return
    proj_id, proj_path = row[0], row[1]

    # Build file path → id mapping
    files = cur.execute(
        "SELECT id, path FROM files WHERE project_id = ?", (proj_id,)
    ).fetchall()
    path_to_id = {f[1]: f[0] for f in files}

    # Clear existing
    deleted = cur.execute(
        "DELETE FROM file_history WHERE project_id = ?", (proj_id,)
    ).rowcount

    # Run git log
    try:
        result = subprocess.run(
            ['git', '-C', proj_path, 'log',
             '--format=%H%x00%at%x00%an%x00%s',
             '--name-status', '--diff-filter=AMDR'],
            capture_output=True, text=True, timeout=60
        )
    except subprocess.TimeoutExpired:
        print("Git log timed out")
        db.close()
        return

    if result.returncode != 0:
        print(f"Git error: {result.stderr[:200]}")
        db.close()
        return

    inserted = 0
    skipped = 0
    current = None

    for line in result.stdout.strip().split('\n'):
        if not line.strip():
            continue
        if '\x00' in line and '\t' not in line:
            parts = line.split('\x00', 3)
            if len(parts) >= 4:
                current = {'hash': parts[0], 'ts': int(parts[1]),
                           'author': parts[2], 'msg': parts[3][:500]}
            continue
        if '\t' in line and current:
            status = line[0]
            filepath = line.split('\t')[-1]
            if filepath not in path_to_id:
                skipped += 1
                continue
            change_type = {'A': 'added', 'M': 'modified',
                           'D': 'deleted', 'R': 'renamed'}.get(status, 'modified')
            cur.execute("""
                INSERT INTO file_history
                (file_id, project_id, commit_hash, commit_msg, author,
                 committed_at, change_type, old_hash, new_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, '', '')
            """, (path_to_id[filepath], proj_id, current['hash'],
                  current['msg'], current['author'], current['ts'], change_type))
            inserted += 1

    db.commit()
    print(f"History backfill: {inserted} entries ({skipped} files outside index, "
          f"{deleted} old entries cleared)")
    db.close()


def stats_extended(project: str = None):
    """Enhanced stats including history and session refs."""
    init_schema()
    db = get_db()
    cur = db.cursor()

    if project:
        cur.execute("SELECT id FROM projects WHERE name = ?", (project,))
        if not cur.fetchone():
            print(f"Project '{project}' not found")
            db.close()
            return
        proj_filter = "WHERE p.name = ?"
        params = (project,)
    else:
        proj_filter = ""
        params = ()

    print(f"\n  glyph v{VERSION} — knowledge graph")
    print(f"  {'─'*45}")

    rows = cur.execute(f"""
        SELECT p.name, p.last_scan,
               (SELECT COUNT(*) FROM files f WHERE f.project_id = p.id) as files,
               (SELECT COUNT(*) FROM symbols s WHERE s.project_id = p.id) as symbols,
               (SELECT COUNT(*) FROM edges e WHERE e.project_id = p.id) as edges,
               (SELECT COUNT(*) FROM file_history fh WHERE fh.project_id = p.id) as history,
               (SELECT COUNT(*) FROM session_refs sr WHERE sr.project_id = p.id) as refs,
               (SELECT COUNT(*) FROM descriptions d WHERE d.project_id = p.id) as descs
        FROM projects p {proj_filter}
        ORDER BY p.last_scan DESC
    """, params).fetchall()

    for r in rows:
        name, ts, nf, ns, ne, nh, nr, nd = r
        when = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M') if ts else 'never'
        print(f"\n  {name}")
        print(f"    Scanned:     {when}")
        print(f"    Structure:   {nf} files, {ns} symbols, {ne} edges")
        print(f"    History:     {nh} git changes")
        print(f"    Context:     {nr} session refs, {nd} descriptions")

    if not project:
        db_size = os.path.getsize(os.path.expanduser('~/.glyph/glyph.db'))
        print(f"\n  DB: {db_size/1024/1024:.1f} MB")
    db.close()


def usage():
    print(f"""glyph v{VERSION} — fast incremental codebase knowledge graph

  glyph scan <name> <path>            Index a project
  glyph scan <name> <path> --full     Full re-index (ignore cache)
  glyph find <project> <symbol>       Where is X defined?
  glyph deps <project> <symbol>       What calls X / what does X call?
  glyph path <project> <A> <B>        Find call chain from A to B
  glyph godnodes <project> [N]        Most-connected symbols
  glyph bridges <project> [N]         Cross-file connectors
  glyph orphans <project> [N]         Unused exported symbols
  glyph stats [project]               Indexing statistics (extended)
  glyph map <project>                 Generate PROJECT_MAP.md
  glyph list                          List indexed projects
  glyph watch <name> [interval]       Poll for changes

  ── v1.1 knowledge extensions ──
  glyph history <project>             Backfill git change history
  glyph refs <project> [N]            Backfill session cross-references
""")


def main():
    if len(sys.argv) < 2:
        usage()
        return

    cmd = sys.argv[1]

    if cmd == "scan":
        if len(sys.argv) < 4:
            print("Usage: glyph scan <name> <path> [--full]")
            return
        name, path = sys.argv[2], sys.argv[3]
        full = "--full" in sys.argv
        scan_project(name, path, full=full)

    elif cmd == "find":
        if len(sys.argv) < 4:
            print("Usage: glyph find <project> <symbol>")
            return
        find_symbol(sys.argv[2], sys.argv[3])

    elif cmd == "deps":
        if len(sys.argv) < 4:
            print("Usage: glyph deps <project> <symbol>")
            return
        deps(sys.argv[2], sys.argv[3])

    elif cmd == "path":
        if len(sys.argv) < 5:
            print("Usage: glyph path <project> <A> <B>")
            return
        path_between(sys.argv[2], sys.argv[3], sys.argv[4])

    elif cmd == "godnodes":
        if len(sys.argv) < 3:
            print("Usage: glyph godnodes <project> [N]")
            return
        n = int(sys.argv[3]) if len(sys.argv) > 3 else 15
        godnodes(sys.argv[2], n)

    elif cmd == "bridges":
        if len(sys.argv) < 3:
            print("Usage: glyph bridges <project> [N]")
            return
        n = int(sys.argv[3]) if len(sys.argv) > 3 else 20
        bridges(sys.argv[2], n)

    elif cmd == "orphans":
        if len(sys.argv) < 3:
            print("Usage: glyph orphans <project> [N]")
            return
        n = int(sys.argv[3]) if len(sys.argv) > 3 else 30
        orphans(sys.argv[2], n)

    elif cmd == "stats":
        project = sys.argv[2] if len(sys.argv) > 2 else None
        stats_extended(project)

    elif cmd == "history":
        if len(sys.argv) < 3:
            print("Usage: glyph history <project>")
            return
        history_project(sys.argv[2])

    elif cmd == "refs":
        if len(sys.argv) < 3:
            print("Usage: glyph refs <project> [N]")
            return
        n = int(sys.argv[3]) if len(sys.argv) > 3 else 50
        print(f"Run: python3 ~/.glyph/glyph-extend.py --backfill-sessions --project {sys.argv[2]}")
        print(f"(session backfill reads state.db — use glyph-extend.py for now)")

    elif cmd in ("--version", "-v", "version"):
        print(f"glyph v{VERSION}")

    elif cmd == "map":
        if len(sys.argv) < 3:
            print("Usage: glyph map <project>")
            return
        generate_map(sys.argv[2])

    elif cmd == "list":
        list_projects()

    elif cmd == "watch":
        if len(sys.argv) < 3:
            print("Usage: glyph watch <name> [interval_seconds]")
            return
        interval = int(sys.argv[3]) if len(sys.argv) > 3 else 10
        watch_project(sys.argv[2], interval)

    else:
        print(f"Unknown command: {cmd}")
        usage()


if __name__ == "__main__":
    main()
