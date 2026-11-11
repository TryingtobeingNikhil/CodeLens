import os
import time
import json
import asyncio
import logging
import threading
import urllib.request
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv

# Load explicitly before importing internal config
load_dotenv()

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from backend.config import Settings
from backend.db_client import get_db
from backend.indexer import Indexer
from backend.query import ExplainError, explain_code, explain_results, run_query, search
from backend.suggest import suggest

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

REPOS_FILE = os.path.join(Settings.DB_PATH, "repos.json")
LEGACY_SIDECAR = "last_repo.json"

_indexers: dict[str, Indexer] = {}
_registry_lock = threading.Lock()


# ----------------------------------------------------------------------
# Repo registry: one Indexer (and watcher) per indexed repo
# ----------------------------------------------------------------------

def load_repos() -> dict[str, dict]:
    repos: dict[str, dict] = {}
    for path in (REPOS_FILE, LEGACY_SIDECAR):
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if "repos" in data:
            repos.update(data["repos"])
        elif data.get("repo_path"):  # legacy single-repo sidecar
            repos.setdefault(os.path.realpath(data["repo_path"]),
                             {"last_indexed": data.get("last_indexed")})
    return repos


def save_repo(repo: str):
    with _registry_lock:
        repos = load_repos()
        repos[repo] = {"last_indexed": datetime.now().isoformat(timespec="seconds")}
        try:
            os.makedirs(os.path.dirname(REPOS_FILE), exist_ok=True)
            with open(REPOS_FILE, "w", encoding="utf-8") as f:
                json.dump({"repos": repos}, f, indent=2)
        except OSError as e:
            logger.error(f"Failed to save repo registry: {e}")


def get_indexer(repo: str) -> Indexer:
    with _registry_lock:
        if repo not in _indexers:
            _indexers[repo] = Indexer(repo)
        return _indexers[repo]


def _resolve_repo(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    repo = os.path.realpath(os.path.expanduser(path))
    if not os.path.isdir(repo):
        raise HTTPException(status_code=400, detail=f"Not a directory: {path}")
    return repo


def _ollama_status() -> tuple[bool, bool, Optional[str]]:
    """(reachable, embed model pulled, error)"""
    try:
        with urllib.request.urlopen(f"{Settings.OLLAMA_HOST}/api/tags", timeout=2) as resp:
            models = {m.get("name", "") for m in json.load(resp).get("models", [])}
        pulled = any(m == Settings.EMBED_MODEL or m.startswith(Settings.EMBED_MODEL + ":")
                     for m in models)
        return True, pulled, None
    except Exception as e:
        return False, False, str(e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = get_db()
    ok, pulled, err = _ollama_status()
    print("\n" + "=" * 60)
    print("CodeLens backend starting")
    print(f"  Index:   {db.db_file} ({db.count()} chunks, vectors via {db.stats()['vector_backend']})")
    if ok:
        print(f"  Ollama:  online{'' if pulled else f' (model {Settings.EMBED_MODEL} not pulled)'}")
    else:
        print(f"  Ollama:  OFFLINE ({err}); keyword search still works")
    for repo in load_repos():
        if os.path.isdir(repo):
            print(f"  Watching {repo}")
            get_indexer(repo).start_watchdog()
    print("=" * 60 + "\n")

    yield

    for indexer in list(_indexers.values()):
        indexer.stop_watchdog()


app = FastAPI(title="CodeLens", lifespan=lifespan)

# The API reads your source code, so it only answers local, non-browser callers.
# No CORS: a web page you visit must not be able to query or index your disk,
# and the Host check blocks DNS-rebinding attacks.
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "testserver"}


@app.middleware("http")
async def local_only(request: Request, call_next):
    host = request.headers.get("host", "")
    host = host[:host.index("]") + 1] if host.startswith("[") and "]" in host else host.split(":")[0]
    origin = request.headers.get("origin")
    if host not in _LOCAL_HOSTS or (origin and not origin.startswith("vscode-webview://")):
        return JSONResponse(status_code=403, content={"error": "Forbidden",
                                                      "detail": "CodeLens only accepts local requests"})
    return await call_next(request)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error")
    return JSONResponse(status_code=500, content={"error": type(exc).__name__, "detail": str(exc)})


class IndexRequest(BaseModel):
    repo_path: str
    force_reindex: bool = False


class ExplainRequest(BaseModel):
    """Either `code` (explain this snippet) or `query` (search, then explain the top results)."""
    code: Optional[str] = Field(None, max_length=200_000)
    file_path: str = ""
    symbol_name: str = ""
    language: str = ""
    query: Optional[str] = None
    repo_path: Optional[str] = None
    top_k: int = Field(5, ge=1, le=20)


class QueryRequest(BaseModel):
    query: str
    top_k: int = Field(8, ge=1, le=50)
    explain: bool = False
    repo_path: Optional[str] = None
    live: bool = False  # search-as-you-type: last word matched as a prefix


@app.post("/index")
async def api_index(req: IndexRequest):
    repo = _resolve_repo(req.repo_path)
    indexer = get_indexer(repo)
    if indexer.indexing:
        raise HTTPException(status_code=409, detail="This repo is already being indexed")
    indexer.indexing = True  # claim it now: the worker thread takes the lock later

    q: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def send(event: Optional[dict]):
        loop.call_soon_threadsafe(q.put_nowait, event)

    def worker():
        try:
            send({"type": "start", "repo": repo})
            result = indexer.index(force=req.force_reindex, progress=send)
            save_repo(repo)
            indexer.start_watchdog()
            send(result)
        except Exception as e:
            logger.exception("Indexing failed")
            send({"type": "error", "message": str(e), "file": "system"})
        finally:
            send(None)

    threading.Thread(target=worker, name=f"index:{os.path.basename(repo)}", daemon=True).start()

    async def sse():
        while True:
            event = await q.get()
            if event is None:
                break
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _format_terminal(query: str, data: dict) -> str:
    """Coloured plain-text results for `curl` demos."""
    lines = [f"\n🎯 \033[1;32mSearch Results for:\033[0m '{query}' \033[90m({data['query_ms']}ms)\033[0m"]
    lines.append("\033[90m" + "━" * 80 + "\033[0m")
    for w in data.get("warnings", []):
        lines.append(f"\033[33m⚠ {w}\033[0m")
    for i, r in enumerate(data["results"]):
        score = "  -  " if r.get("score") is None else f"{r['score']:.2f}"
        file_info = f"\033[1;36m{r['file_path']}\033[0m:\033[36m{r['line_start']}\033[0m"
        symbol = f"\033[1;33m{r['symbol_name']}\033[0m"
        lines.append(f"\033[1;37m{i + 1}.\033[0m {file_info} ⚡ {symbol}  "
                     f"\033[90m[Score: \033[1;35m{score}\033[90m · {r.get('match', '')}]\033[0m")
        snippet = r["chunk_text"].strip().split("\n")
        for snip_line in snippet[:3]:
            lines.append(f"    \033[90m│\033[0m \033[38;5;250m{snip_line.replace(chr(9), '    ')[:80]}\033[0m")
        if len(snippet) > 3:
            lines.append("    \033[90m│ ...\033[0m")
        lines.append("")
    return "\n".join(lines)


@app.post("/query")
async def api_query(req: QueryRequest, request: Request):
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="Query is empty")
    repo = _resolve_repo(req.repo_path)

    data = await run_query(req.query, top_k=req.top_k, explain=req.explain, repo=repo, live=req.live)
    data["total_indexed"] = get_db().count(repo)
    logger.info(f"Query {req.query!r} -> {len(data['results'])} results in {data['query_ms']}ms")

    if "curl" in request.headers.get("user-agent", "").lower() and \
            "application/json" not in request.headers.get("accept", ""):
        return Response(content=_format_terminal(req.query, data), media_type="text/plain")
    return data


@app.post("/explain")
async def api_explain(req: ExplainRequest):
    """Explain code with the local LLM. Independent of search: send a snippet and get
    an explanation. With only `query`, the top search results are explained instead."""
    start = time.time()
    try:
        if req.code and req.code.strip():
            text = await asyncio.to_thread(explain_code, req.code, req.file_path, req.symbol_name,
                                           req.language, req.query)
        elif req.query and req.query.strip():
            repo = _resolve_repo(req.repo_path)
            found = await asyncio.to_thread(search, req.query, repo, req.top_k)
            if not found["results"]:
                raise HTTPException(status_code=404, detail="No results to explain")
            text = await asyncio.to_thread(explain_results, req.query, found["results"])
        else:
            raise HTTPException(status_code=400, detail="Send `code` or `query`")
    except ExplainError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"explain_text": text, "model": Settings.EXPLAIN_MODEL, "ms": int((time.time() - start) * 1000)}


@app.get("/suggestions")
async def api_suggestions(repo_path: Optional[str] = None):
    """Example searches built from the symbols indexed for this repo."""
    repo = _resolve_repo(repo_path)
    return await asyncio.to_thread(suggest, repo)


@app.get("/status")
async def api_status(repo_path: Optional[str] = None):
    repo = _resolve_repo(repo_path)
    db = get_db()
    stats = db.stats(repo)
    repos = load_repos()
    indexer = _indexers.get(repo) if repo else None
    return {
        "indexed_chunks": stats["chunks"],
        **stats,
        "repo": repo,
        "last_indexed": repos.get(repo, {}).get("last_indexed") if repo else None,
        "repos": sorted(repos),
        "embed_model": Settings.EMBED_MODEL,
        "indexing": bool(indexer and indexer.indexing),
        "watching": bool(indexer and indexer.observer) if repo
                    else any(i.observer for i in _indexers.values()),
        "last_result": indexer.last_result if indexer else None,
    }


@app.get("/health")
@app.get("/api/v1/health", include_in_schema=False)
async def api_health():
    ok, pulled, err = await asyncio.to_thread(_ollama_status)
    try:
        db = get_db()
        db.count()
        db_ok, backend = True, db.stats()["vector_backend"]
    except Exception as e:
        db_ok, backend = False, str(e)
    return {
        "ollama": ok,
        "embed_model": Settings.EMBED_MODEL,
        "embed_model_pulled": pulled,
        "ollama_error": err,
        "explain_model": Settings.EXPLAIN_MODEL,
        "db": db_ok,
        "vectorai": db_ok,  # legacy field
        "vector_backend": backend,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="127.0.0.1", port=int(os.getenv("CODELENS_PORT", "8000")))
