<div align="center">

# 🔍 CodeLens

### *Search your codebase by meaning or by name. 100% offline.*

[![CI](https://github.com/TryingtobeingNikhil/CodeLens/actions/workflows/ci.yml/badge.svg)](https://github.com/TryingtobeingNikhil/CodeLens/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![TypeScript](https://img.shields.io/badge/TypeScript-5.3+-3178C6?style=flat-square&logo=typescript&logoColor=white)](https://typescriptlang.org)
[![SQLite](https://img.shields.io/badge/SQLite-FTS5%20%2B%20sqlite--vec-003B57?style=flat-square&logo=sqlite&logoColor=white)](https://github.com/asg017/sqlite-vec)
[![Ollama](https://img.shields.io/badge/Ollama-nomic--embed--text-FFA500?style=flat-square)](https://ollama.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](LICENSE)

**Type a question or a symbol name. Get the exact function. Jump to the line. No internet, no API keys.**

[Install](#-install-the-vs-code-extension) · [Use it](#-using-the-extension) · [Ollama](#-ollama-optional) · [Docker](#-run-with-docker) · [Search syntax](#-search-syntax) · [API](#-api)

<img src="docs/demo.gif" alt="Typing a query in the CodeLens sidebar: matching functions appear as you type, then a result is selected and its code copied" width="400">

</div>

---

## 🎯 The problem

I join a large codebase. I know there's *some* function that refreshes JWT tokens, but `Ctrl+F "token"` returns 847 matches. I spend 20 minutes hunting. Keyword search matches *text*, not *intent*: it can't tell that **"where do sessions expire"** and `def invalidate_jwt_token()` are the same thing.

Pure semantic search has the opposite problem: ask for `parseConfig` by name and an embedding model may rank a vaguely similar function above it.

**CodeLens does both.** Every query runs a semantic search and a keyword search, and fuses the two rankings.

```
Query: "where do we split camelCase identifiers"          (145 ms)

1  backend/text.py:19            split_identifier       function  both  0.78
2  backend/text.py:27            index_terms            function  both  0.69
3  tests/test_query_parse.py:29  test_split_identifier  function  both  0.73

Query: "embedding failures retry lang:py -in:tests"       (31 ms)

1  backend/embedder.py:34        Embedder._embed              method  both  0.67
2  backend/indexer.py:184        Indexer._embed_missing       method  both  0.65
3  backend/db_client.py:261      CodeLensDB.put_embeddings    method  both  0.67
```

Results are ordered by the fused rank; the last column is the cosine similarity to the query, so it isn't always decreasing.

All of it runs locally. Turn the WiFi off; it still works.

---

## ✨ Features

| | |
|---|---|
| **Hybrid search** | Semantic similarity (Ollama embeddings) and BM25 keyword search over symbol names, identifier sub-words, paths and code, merged with reciprocal-rank fusion. An exact symbol-name match always ranks first. Results are listed best first. |
| **Explain** | Ask a local model what a piece of code does: from a search result, or from any selection in the editor, no search needed. |
| **Suggestions from your repo** | The example searches in the sidebar are built from the symbols in the repo you opened, not a fixed list. |
| **Search as you type** | Results update while you type. The last word is matched as a prefix; stale responses are dropped. |
| **Filters** | `in:api -in:tests lang:py kind:function`, the same `key:value` style as [gyotaku](https://github.com/xevrion/gyotaku). An unfinished filter is ignored, not searched for, so results don't empty mid-keystroke. |
| **Symbol-aware chunks** | tree-sitter splits code into functions, methods, classes and types (Python, TS/TSX, JS, Go, Rust, Java, C/C++, CUDA), named like `Class.method`. Classes get a compact summary chunk; long functions are split into overlapping parts. |
| **Incremental and live** | Only changed files are re-chunked; only new content is re-embedded. A function that moved keeps its vector and gets its new line numbers. A file watcher picks up saves, creates, renames and deletes, and respects `.gitignore`. |
| **Ollama optional** | Without Ollama, files are still indexed and keyword search works; semantic search and Explain switch on once Ollama is running. |
| **Per-repo indexes** | Each workspace is indexed and searched separately in one local database. |
| **VS Code native** | Sidebar search, keyboard navigation, jump-to-line with a highlight flash, "Find Similar Code" and "Explain Selection" on a selection, status bar. Sets up its own Python environment on first run. |

---

## 🏗️ How it works

```
                          ┌──────────────────────────── ~/.codelens/codelens.db (SQLite) ──┐
 git ls-files             │                                                               │
 (honours .gitignore)     │  files        path → content hash (incremental indexing)      │
      │                   │  chunks       symbol, kind, language, lines, code             │
      ▼                   │  chunks_fts   FTS5: symbol · identifier sub-words · path · code│
 tree-sitter chunker ────▶│  embeddings   one float32 vector per distinct chunk content   │
      │                   │  vec_*        sqlite-vec index over those vectors, per repo   │
      ▼                   └───────────────────────────────────────────────────────────────┘
 Ollama nomic-embed-text            ▲                                   ▲
 (batched, 768-dim)  ───────────────┘                                   │
                                                                        │
 query ─▶ parse filters ─┬─▶ embed query ─▶ cosine top-k (sqlite-vec) ──┤
                         └─▶ FTS5 BM25 top-k ───────────────────────────┤
                                                                        ▼
                                      reciprocal-rank fusion + exact-name boost
                                      ─▶ collapse duplicates ─▶ VS Code sidebar
```

### Why SQLite + FTS5 + sqlite-vec

Earlier versions described the store as "Actian VectorAI DB"; it was actually a SQLite table of vectors that was fully read from disk and scanned with numpy on every query. It is now a real hybrid index in one file:

- **One transaction per file.** A file's chunks, its keyword index and its vectors change together, so the indexes never disagree, even if indexing is interrupted.
- **Keyword and vector search in the same place**, filtered by the same SQL `WHERE` clause.
- **[sqlite-vec](https://github.com/asg017/sqlite-vec)** does exact cosine search in C, partitioned by repo. When the Python build can't load SQLite extensions (python.org's macOS builds can't), CodeLens falls back to an in-memory numpy matrix that is rebuilt only when the index changes. Vectors are stored as plain BLOBs, so the sqlite-vec index is rebuilt automatically the next time a capable Python opens the file.
- **Tiny and portable.** No server, no JVM, no cloud: `pip install sqlite-vec` is ~200 KB, and the whole index is one file you can delete or copy.

Search is exact, not approximate: at codebase scale (thousands to low hundreds of thousands of chunks) a brute-force scan in C takes milliseconds, and an approximate index would only cost recall.

---

## 📦 Install the VS Code extension

Requirements: VS Code 1.86+ and Python 3.10+. Ollama is optional (see [below](#-ollama-optional)).

**Option A: download the .vsix (no build tools needed)**

1. Download `codelens-<version>.vsix` from the [Releases page](https://github.com/TryingtobeingNikhil/CodeLens/releases). Builds of every commit are also attached to the [CI runs](https://github.com/TryingtobeingNikhil/CodeLens/actions/workflows/ci.yml) as the `codelens-vsix` artifact.
2. In VS Code, open the Extensions view, click `···` at the top, choose **Install from VSIX...**, and pick the file. Or from a terminal:
   ```bash
   code --install-extension codelens-0.4.0.vsix
   ```
3. Reload VS Code. The first time, CodeLens asks to **Set Up** its search engine: it creates a private Python environment in VS Code's storage and installs the backend's packages there (about 150 MB, about a minute). Nothing is installed system-wide. Prefer your own environment? Choose **Use My Own Python** and set `codelens.pythonPath`.

**Option B: build it yourself**

```bash
git clone https://github.com/TryingtobeingNikhil/CodeLens.git
cd CodeLens
npm install
npm run package                        # builds codelens-0.4.0.vsix with the backend included
code --install-extension codelens-0.4.0.vsix
```

**Option C: run from source (for development)**

```bash
./setup.sh      # creates .venv with the backend and builds the extension (never installs Ollama)
```

Open the folder in VS Code and press F5 to start an Extension Development Host. The extension uses the repo's `.venv`.

---

## 🧭 Using the extension

1. **Open a project** and click the CodeLens icon in the activity bar (or press `Cmd/Ctrl+Alt+K`).
2. **Index it.** Click **Index workspace** in the sidebar the first time, or **Re-index** later. After that a file watcher keeps the index current as you save. The progress bar shows files read, then chunks embedded.
3. **Search.** Type a question ("where do we retry failed requests") or a name (`parseConfig`). Results update as you type and are listed best match first. Add [filters](#-search-syntax) such as `-in:tests` or `lang:py`. Not sure what to type? The sidebar suggests a few searches built from your repo's own code.
4. **Use a result.**
   - Click the card, press `Enter`, or click **Open** to jump to the code. The lines flash briefly.
   - **Explain** asks the local model what that code does. The answer appears under the code; click again to hide it.
   - **Copy code** copies the snippet to the clipboard, ready to paste into a chat, a note or an issue.
   - **Explain results** (above the list) answers your query using the top results together.
5. **Explain any code, no search needed.** Select code in the editor, right-click, **CodeLens: Explain Selection**. The answer appears at the top of the sidebar.
6. **Find similar code.** Select code, right-click, **CodeLens: Find Similar Code**.

The dot next to "CodeLens" shows the state: **green** means everything works, **amber** means Ollama is off or some chunks have no vectors yet (keyword search still works; hover for details), **red** means the backend isn't running (run **CodeLens: Restart Backend**, then check **CodeLens: Show Logs**).

The extension uses a backend that is already running on port 8000 (for example [in Docker](#-run-with-docker)), and otherwise starts its own.

### Terminal use

```bash
./start.sh      # backend on http://127.0.0.1:8000
curl -N -X POST localhost:8000/index -H 'Content-Type: application/json' -d "{\"repo_path\": \"$PWD\"}"
curl -X POST localhost:8000/query -H 'Content-Type: application/json' \
     -d "{\"query\": \"where do we retry failed requests -in:tests\", \"repo_path\": \"$PWD\"}"
```

`curl` gets coloured terminal output; send `Accept: application/json` for JSON.

---

## 🦙 Ollama (optional)

CodeLens never installs Ollama for you. Install it the way you prefer, then pull the models:

| | |
|---|---|
| macOS | `brew install ollama` or the app from [ollama.com/download](https://ollama.com/download) |
| Linux | your package manager (`pacman -S ollama`, `nix-env -iA nixpkgs.ollama`, ...), or [ollama.com/download](https://ollama.com/download) |
| Windows | the installer from [ollama.com/download](https://ollama.com/download) |
| Docker | the bundled `docker-compose.yml` runs it for you |

```bash
ollama pull nomic-embed-text   # semantic search (~274 MB)
ollama pull mistral            # Explain (optional; any chat model works, see codelens.explainModel)
```

| Without Ollama | With Ollama |
|---|---|
| Indexing, keyword and name search, filters, repo suggestions, jump to line, copy | All of that, plus search by meaning and Explain |

Started Ollama after indexing? Click **Re-index**: only the missing vectors are computed.

---

## 🐳 Run with Docker

Runs the backend, and optionally Ollama, in containers. The VS Code extension finds the backend on port 8000 and uses it instead of starting its own.

```bash
docker compose up -d --build                 # backend + Ollama; pulls nomic-embed-text on first start
docker compose logs -f backend               # watch it start
curl localhost:8000/health                   # {"ollama": true, ...}
```

Already running Ollama on your machine? Start only the backend and point it at yours:

```bash
OLLAMA_HOST=http://host.docker.internal:11434 docker compose up -d --build --no-deps backend
```

Things to know:

- **Repos are indexed by their absolute path**, so your home folder is mounted read-only at the same path inside the container. To index a repo outside your home, add a line under `volumes:` such as `- /srv/code:/srv/code:ro`.
- **Explain** needs its model pulled into the Ollama container: `docker compose exec ollama ollama pull mistral`. Pick another with `EXPLAIN_MODEL=llama3.2 docker compose up -d`.
- The index lives in the `codelens_data` volume and models in `ollama_data`. `docker compose down -v` deletes both.
- Ports are published on `127.0.0.1` only: the API reads your source code.
- To stop the extension from starting a second backend, set `codelens.autoStartBackend` to `false`.

---

## 🔎 Search syntax

Type plain words, a question, or a symbol name. Add filters anywhere:

| Filter | Matches |
|---|---|
| `in:<name>` | Paths with a folder or file starting with `<name>`, at any depth, ignoring case. `in:api`, `in:src/auth`, `in:"my dir"`. Several `in:` filters are ORed. |
| `-in:<name>` | Excludes those paths. `-in:tests` |
| `lang:<lang>` | `py`, `ts` (includes TSX), `tsx`, `js`, `go`, `rs`, `java`, `cpp` (C and C++), `cuda`, `md`, `yaml`, `toml` |
| `kind:<kind>` | `function`, `method`, `class`, `interface`, `type`, `struct`, `impl`, `module`, `text` (docs and config) |

A query made only of filters, such as `kind:class in:models`, lists every matching chunk. Words with a colon that aren't one of these keys (`http://`, `12:30`) are searched as text.

### Keys in the sidebar

| Key | Action |
|---|---|
| Type | Search as you type |
| `↑` `↓` | Select a result |
| `Enter` | Open the selected result, or search |
| `Esc` | Clear the selection, then the query |
| `Cmd/Ctrl+Alt+K` | Focus CodeLens search |
| Shift+click **Re-index** | Rebuild the index from scratch |

Commands: **CodeLens: Search**, **Explain Selection** and **Find Similar Code** (editor context menu on a selection), **Re-index Workspace**, **Rebuild Index From Scratch**, **Set Up Search Engine**, **Restart Backend**, **Show Logs**.

### Settings

| Setting | Default | |
|---|---|---|
| `codelens.pythonPath` | *(empty)* | Python for the backend. Empty: the backend folder's `.venv`, then CodeLens's own environment, then `python3` |
| `codelens.backendPath` | *(empty)* | Folder containing `backend/main.py`. Empty: the copy bundled in the extension |
| `codelens.port` | `8000` | Backend port |
| `codelens.autoStartBackend` | `true` | Start the backend unless one is already running |
| `codelens.ollamaHost` | *(empty)* | Ollama address for a backend the extension starts. Empty: `http://localhost:11434` |
| `codelens.explainModel` | *(empty)* | Ollama model for Explain, e.g. `mistral`, `llama3.2`, `qwen2.5-coder`. Empty: `mistral` |

Backend environment variables (or `.env`): `CODELENS_DB_PATH` (default `~/.codelens`), `OLLAMA_HOST`, `EMBED_MODEL`, `EXPLAIN_MODEL` (default `mistral`), `MAX_FILE_BYTES` (default 512 KB), `EMBED_BATCH_SIZE`, `CODELENS_USE_SQLITE_VEC=0` to force the numpy backend.

---

## 📊 Measured numbers

Measured on this repository (24 files) on an Apple Silicon Mac with `nomic-embed-text` in Ollama:

| Metric | Result |
|---|---|
| First index (217 chunks, all embedded) | 9.3 s |
| Re-index with nothing changed | 33 ms |
| File saved → searchable | ~1–2 s (1 s debounce + embedding) |
| Query, warm | 30–60 ms, most of it embedding the query |
| Query with only filters | < 5 ms |
| Index size | 4.6 MB |

---

## 🔌 API

The backend only answers local requests: requests with a non-local `Host` header (DNS rebinding) or a browser `Origin` are rejected, and there is no CORS, so web pages you visit can't read your code through it.

| Endpoint | |
|---|---|
| `POST /index` `{repo_path, force_reindex?}` | Server-sent events: `start`, `progress` (`phase: scan\|embed`, `done`, `total`, `file`), `error` (per file, non-fatal), `complete` (counts, `embed_error`, `duration_ms`). Starts watching the repo. `409` if that repo is already being indexed. |
| `POST /query` `{query, repo_path?, top_k?, explain?, live?}` | `results` best first (file, lines, symbol, kind, language, code, `score` = cosine similarity or `null`, `match` = `both\|semantic\|keyword\|filter`), parsed `filters`, `terms`, `warnings`, `explain_text`, `query_ms`, `total_indexed` |
| `POST /explain` `{code, file_path?, symbol_name?, language?, query?}` or `{query, repo_path?, top_k?}` | `explain_text`, `model`, `ms`. With `code`, explains that snippet without searching. With only `query`, searches and explains the top results. `503` with a fix-it message if Ollama or the model is missing. |
| `GET /suggestions?repo_path=` | `suggestions` (example searches built from the repo's symbols), `languages`, `kinds` |
| `GET /status?repo_path=` | Chunk, file and embedded counts, vector backend, indexing and watching state, known repos |
| `GET /health` | Ollama reachability, whether the embedding model is pulled, database state |

---

## 🔒 Privacy

Everything runs on your machine: no telemetry, no accounts, no network access except to your local Ollama. The index lives in `~/.codelens/` and can be deleted at any time. Files ignored by git, files over 512 KB, and minified or binary files are never indexed.

---

## 📁 Project structure

```
backend/
  main.py               FastAPI app: /index (SSE), /query, /status, /health; repo registry; local-only guard
  indexer.py            File listing (git-aware), incremental sync, batched embedding, debounced watcher
  tree_sitter_parser.py Symbol-aware chunking for 8 languages, sliding windows for docs and config
  db_client.py          SQLite schema, FTS5 and sqlite-vec indexes, numpy fallback
  query.py              Filter parsing, hybrid search, rank fusion, explain
  suggest.py            Example searches built from a repo's indexed symbols
  embedder.py           Batched Ollama embeddings with a query cache
  text.py               Identifier splitting and FTS query building
extension/
  src/extension.ts      Backend discovery, first-run environment setup, commands, status bar
  src/searchPanel.ts    Sidebar webview provider, jump-to-line, indexing progress
  src/client.ts         HTTP/SSE client (Node's built-in fetch)
  media/panel.html      Sidebar UI (strict CSP, all backend data escaped)
shared/types.ts         API wire types
tests/                  pytest suite (runs against both sqlite-vec and numpy backends)
docs/demo.gif           README demo (not shipped in the .vsix)
scripts/make_demo_gif.sh  Turn a screen recording into docs/demo.gif
```

---

## 🧪 Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest          # backend tests, no Ollama needed (a fake embedder is used)

npm install
npm run watch                       # then F5 in VS Code
npm run package                     # builds codelens-<version>.vsix, backend included
```

CI runs the tests on Linux and macOS with Python 3.10 and 3.12, compiles the extension and uploads the `.vsix`. Pushing a tag like `v0.4.0` also publishes the `.vsix` on the Releases page.

To refresh the demo GIF from a screen recording (needs ffmpeg):

```bash
scripts/make_demo_gif.sh recording.mp4          # writes docs/demo.gif, 12 fps, 800 px wide
```

---

## 🗺️ Roadmap

- [x] Hybrid keyword + semantic search with rank fusion
- [x] `in:` / `lang:` / `kind:` filters
- [x] Search as you type
- [x] Incremental indexing that survives Ollama being offline
- [x] Per-repo indexes, `.gitignore` support
- [x] Explain any snippet without searching
- [x] Search suggestions built from the repo
- [x] One-click backend setup from the extension; Ollama optional
- [ ] Group near-duplicate results (copy-pasted code) into one card, with the rest a key away
- [ ] A `codelens` CLI (`codelens search`, `codelens index`, `codelens stats`)
- [ ] Index the most recently changed files first, so search is useful while a large repo indexes
- [ ] Optional reranking of the top results with a small local cross-encoder
- [ ] "Find usages by meaning": rank callers of the selected function

---

## 📄 License

MIT. See [LICENSE](LICENSE).

---

<div align="center">

**Built For hackathon & I literally killed it.** · April 2026

*CodeLens is proof that developer tools don't need the cloud to be powerful.*

</div>
