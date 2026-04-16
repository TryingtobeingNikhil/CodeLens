"""
Indexer — walks a repo, AST-chunks files, embeds with Ollama,
and upserts into the VectorAI DB (SQLite+numpy adapter).

Changes vs original:
 • tree-sitter 0.22+ API (Language one-arg, Parser(language))
 • ollama 0.5+ SDK: response is an object, not a dict (.embedding not ['embedding'])
 • Uses tree_sitter_parser.extract_chunks / _sliding_window exclusively
 • Point.row instead of tuple indexing for start/end
"""

import os
import hashlib
import asyncio
import logging
from typing import List, Generator, Dict, Any
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from rich.progress import Progress, TextColumn, BarColumn, TaskProgressColumn, TimeElapsedColumn
from ollama import AsyncClient

from backend.db_client import get_db
from backend.config import Settings
from backend.tree_sitter_parser import LANGUAGES, extract_chunks, _sliding_window

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

IGNORE_DIRS = {"node_modules", ".git", "vendor", "dist", "__pycache__", "build",
               ".venv", "venv", ".vectorai_db", "out"}

SUPPORTED_EXTENSIONS = set(LANGUAGES.keys()) | {".md", ".txt", ".toml", ".yaml", ".yml"}


def md5_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def chunk_file(file_path: str, repo_root: str) -> List[Dict[str, Any]]:
    """Read one file, run AST chunking, attach content_hash to every chunk."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        logger.warning(f"Could not read {file_path}: {e}")
        return []

    if not content.strip():
        return []

    rel_path = os.path.relpath(file_path, repo_root)
    ext = os.path.splitext(file_path)[1].lower()

    if ext in LANGUAGES:
        raw_chunks = extract_chunks(rel_path, content)
    else:
        raw_chunks = _sliding_window(rel_path, content, "unknown")

    # Attach content_hash for incremental-index dedup
    for chunk in raw_chunks:
        chunk["content_hash"] = md5_hash(chunk["chunk_text"])

    return raw_chunks


class Indexer:
    def __init__(self, repo_path: str):
        self.repo_path = repo_path
        self.db = get_db()
        self.observer: Observer | None = None
        self.ollama_client = AsyncClient(host=Settings.OLLAMA_HOST)

    def walk_repo(self) -> Generator[str, None, None]:
        for root, dirs, files in os.walk(self.repo_path):
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
            for file in files:
                ext = os.path.splitext(file)[1].lower()
                if ext in SUPPORTED_EXTENSIONS:
                    yield os.path.join(root, file)

    async def _embed_text(self, text: str) -> List[float]:
        """Embed one text string. Returns zero vector on failure."""
        try:
            resp = await self.ollama_client.embeddings(
                model=Settings.EMBED_MODEL,
                prompt=f"search_document: {text}"
            )
            # ollama SDK 0.5+: resp is an EmbeddingsResponse object
            return list(resp.embedding) if hasattr(resp, "embedding") else resp["embedding"]
        except Exception as e:
            logger.error(f"Ollama embed error: {e}")
            return [0.0] * 768

    async def embed_and_store(self, chunks: List[Dict[str, Any]]):
        if not chunks:
            return

        # Skip already-indexed chunks (content-hash dedup)
        existing = set(self.db.get_existing_hashes([c["content_hash"] for c in chunks]))
        new_chunks = [c for c in chunks if c["content_hash"] not in existing]
        if not new_chunks:
            return

        batch_size = 20
        for i in range(0, len(new_chunks), batch_size):
            batch = new_chunks[i : i + batch_size]
            embeddings = [await self._embed_text(c["chunk_text"]) for c in batch]
            self.db.batch_upsert(batch, embeddings)

    def run_full_index(self):
        files = list(self.walk_repo())
        all_chunks: List[Dict[str, Any]] = []

        with Progress(
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
        ) as progress:
            parse_task = progress.add_task("Parsing files", total=len(files))
            for fp in files:
                chunks = chunk_file(fp, self.repo_path)
                all_chunks.extend(chunks)
                progress.update(parse_task, description=f"[cyan]{os.path.basename(fp)}")
                progress.advance(parse_task)

            embed_task = progress.add_task("Embedding chunks", total=len(all_chunks))

            async def _run():
                batch_size = 20
                for i in range(0, len(all_chunks), batch_size):
                    batch = all_chunks[i : i + batch_size]
                    await self.embed_and_store(batch)
                    progress.advance(embed_task, advance=len(batch))

            asyncio.run(_run())

        logger.info(f"Indexed {len(files)} files → {len(all_chunks)} chunks total.")

    async def reindex_file_async(self, file_path: str):
        rel_path = os.path.relpath(file_path, self.repo_path)
        self.db.delete_by_filepath(rel_path)
        chunks = chunk_file(file_path, self.repo_path)
        await self.embed_and_store(chunks)
        logger.info(f"Re-indexed <{rel_path}> ({len(chunks)} chunks).")

    def reindex_file(self, file_path: str):
        asyncio.run(self.reindex_file_async(file_path))

    def start_watchdog(self):
        if self.observer is not None:
            return
        handler = RepoEventHandler(self)
        self.observer = Observer()
        self.observer.schedule(handler, self.repo_path, recursive=True)
        self.observer.start()
        logger.info(f"Watchdog watching: {self.repo_path}")


class RepoEventHandler(FileSystemEventHandler):
    def __init__(self, indexer: Indexer):
        self.indexer = indexer

    def on_modified(self, event):
        if event.is_directory:
            return
        ext = os.path.splitext(event.src_path)[1].lower()
        if ext in LANGUAGES:
            parts = event.src_path.split(os.sep)
            if not any(ign in parts for ign in IGNORE_DIRS):
                logger.info(f"File changed: {event.src_path} — re-indexing")
                self.reindex_safe(event.src_path)

    def reindex_safe(self, path: str):
        try:
            self.indexer.reindex_file(path)
        except Exception as e:
            logger.error(f"Re-index failed for {path}: {e}")
