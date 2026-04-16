import os
import time
import json
import asyncio
import logging
import threading
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# Load explicitly before importing internal config
load_dotenv()

from backend.config import Settings
from backend.db_client import get_db
from backend.indexer import Indexer, chunk_file
from backend.query import run_query

logger = logging.getLogger(__name__)

SIDECAR_FILE = os.path.join(
    os.path.dirname(Settings.VECTORAI_DB_PATH) if os.path.dirname(Settings.VECTORAI_DB_PATH) else ".",
    "last_repo.json"
)
global_indexer: Indexer | None = None

def get_last_repo() -> str | None:
    if os.path.exists(SIDECAR_FILE):
        try:
            with open(SIDECAR_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("repo_path")
        except Exception:
            pass
    return None

def save_last_repo(path: str):
    try:
        with open(SIDECAR_FILE, "w", encoding="utf-8") as f:
            json.dump({"repo_path": path, "last_indexed": datetime.now().isoformat()}, f)
    except Exception as e:
        logger.error(f"Failed to save sidecar tracker: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global global_indexer
    
    # Init DB explicitly to load resources safely
    db = get_db()
    
    print("\n" + "="*60)
    print("🚀 CodeLens Backend Engine Starting...")
    print(f"📁 VectorAI DB Path: {Settings.VECTORAI_DB_PATH}")
    print(f"📦 Total Chunks in DB: {db.count()}")
    
    import urllib.request
    try:
        urllib.request.urlopen(f"{Settings.OLLAMA_HOST}/api/tags", timeout=2)
        print("🟢 Ollama Status: ONLINE")
    except Exception as e:
        print(f"🔴 Ollama Status: OFFLINE ({e})")
        
    last_repo = get_last_repo()
    if last_repo and os.path.exists(last_repo) and os.path.isdir(last_repo):
        print(f"🔍 Restoring live file watcher for: {last_repo}")
        global_indexer = Indexer(last_repo)
        global_indexer.start_watchdog()
        
    print("="*60 + "\n")
    
    yield
    
    # Teardown logic
    if global_indexer and global_indexer.observer:
        global_indexer.observer.stop()
        global_indexer.observer.join()

app = FastAPI(title="CodeLens Offline Core", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    status_code = 500
    if isinstance(exc, HTTPException):
        status_code = exc.status_code
    return JSONResponse(
        status_code=status_code,
        content={"error": type(exc).__name__, "detail": str(exc)}
    )

class IndexRequest(BaseModel):
    repo_path: str
    force_reindex: bool = False

class QueryRequest(BaseModel):
    query: str
    top_k: int = Field(8, ge=1, le=20)
    explain: bool = False

def indexer_worker(repo_path: str, force: bool, q: asyncio.Queue, main_loop: asyncio.AbstractEventLoop):
    def send(event: dict | None):
        asyncio.run_coroutine_threadsafe(q.put(event), main_loop)
        
    try:
        indexer = Indexer(repo_path)
        if force:
            # Assuming db_client wrapper provides a clear/destroy mechanism natively
            # Typically you'd truncate records referencing this path
            pass 
            
        files = list(indexer.walk_repo())
        total_files = len(files)
        total_chunks = 0
        start_time = time.time()
        
        local_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(local_loop)
        
        for file_path in files:
            try:
                rel = os.path.relpath(file_path, repo_path)
                chunks = chunk_file(file_path, repo_path)
                chunk_count = len(chunks)
                total_chunks += chunk_count
                
                send({
                    "type": "progress", 
                    "file": rel, 
                    "chunks": chunk_count, 
                    "total_files": total_files
                })
                
                if chunks:
                    local_loop.run_until_complete(indexer.embed_and_store(chunks))
            except Exception as err:
                 send({"type": "error", "message": str(err), "file": str(file_path)})

        local_loop.close()
        
        duration = int((time.time() - start_time) * 1000)
        send({
            "type": "complete", 
            "total_chunks": total_chunks, 
            "duration_ms": duration, 
            "skipped": 0 # Tracked internally by incremental hashes logic
        })
        
        save_last_repo(repo_path)
        global global_indexer
        global_indexer = indexer
        global_indexer.start_watchdog()
        
    except Exception as e:
        send({"type": "error", "message": str(e), "file": "system"})
    finally:
        send(None)

@app.post("/index")
async def api_index(req: IndexRequest):
    if not os.path.exists(req.repo_path) or not os.path.isdir(req.repo_path):
        raise HTTPException(status_code=400, detail="Target repo_path does not exist or is not a strictly defined directory.")

    q: asyncio.Queue = asyncio.Queue()
    main_loop = asyncio.get_running_loop()
    
    t = threading.Thread(target=indexer_worker, args=(req.repo_path, req.force_reindex, q, main_loop))
    t.start()
    
    async def sse_gen():
        while True:
            event = await q.get()
            if event is None:
                break
            yield f"data: {json.dumps(event)}\n\n"
            
    return StreamingResponse(sse_gen(), media_type="text/event-stream")

@app.post("/query")
async def api_query(req: QueryRequest):
    if not req.query.strip():
         raise HTTPException(status_code=400, detail="Query payload empty.")
         
    start_time = time.time()
    db = get_db()
    
    data = await run_query(req.query, top_k=req.top_k, explain=req.explain)
    
    query_ts = int((time.time() - start_time) * 1000)
    total_indexed = db.count()
    
    logger.info(f"Query: '{req.query}' | Results: {len(data['results'])} | Execution: {query_ts}ms")
    
    return {
        "results": data["results"],
        "explain_text": data.get("explain_text"),
        "query_ms": query_ts,
        "total_indexed": total_indexed
    }

@app.get("/status")
async def api_status():
    db = get_db()
    
    last_repo = get_last_repo()
    last_indexed = None
    
    if os.path.exists(SIDECAR_FILE):
        try:
            with open(SIDECAR_FILE, "r", encoding="utf-8") as f:
                file_data = json.load(f)
                last_indexed = file_data.get("last_indexed")
        except Exception:
            pass
            
    return {
        "indexed_chunks": db.count(),
        "last_indexed": last_indexed,
        "db_path": Settings.VECTORAI_DB_PATH,
        "embed_model": Settings.EMBED_MODEL,
        "watching": global_indexer is not None and global_indexer.observer is not None
    }

@app.get("/health")
async def api_health():
    import urllib.request
    ollama_ok = False
    ollama_err = None
    
    try:
        urllib.request.urlopen(f"{Settings.OLLAMA_HOST}/api/tags", timeout=1)
        ollama_ok = True
    except Exception as e:
        ollama_err = str(e)
        
    db_ok = False
    db_dir = os.path.dirname(Settings.VECTORAI_DB_PATH)
    if os.path.exists(Settings.VECTORAI_DB_PATH) or (db_dir and os.path.exists(db_dir)):
        db_ok = True
        
    return {
        "ollama": ollama_ok,
        "vectorai": db_ok,
        "ollama_error": ollama_err
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, reload=True)
