# 🎬 CodeLens — Demo Guide for Judges

## 30-Second Live Demo Script

> **Turn WiFi off before starting. That's the killer moment.**

---

## Step 1 — Setup (one-time, ~2 min)

```bash
git clone https://github.com/TryingtobeingNikhil/CodeLens.git
cd CodeLens
chmod +x setup.sh && ./setup.sh
```

---

## Step 2 — Start the Engine

```bash
chmod +x start.sh && ./start.sh
```

You'll see:
```
🚀 CodeLens Backend Engine Starting...
📁 VectorAI DB Path: ./.vectorai_db
📦 Total Chunks in DB: 0
🟢 Ollama Status: ONLINE
```

---

## Step 3 — Index a Codebase

```bash
# Index THIS repo (eats its own dog food)
curl -X POST http://localhost:8000/index \
  -H "Content-Type: application/json" \
  -d '{"repo_path": "/path/to/CodeLens"}'
```

**Stream output:**
```
{"type": "progress", "file": "backend/indexer.py", "chunks": 16}
{"type": "progress", "file": "extension/src/searchPanel.ts", "chunks": 8}
{"type": "complete", "total_chunks": 93, "duration_ms": 5217}
```

---

## Step 4 — Run These 3 Queries (while offline)

### Query 1: Embedding pipeline
```bash
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"query": "where does code embedding happen?", "top_k": 3}'
```
**Expected:** `_embed_text` and `embed_and_store` in `backend/indexer.py` — score ~0.74

### Query 2: Vector database
```bash
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"query": "vector similarity search function", "top_k": 3}'
```
**Expected:** `search` in `backend/db_client.py` (the cosine ANN function) — score ~0.60

### Query 3: VS Code jump-to-file
```bash
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"query": "jump to file in editor when user clicks result", "top_k": 3}'
```
**Expected:** `handleJumpTo` in `extension/src/searchPanel.ts` (TypeScript) — score ~0.63

---

## Step 5 — Check Status
```bash
curl http://localhost:8000/status
```
```json
{
  "indexed_chunks": 93,
  "embed_model": "nomic-embed-text",
  "watching": true
}
```

---

## Architecture in One Sentence

> Source files → tree-sitter AST → nomic-embed-text (768-dim) → **Actian VectorAI DB** (SQLite+numpy, local) → cosine ANN → VS Code sidebar

---

## Why VectorAI DB?

| Need | VectorAI DB | Alternative |
|------|------------|-------------|
| Offline demo | ✅ Zero internet | ❌ Pinecone needs cloud |
| No config | ✅ Just a directory | ⚠️ Chroma needs server |
| ARM native | ✅ Apple Silicon | ⚠️ FAISS needs build |
| Copy/paste portability | ✅ `cp -r .vectorai_db/` | ❌ Cloud-locked |

---

## Key Numbers from Live Run

| Metric | Value |
|--------|-------|
| Files indexed | 14 |
| Chunks extracted | 93 |
| Index time | **5.2 seconds** |
| Query latency | **53–131 ms** |
| Embedding dims | 768 |
| Model size | 274 MB |
| Internet needed | **ZERO** |
