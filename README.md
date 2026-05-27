# 𐂷 Glyph

> **Carve meaning into your codebase.**

Glyph is a **lightning-fast, zero-LLM-cost codebase indexer** that builds a knowledge graph of your source code using tree-sitter AST parsing. It answers the questions that slow you down: *Where is X defined? What calls this? What are the most-connected symbols? Show me the call chain from A to B.*

---

## The Problem

Navigating large codebases is hard. Tools like `grep` and `rg` find text, but they can't tell you structure. They can't show you call chains, identify cross-module bridges, or surface the "god nodes" that everything depends on.

We tried [Graphify](https://github.com/safishamsi/graphify) — an ambitious tool that combines AST parsing with LLM-powered semantic extraction. But in practice:

| Graphify Problem | Real-World Impact |
|-----------------|-------------------|
| **LLM extraction fails silently** | DeepSeek/OpenAI API errors mid-job, no retry, no resume |
| **Full rescan every time** | 2,400 files = 90+ seconds, even if you changed 3 files |
| **No incremental updates** | No file hashing, no dirty tracking — always starts from zero |
| **Code skips LLM anyway** | The docs admit "code files skip LLM extraction" — so the big value prop is dead on arrival for TypeScript/Python |
| **Fragile installation** | npm link issues, Node version sensitivity |
| **JSON dump, no query engine** | Raw JSON output with no indexed lookups, no path traversal |
| **No multi-project view** | One project at a time, no cross-repo awareness |
| **Performative boot screen** | "357 agents across 12 departments" — it's a static ASCII banner, not actually orchestrating anything |

---

## How Glyph Is Different

| Feature | Graphify | Glyph |
|---------|----------|-------|
| **Indexing engine** | AST + LLM (but LLM often fails) | Pure tree-sitter AST — fast, reliable, never fails |
| **Incremental updates** | ❌ Full rescan every time | ✅ MD5 file hashing — 3 changed files = <100ms |
| **LLM cost** | $0.002–$0.05 per scan | **$0.00** — AST is free |
| **Storage** | JSON files | SQLite with indexed lookups |
| **Query engine** | CLI + regex on JSON | BFS path traversal, indexed symbol lookup, edge counting |
| **Multi-project** | ❌ | ✅ One DB, many projects |
| **Installation** | npm + Node version dance | `pip install` + one script |
| **Languages** | TS, Python | TS/TSX/JS/JSX, Python, Go, Bash |
| **Auto-generated maps** | ❌ | ✅ `glyph map` generates `PROJECT_MAP.md` |
| **Lines of code** | ~5,000+ (complex multi-module) | **~1,200** (single file, readable) |

---

## What Glyph Tells You

```bash
# Where is something defined?
glyph find myproject sendEmail
  → SendEmailOptions (interface) → src/lib/email/types.ts:66
  → sendEmailSchema (const) → src/app/api/email/send/route.ts:19

# Show me the god nodes — most-connected symbols
glyph godnodes myproject
  → Button (component) — 713 connections
  → Card (component) — 696 connections
  → cn (function) — 422 connections

# Trace a call chain
glyph path myproject "webhookHandler" "sendEmail"
  → webhookHandler → processPush → deployApp → notifyUser → sendEmail (4 hops)

# Find cross-file bridges
glyph bridges myproject
  → logger — called from 47 different files
  → withErrorHandler — called from 35 different files

# Generate a PROJECT_MAP.md for your repo
glyph map myproject
  → Writes PROJECT_MAP.md with directory tree, god nodes, key symbols

# Stats at a glance
glyph stats myproject
  → boottify: 2,525 files, 31,348 symbols, 11,090 edges
```

---

## Installation

```bash
# Clone and install
git clone https://github.com/boottify/glyph.git
cd glyph
./install.sh
```

Or one-liner:

```bash
curl -fsSL https://raw.githubusercontent.com/boottify/glyph/main/install.sh | bash
```

**Requirements:** Python 3.10+, pip, git

---

## Quick Start

```bash
# 1. Index your project
glyph scan myproject /path/to/your/repo

# 2. Explore
glyph find myproject sendEmail
glyph godnodes myproject
glyph bridges myproject
glyph path myproject "auth" "email"

# 3. Generate documentation
glyph map myproject       # Creates PROJECT_MAP.md at repo root
glyph stats               # All indexed projects

# 4. Update after code changes (incremental — only changed files)
glyph scan myproject /path/to/your/repo

# 5. Index more projects
glyph scan another /path/to/another/repo
glyph list                # See all indexed projects
```

---

## How It Works

```
┌─────────────────────────────────────────┐
│  glyph scan myproject /path/to/repo     │
│                                         │
│  1. Walk filesystem                     │
│     ↓                                   │
│  2. MD5 hash → skip unchanged files     │  ← incremental!
│     ↓                                   │
│  3. tree-sitter AST parse               │  ← TypeScript, Python, Go, Bash
│     ↓                                   │
│  4. Extract symbols + edges             │  ← functions, classes, imports, calls
│     ↓                                   │
│  5. Store in SQLite (~/.glyph/glyph.db) │  ← indexed, queryable
│     ↓                                   │
│  glyph find / godnodes / path / map     │
└─────────────────────────────────────────┘
```

### Symbol Kinds Detected

- **TypeScript/JSX:** `function`, `class`, `const`, `let`, `var`, `interface`, `type`, `enum`, `component` (React), `method`
- **Python:** `function`, `class`
- **Go:** `function`, `method`, `type`
- **Bash:** `function`

### Edge Kinds

- `import` — module imports
- `call` — function/method calls
- `jsx_use` — React component usage in JSX

---

## Real-World Performance

Indexed against **daigtas-platform** (Boottify SaaS — 2,525 source files):

| Metric | Value |
|--------|-------|
| Files indexed | 2,525 (.ts, .tsx, .js, .jsx) |
| Symbols extracted | **31,348** |
| Edges resolved | **11,090** |
| First scan time | ~30 seconds |
| Incremental (3 files changed) | ~120ms |
| Database size | 8.2 MB |
| LLM cost | **$0.00** |

```
Symbol kinds:
  const            23,956
  function          4,216
  interface         2,011
  let                 502
  type                269
  method              253
  component            94
  class                34
```

---

## Use Cases

- **AI coding agents** — Give your agent a map of the codebase before it starts working. Dramatically reduces the "find → read → trace" tool-call loop.
- **Onboarding** — New developers get `PROJECT_MAP.md` instantly.
- **Refactoring** — Find all callers of a function, identify unused exports.
- **Architecture review** — God nodes and bridges reveal structural coupling.
- **Multi-repo navigation** — Index all your projects in one DB.

---

## Compared to...

| Tool | Approach | Glyph Advantage |
|------|----------|-----------------|
| `grep` / `rg` | Text search | Structure-aware — "show me what *calls* this" |
| Graphify | AST + optional LLM | Incremental updates, SQLite queries, multi-project |
| Sourcegraph | Cloud-hosted, full LSIF | Local, free, zero setup |
| GitHub Code Search | Text index | Finds definitions by AST, not just text matches |

---

## Roadmap

- [ ] `glyph watch` — filesystem watcher for live incremental updates
- [ ] Source-level edge resolution (which file does `call` point to)
- [ ] Community detection (Louvain algorithm)
- [ ] HTML graph visualization
- [ ] PR diff mode — "what changed in the graph between commits?"
- [ ] VSCode extension

---

## Contributing

Glyph is a single Python file. It's designed to be readable and hackable.

```bash
git clone https://github.com/boottify/glyph.git
cd glyph
python3 -m venv venv
source venv/bin/activate
pip install tree-sitter tree-sitter-typescript tree-sitter-python tree-sitter-go tree-sitter-bash
python3 glyph.py scan test /path/to/some/code
```

Pull requests welcome. See `glyph.py` — it's ~1,200 lines with clear sections.

---

## License

MIT © [Boottify](https://boottify.com)

---

*"The most valuable tool for an AI agent isn't a faster grep — it's a map."*
