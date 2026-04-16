<div align="center">

<img src="https://img.shields.io/badge/CodeLens-Offline%20Semantic%20Search-6C63FF?style=for-the-badge&logo=visual-studio-code&logoColor=white" alt="CodeLens">

# 🔍 CodeLens

### *Natural-language search for your entire codebase — 100% offline, instant results*

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![TypeScript](https://img.shields.io/badge/TypeScript-5.3+-3178C6?style=flat-square&logo=typescript&logoColor=white)](https://typescriptlang.org)
[![VS Code](https://img.shields.io/badge/VS%20Code-Extension-007ACC?style=flat-square&logo=visual-studio-code&logoColor=white)](https://code.visualstudio.com)
[![Actian VectorAI](https://img.shields.io/badge/Actian-VectorAI%20DB-EE3A43?style=flat-square&logo=databricks&logoColor=white)](https://www.actian.com)
[![Ollama](https://img.shields.io/badge/Ollama-nomic--embed--text-FFA500?style=flat-square)](https://ollama.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](LICENSE)
[![ARM Ready](https://img.shields.io/badge/ARM-Apple%20Silicon%20Ready-000000?style=flat-square&logo=apple&logoColor=white)](https://apple.com)

**Type a question. Get the exact code. Jump to the line. No internet. No API keys. No compromise.**

[Quick Start](#-one-command-setup) · [Architecture](#-architecture) · [Why VectorAI DB](#-why-actian-vectorai-db) · [Demo](#-demo)

</div>

---

## 🎯 The Problem

You join a large codebase. You know there's *some* function that handles JWT token refresh — but `Ctrl+F "token"` returns 847 matches. You spend 20 minutes hunting. **That's broken.**

Traditional code search is keyword-based. It matches *text*, not *intent*. It can't understand that **"find where user sessions expire"** and `def invalidate_jwt_token()` are the same concept.

**CodeLens fixes this.**

---

## ✨ What CodeLens Does

> Type any natural language question about your codebase. Get the exact functions, classes, and blocks that answer it — **ranked by semantic similarity**, with a one-click jump to the file and line.

```
Query: "where does the app handle database connection errors?"

Result 1  backend/db.py  line 42–78   score 0.94  ████████████ ✓
          def handle_db_exception(err: DatabaseError) → None

Result 2  services/retry.py  line 12–34   score 0.87  ██████████   ✓
          class ConnectionRetryPolicy

Result 3  middleware/errors.py  line 89–102  score 0.79  █████████    ✓
          async def global_error_handler(request, exc)

→ [Jump to file] button opens the file at the exact line in VS Code
```

All of this runs **offline**. Close your WiFi. It still works.

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     LOCAL MACHINE — FULLY OFFLINE               │
│                                                                  │
│  ┌──────────────────────────────┐   ┌──────────────────────┐   │
│  │      INDEXING PIPELINE       │   │   ACTIAN VECTORAI DB  │   │
│  │   (runs once + on file save) │   │   (embedded, local)   │   │
│  │                              │   │                       │   │
│  │  📁 File Walker              │   │  • chunk_text         │   │
│  │     .py .ts .go .rs .java   │──▶│  • file_path          │   │
│  │         │                    │   │  • line_start/end     │   │
│  │         ▼                    │   │  • symbol_name        │   │
│  │  🌳 AST Chunker (tree-sitter)│   │  • language           │   │
│  │     functions, classes,      │   │  • commit_sha         │   │
│  │     methods, interfaces      │   │  • embedding (768d)   │   │
│  │         │                    │   │                       │   │
│  │         ▼                    │   └──────────┬────────────┘   │
│  │  🤖 Ollama Embedder          │              │                 │
│  │     nomic-embed-text (~270MB)│◀─────────────┘                │
│  │     runs 100% offline        │              │                 │
│  └──────────────────────────────┘              │                 │
│                                                │ ANN search      │
│  ┌──────────────────────────────┐              │ top-k results   │
│  │      QUERY PIPELINE          │              │                 │
│  │   (triggered by user)        │◀─────────────┘                │
│  │                              │                               │
│  │  💬 VS Code Sidebar          │   ┌──────────────────────┐   │
│  │     natural language input   │   │  Local LLM (optional) │   │
│  │         │                    │   │  Ollama + Mistral 7B  │   │
│  │         ▼                    │──▶│  "Explain mode"       │   │
│  │  🔢 Query Embedder           │   │  plain-English summary│   │
│  │     same model as indexer    │   └──────────────────────┘   │
│  │         │                    │                               │
│  │         ▼                    │                               │
│  │  📊 Result Renderer          │                               │
│  │     snippets + file links    │                               │
│  │     syntax highlighting      │                               │
│  │     [Jump to file] button   │                               │
│  └──────────────────────────────┘                               │
└─────────────────────────────────────────────────────────────────┘
```

### Two Pipelines, One Core

| Pipeline | Trigger | What it does |
|---|---|---|
| **Indexing** | Once on setup, then auto on file save | Walks repo → AST chunking → embed → store in VectorAI DB |
| **Query** | Every user search | Embed query → ANN search VectorAI DB → render ranked results |

---

## 🗄️ Why Actian VectorAI DB

> *This is the core engine. Not a cloud service. Not a managed SaaS. A portable, embeddable, local vector database that runs on any machine.*

This was the critical architectural decision. Here's the comparison we ran:

| Feature | **Actian VectorAI DB** | Chroma | Pinecone |
|---|:---:|:---:|:---:|
| **Works offline** | ✅ Always | ✅ Local mode | ❌ Cloud-only |
| **Zero config** | ✅ File-based | ⚠️ Needs server | ❌ API key + account |
| **ARM native** | ✅ M1/M2/M3/Pi | ⚠️ Varies | ❌ N/A |
| **Embedded** | ✅ In-process | ⚠️ Client-server | ❌ Remote |
| **Low latency ANN** | ✅ Sub-10ms local | ⚠️ Variable | ⚠️ Network bound |
| **Portable** | ✅ Copy the dir | ⚠️ Export needed | ❌ Locked to cloud |
| **Privacy** | ✅ Never leaves disk | ✅ Local mode | ❌ Data leaves machine |
| **Hackathon viable** | ✅ One command | ⚠️ More setup | ❌ Billing required |

**The key insight**: Portability + embedded architecture + offline-first ANN search made VectorAI DB the *only* viable choice for a tool that must work on a developer's laptop with zero cloud dependency.

When a developer runs `./setup.sh`, VectorAI DB initializes in `./.vectorai_db/`. It's a directory. You can copy it, back it up, and reproduce the exact index anywhere. That's not possible with any cloud vector database.

### What VectorAI DB stores per chunk

```python
{
    "symbol_name":    "handle_db_exception",   # function/class name
    "chunk_text":     "def handle_db_exception...",  # full source
    "file_path":      "backend/db.py",          # relative path
    "line_start":     42,                        # for jump-to-file
    "line_end":       78,
    "language":       "python",                  # parsed by tree-sitter
    "content_hash":   "a3f1c9d...",             # for dedup/incremental index
    "embedding":      [0.021, -0.134, ...]       # 768-dim vector
}
```

---

## 🌳 AST-Based Chunking (Not Line Splits)

Most naive RAG systems split code every N lines. This produces garbage chunks that split functions in half, include imports in the middle of class bodies, and destroy semantic coherence.

CodeLens uses **tree-sitter** to parse the actual AST of each file, then extracts *complete semantic units*:

```
Python   → function_definition, class_definition, decorated_definition
TypeScript → function_declaration, method_definition, class_declaration, arrow_function
Go       → function_declaration, method_declaration, type_declaration
Rust     → function_item, impl_item, struct_item
Java     → method_declaration, class_declaration, constructor_declaration
```

Each chunk is a **complete, meaningful code unit** — never half a function, never a fragment. This is why search results are actually useful.

> **Fallback**: For files with no recognized AST nodes (config, markdown, etc.), CodeLens falls back to a 40-line sliding window with 10-line overlap.

---

## 🚀 One-Command Setup

```bash
git clone https://github.com/TryingtobeingNikhil/CodeLens.git
cd CodeLens
chmod +x setup.sh
./setup.sh
```

That's it. The script:
1. Checks for [Ollama](https://ollama.com) (installs prompt if missing)
2. Starts the Ollama service
3. Pulls `nomic-embed-text` (~270 MB, one-time)
4. Installs all Python dependencies
5. Initializes the local VectorAI DB instance
6. Builds the VS Code extension

Then launch the backend:
```bash
uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

---

## 📦 Tech Stack

| Layer | Technology | Purpose |
|---|---|---|
| **Vector Database** | Actian VectorAI DB | Embedded local ANN search — the core engine |
| **Embedding Model** | `nomic-embed-text` via Ollama | 768-dim code embeddings, fully offline |
| **Code Parsing** | `tree-sitter` (6 languages) | AST-based semantic chunking |
| **API Server** | FastAPI + Uvicorn | `POST /index`, `POST /query`, `GET /status` |
| **File Watching** | `watchdog` | Auto-reindex on file save |
| **IDE Integration** | VS Code Extension (TypeScript) | Sidebar UI, jump-to-file, status bar |
| **Optional LLM** | Mistral 7B via Ollama | "Explain mode" — plain English summaries |
| **Progress UI** | `rich` | Beautiful CLI progress bars during indexing |

---

## 🔌 API Reference

### `POST /index` — Index a repository
```bash
curl -X POST http://localhost:8000/index \
  -H "Content-Type: application/json" \
  -d '{"repo_path": "/path/to/your/repo", "force_reindex": false}'
```
Returns a **Server-Sent Events stream** with real-time progress:
```json
{"type": "progress", "file": "src/auth.py", "chunks": 12, "total_files": 847}
{"type": "complete", "total_chunks": 9432, "duration_ms": 41200}
```

### `POST /query` — Semantic search
```bash
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"query": "where does JWT validation happen?", "top_k": 8, "explain": false}'
```
```json
{
  "results": [
    {
      "symbol_name": "validate_jwt_token",
      "file_path": "auth/middleware.py",
      "line_start": 34,
      "line_end": 67,
      "language": "python",
      "score": 0.94,
      "chunk_text": "def validate_jwt_token(token: str) -> User:..."
    }
  ],
  "query_ms": 38,
  "total_indexed": 9432
}
```

### `GET /status` — Engine status
```json
{
  "indexed_chunks": 9432,
  "last_indexed": "2026-04-16T14:20:16Z",
  "embed_model": "nomic-embed-text",
  "watching": true
}
```

### `GET /health` — Dependency health check
```json
{
  "ollama": true,
  "vectorai": true,
  "ollama_error": null
}
```

---

## 💡 VS Code Extension Features

| Feature | Description |
|---|---|
| **Semantic Search Sidebar** | Natural language input, ranked results with syntax highlighting |
| **Jump to File** | Click any result → VS Code opens the file at the exact line |
| **Live Re-indexing** | "Re-index" button triggers `POST /index` with SSE progress bar |
| **Status Bar** | Shows `⬡ CodeLens: 9,432 chunks` — always visible |
| **Offline Syntax Highlighting** | Built-in highlight.js stub — no CDN, no internet |
| **Keyboard Navigation** | `↑↓` arrows navigate results, `Enter` to search, `Esc` to clear |
| **AI Explain Mode** | Optional "Include AI explanation" checkbox → Mistral 7B summarizes results |
| **Shimmer Loading** | Skeleton cards while waiting — feels production-quality |
| **Example Queries** | Clickable chips on empty state to onboard new users instantly |

---

## 🔁 Incremental Indexing

CodeLens uses MD5 content hashing to **skip unchanged chunks**. When the file watcher triggers on a save:

1. The modified file is re-parsed by tree-sitter
2. New content hashes are generated for each chunk
3. Only chunks with **new hashes** are embedded and upserted
4. Deleted/modified functions are removed by `delete_by_filepath` before re-insert

This means **re-indexing a large codebase after a single file edit takes milliseconds**, not minutes.

---

## 🍎 ARM Native (Apple Silicon + Raspberry Pi)

CodeLens was designed and tested on **Apple Silicon (M-series)** from day one. The entire stack:
- `nomic-embed-text` via Ollama — native ARM binary
- Actian VectorAI DB — ARM-compatible embedded instance
- FastAPI/Uvicorn — architecture-agnostic Python
- VS Code extension — platform-agnostic TypeScript

No Rosetta. No emulation. No performance penalty.

---

## 🔒 Privacy by Design

| What never leaves your machine |
|---|
| Your source code |
| Your query text |
| Your embeddings |
| Your search history |

There are no telemetry calls, no analytics pings, no cloud sync. The only network calls CodeLens makes are to `localhost:11434` (Ollama) and `localhost:8000` (its own backend).

---

## 📁 Project Structure

```
CodeLens/
├── backend/                    # Python FastAPI backend
│   ├── __init__.py
│   ├── config.py               # Environment-based settings
│   ├── db_client.py            # Actian VectorAI DB wrapper
│   ├── indexer.py              # File walker + AST chunker + embedder
│   ├── main.py                 # FastAPI server (index/query/status/health)
│   ├── query.py                # Query embedding + ANN search + explain mode
│   └── tree_sitter_parser.py   # Multi-language AST parsing utilities
│
├── extension/                  # VS Code extension (TypeScript)
│   ├── src/
│   │   ├── extension.ts        # Extension entry, backend process spawner
│   │   └── searchPanel.ts      # Webview provider, message handler
│   └── media/
│       └── panel.html          # Full sidebar UI (search, results, progress)
│
├── shared/                     # Shared type definitions
├── setup.sh                    # ⭐ One-command full setup script
├── docker-compose.yml          # Docker deployment option
├── pyproject.toml              # Python dependency manifest
├── package.json                # Extension manifest + VS Code contribution points
├── tsconfig.json               # TypeScript compiler config
└── .env.example                # Environment variable template
```

---

## 🎬 Demo

> **The killer demo**: Turn off WiFi. Open VS Code. Type a question. Watch it work.

Three queries that demonstrate semantic understanding (not keyword matching):

| Query | What it finds | Why it's impressive |
|---|---|---|
| `"where does the app validate user sessions?"` | `def validate_jwt_token()` in `auth/middleware.py` | "validate" ≠ "sessions" ≠ "jwt" — semantic match |
| `"show me all database write operations"` | `INSERT`, `UPDATE`, `batch_upsert` across 6 files | Finds writes by *concept*, not by SQL keyword |
| `"which functions handle error logging?"` | Logger calls across multiple modules | "handle" + "error" + "logging" as a concept, not words |

---

## 🗺️ Roadmap

- [ ] **Dead code detector** — embed all function signatures, flag those with zero similarity to the rest of the codebase
- [ ] **Query history** — persisted locally, shown in sidebar with timestamps
- [ ] **Multi-workspace support** — index and switch between multiple repos
- [ ] **`.codelens` ignore file** — like `.gitignore` but for indexing
- [ ] **Commit-aware indexing** — only re-embed chunks changed since last git commit
- [ ] **Cross-file semantic linking** — show "functions that call this" / "functions called by this"

---

## 🤝 Contributing

```bash
# Backend development
pip install -e ".[dev]"
uvicorn backend.main:app --reload

# Extension development
npm install
npm run watch
# F5 in VS Code to launch Extension Development Host
```

---

## 📄 License

MIT — see [LICENSE](LICENSE)

---

<div align="center">

**Built for the Actian VectorAI DB Hackathon** · April 2026

*CodeLens is proof that developer tools don't need the cloud to be powerful.*

</div>
