"""
Indexer: lists a repo's files, chunks them with tree-sitter, stores the chunks
and their keyword index, then embeds whatever has no vector yet.

Incremental by construction:
 • a file is re-chunked only when its content hash changed,
 • files that disappeared are removed,
 • a vector is computed only for chunk content that has none, so a function
   that merely moved keeps its vector but gets its new line numbers,
 • if Ollama is down, chunks are still stored (keyword search works) and the
   missing vectors are filled in on the next run.
"""

import os
import time
import hashlib
import logging
import subprocess
import threading
from typing import Any, Callable, Iterable, Optional

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from backend.config import Settings
from backend.db_client import CodeLensDB, get_db
from backend.embedder import Embedder, EmbeddingError, get_embedder
from backend.tree_sitter_parser import LANGUAGES, TEXT_LANGUAGES, extract_chunks, sliding_window

logger = logging.getLogger(__name__)

IGNORE_DIRS = {"node_modules", ".git", "vendor", "dist", "__pycache__", "build", ".venv", "venv",
               ".vectorai_db", ".codelens", "out", "target", ".next", ".nuxt", ".tox",
               ".mypy_cache", ".pytest_cache", ".ruff_cache", "coverage", ".gradle", ".idea"}

SUPPORTED_EXTENSIONS = set(LANGUAGES) | set(TEXT_LANGUAGES)

ProgressFn = Callable[[dict], None]


def md5_hash(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.md5(data).hexdigest()


def embed_text(file_path: str, symbol_name: str, chunk_text: str) -> str:
    """What gets embedded: location and name give the model useful context."""
    return f"{file_path} {symbol_name}\n{chunk_text}"[:Settings.EMBED_MAX_CHARS]


def is_supported_path(rel_path: str) -> bool:
    parts = rel_path.replace("\\", "/").split("/")
    if any(p in IGNORE_DIRS for p in parts[:-1]):
        return False
    return os.path.splitext(parts[-1])[1].lower() in SUPPORTED_EXTENSIONS


def looks_generated(data: bytes) -> bool:
    """Binary, minified or bundled files make poor search results."""
    if b"\0" in data[:8192]:
        return True
    lines = data.count(b"\n") + 1
    return len(data) / lines > 300 and len(data) > 20_000


def chunk_content(rel_path: str, content: str, repo: str = "") -> list[dict]:
    ext = os.path.splitext(rel_path)[1].lower()
    if ext in LANGUAGES:
        chunks = extract_chunks(rel_path, content)
    else:
        chunks = sliding_window(rel_path, content, TEXT_LANGUAGES.get(ext, "text"))
    for c in chunks:
        c["content_hash"] = md5_hash(
            repo + "\0" + embed_text(rel_path, c["symbol_name"], c["chunk_text"]))
    return chunks


def chunk_file(file_path: str, repo_root: str) -> list[dict]:
    """Read and chunk one file (kept for scripts that import it)."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError as e:
        logger.warning(f"Could not read {file_path}: {e}")
        return []
    rel = os.path.relpath(file_path, repo_root).replace(os.sep, "/")
    return chunk_content(rel, content, os.path.realpath(repo_root)) if content.strip() else []


class Indexer:
    def __init__(self, repo_path: str, db: Optional[CodeLensDB] = None,
                 embedder: Optional[Embedder] = None):
        self.repo = os.path.realpath(repo_path)
        self.repo_path = self.repo
        self.db = db or get_db()
        self._embedder = embedder
        self._lock = threading.Lock()
        self.indexing = False
        self.last_result: Optional[dict] = None
        self.observer: Optional[Observer] = None
        self._is_git: Optional[bool] = None

    @property
    def embedder(self) -> Embedder:
        return self._embedder or get_embedder()

    # ------------------------------------------------------------------
    # File listing
    # ------------------------------------------------------------------

    def _git(self, *args: str, input: Optional[bytes] = None) -> Optional[bytes]:
        try:
            res = subprocess.run(["git", "-C", self.repo, *args], input=input,
                                 capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return res.stdout if res.returncode in (0, 1) else None

    @property
    def is_git(self) -> bool:
        if self._is_git is None:
            out = self._git("rev-parse", "--is-inside-work-tree")
            self._is_git = bool(out and out.strip() == b"true")
        return self._is_git

    def list_files(self) -> list[str]:
        """Repo-relative POSIX paths of indexable files, honouring .gitignore."""
        if self.is_git:
            out = self._git("ls-files", "-z", "--cached", "--others", "--exclude-standard")
            if out is not None:
                paths = {p for p in out.decode("utf-8", errors="replace").split("\0") if p}
                return sorted(p for p in paths
                              if is_supported_path(p) and os.path.isfile(os.path.join(self.repo, p)))
        files = []
        for root, dirs, names in os.walk(self.repo):
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
            for name in names:
                rel = os.path.relpath(os.path.join(root, name), self.repo).replace(os.sep, "/")
                if is_supported_path(rel):
                    files.append(rel)
        return sorted(files)

    def walk_repo(self) -> Iterable[str]:
        """Absolute paths (back-compat)."""
        return (os.path.join(self.repo, p) for p in self.list_files())

    def _git_ignored(self, rel_paths: list[str]) -> set[str]:
        if not rel_paths or not self.is_git:
            return set()
        out = self._git("check-ignore", "--stdin", "-z", input="\0".join(rel_paths).encode() + b"\0")
        return {p for p in (out or b"").decode("utf-8", errors="replace").split("\0") if p}

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    def _read(self, rel: str) -> Optional[bytes]:
        path = os.path.join(self.repo, rel)
        try:
            if os.path.getsize(path) > Settings.MAX_FILE_BYTES:
                return None
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return None
        return None if looks_generated(data) else data

    def _sync_file(self, rel: str, known_hash: Optional[str]) -> tuple[str, int]:
        """Bring one file's chunks up to date. Returns (status, chunk count)."""
        data = self._read(rel)
        if data is None or not data.strip():
            if known_hash is not None:
                self.db.remove_file(self.repo, rel)
                return "removed", 0
            return "skipped", 0
        file_hash = md5_hash(data)
        if file_hash == known_hash:
            return "unchanged", 0
        chunks = chunk_content(rel, data.decode("utf-8", errors="replace"), self.repo)
        self.db.replace_file(self.repo, rel, file_hash, chunks)
        return "changed", len(chunks)

    def _embed_missing(self, progress: Optional[ProgressFn]) -> tuple[int, Optional[str]]:
        missing = self.db.chunks_missing_embeddings(self.repo)
        total, done = len(missing), 0
        batch_size = max(1, Settings.EMBED_BATCH_SIZE)
        for i in range(0, total, batch_size):
            batch = missing[i:i + batch_size]
            texts = [embed_text(path, symbol, text) for _, path, symbol, text in batch]
            try:
                vectors = self.embedder.embed_documents(texts)
            except EmbeddingError as e:
                logger.error(str(e))
                return done, str(e)
            self.db.put_embeddings(self.repo, [(h, v) for (h, *_), v in zip(batch, vectors)])
            done += len(batch)
            if progress:
                progress({"type": "progress", "phase": "embed", "done": done, "total": total,
                          "file": batch[-1][1]})
        return done, None

    def index(self, force: bool = False, progress: Optional[ProgressFn] = None) -> dict[str, Any]:
        """Full incremental index of the repo. Safe to call repeatedly."""
        with self._lock:
            self.indexing = True
            try:
                return self._index(force, progress)
            finally:
                self.indexing = False

    def _index(self, force: bool, progress: Optional[ProgressFn]) -> dict[str, Any]:
        start = time.time()
        if force:
            self.db.clear_repo(self.repo)
        files = self.list_files()
        known = self.db.file_hashes(self.repo)
        counts = {"changed": 0, "unchanged": 0, "removed": 0, "skipped": 0}

        for rel in set(known) - set(files):
            self.db.remove_file(self.repo, rel)
            counts["removed"] += 1

        total_chunks = 0
        for i, rel in enumerate(files):
            try:
                status, n = self._sync_file(rel, known.get(rel))
            except Exception as e:  # one bad file must not stop the run
                logger.exception(f"Failed to index {rel}")
                if progress:
                    progress({"type": "error", "message": str(e), "file": rel})
                continue
            counts[status] += 1
            total_chunks += n
            if progress:
                progress({"type": "progress", "phase": "scan", "done": i + 1, "total": len(files),
                          "file": rel, "chunks": n, "status": status})

        embedded, embed_error = self._embed_missing(progress)
        self.db.prune_embeddings()
        result = {
            "type": "complete",
            "files": len(files),
            **counts,
            "new_chunks": total_chunks,
            "total_chunks": self.db.count(self.repo),
            "embedded": embedded,
            "embed_error": embed_error,
            "duration_ms": int((time.time() - start) * 1000),
        }
        self.last_result = result
        logger.info(f"Indexed {self.repo}: {result}")
        return result

    def update_paths(self, rel_paths: Iterable[str]) -> dict[str, int]:
        """Re-sync specific files (watcher). Handles creates, edits and deletes."""
        rel_paths = sorted({p for p in rel_paths if is_supported_path(p)})
        if not rel_paths:
            return {}
        with self._lock:
            ignored = self._git_ignored(rel_paths)
            known = self.db.file_hashes(self.repo)
            counts: dict[str, int] = {}
            for rel in rel_paths:
                if rel in ignored or not os.path.isfile(os.path.join(self.repo, rel)):
                    if rel in known:
                        self.db.remove_file(self.repo, rel)
                        counts["removed"] = counts.get("removed", 0) + 1
                    continue
                try:
                    status, _ = self._sync_file(rel, known.get(rel))
                    counts[status] = counts.get(status, 0) + 1
                except Exception:
                    logger.exception(f"Failed to re-index {rel}")
            self._embed_missing(None)
            self.db.prune_embeddings()
            logger.info(f"Watcher update {self.repo}: {counts}")
            return counts

    # Back-compat helpers
    def run_full_index(self):
        return self.index()

    def reindex_file(self, file_path: str):
        rel = os.path.relpath(os.path.realpath(file_path), self.repo).replace(os.sep, "/")
        return self.update_paths([rel])

    # ------------------------------------------------------------------
    # Watching
    # ------------------------------------------------------------------

    def start_watchdog(self, debounce: float = 1.0):
        if self.observer is not None:
            return
        observer = Observer()
        observer.schedule(RepoEventHandler(self, debounce), self.repo, recursive=True)
        observer.daemon = True
        try:
            observer.start()
        except OSError as e:  # e.g. inotify watch limit reached
            logger.error(f"Could not watch {self.repo}: {e}")
            return
        self.observer = observer
        logger.info(f"Watching {self.repo}")

    def stop_watchdog(self):
        if self.observer is not None:
            self.observer.stop()
            self.observer.join(timeout=5)
            self.observer = None


class RepoEventHandler(FileSystemEventHandler):
    """Collects changed paths and re-syncs them once edits go quiet."""

    def __init__(self, indexer: Indexer, debounce: float = 1.0):
        self.indexer = indexer
        self.debounce = debounce
        self._pending: set[str] = set()
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    def _rel(self, path) -> Optional[str]:
        if isinstance(path, bytes):
            path = path.decode("utf-8", errors="replace")
        rel = os.path.relpath(os.path.realpath(path), self.indexer.repo).replace(os.sep, "/")
        return None if rel.startswith("..") or not is_supported_path(rel) else rel

    def on_any_event(self, event):
        if event.is_directory or event.event_type in {"opened", "closed", "closed_no_write"}:
            return
        paths = [event.src_path] + ([event.dest_path] if getattr(event, "dest_path", None) else [])
        rels = [r for r in map(self._rel, paths) if r]
        if not rels:
            return
        with self._lock:
            self._pending.update(rels)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _flush(self):
        with self._lock:
            pending, self._pending = self._pending, set()
            self._timer = None
        try:
            self.indexer.update_paths(pending)
        except Exception:
            logger.exception("Watcher re-index failed")
