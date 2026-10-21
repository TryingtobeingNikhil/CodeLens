"""
CodeLens index store: one local SQLite file holding everything.

    files            one row per indexed file (hash for incremental indexing)
    chunks           symbols and text windows with their location
    chunks_fts       FTS5 keyword index over symbol, identifier sub-words, path, body
    embeddings       one vector per distinct chunk content (float32 BLOB, the source of truth)
    vec_embeddings   sqlite-vec index over the same vectors, partitioned by repo

Vector search uses sqlite-vec when the Python build can load SQLite extensions,
and otherwise an in-memory numpy matrix built from the BLOBs. Both give exact
cosine similarity. Writes for a file (chunks, keyword index, vectors) happen in
one transaction, so the indexes never disagree.
"""

import os
import sqlite3
import threading
import logging
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Optional

import numpy as np

from backend.config import Settings
from backend.text import index_terms

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2"

try:
    import sqlite_vec  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    sqlite_vec = None


def _vec_supported() -> bool:
    if sqlite_vec is None or not Settings.USE_SQLITE_VEC:
        return False
    conn = sqlite3.connect(":memory:")
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        return True
    except Exception:
        return False
    finally:
        conn.close()


CHUNK_COLUMNS = ("id", "repo", "file_path", "symbol_name", "kind", "language",
                 "line_start", "line_end", "chunk_text", "content_hash")


class CodeLensDB:
    def __init__(self, path: Optional[str] = None, use_vec: Optional[bool] = None):
        self.path = path or Settings.DB_PATH
        os.makedirs(self.path, exist_ok=True)
        self.db_file = os.path.join(self.path, "codelens.db")
        self.use_vec = _vec_supported() if use_vec is None else (use_vec and _vec_supported())
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._version = 0                      # bumped on every write; invalidates caches
        self._matrix_cache: Optional[tuple] = None
        self._cache_lock = threading.Lock()
        self._init_schema()

    # ------------------------------------------------------------------
    # Connections: one per thread, kept open
    # ------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_file, timeout=30, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            if self.use_vec:
                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
                conn.enable_load_extension(False)
            self._local.conn = conn
        return conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Serialised write transaction."""
        with self._write_lock:
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            finally:
                self._version += 1

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self):
        conn = self._conn()
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "chunks" in tables:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
            if "embedding" in cols:  # v1 schema: rebuildable cache, drop it
                logger.info("Migrating index from schema v1 (a full re-index is needed)")
                conn.execute("DROP TABLE chunks")

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS files (
                repo TEXT NOT NULL, path TEXT NOT NULL, file_hash TEXT NOT NULL,
                PRIMARY KEY (repo, path)
            );
            CREATE TABLE IF NOT EXISTS chunks (
                id           INTEGER PRIMARY KEY,
                repo         TEXT NOT NULL,
                file_path    TEXT NOT NULL,
                symbol_name  TEXT,
                kind         TEXT,
                language     TEXT,
                line_start   INTEGER,
                line_end     INTEGER,
                chunk_text   TEXT,
                content_hash TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(repo, file_path);
            CREATE INDEX IF NOT EXISTS idx_chunks_hash ON chunks(content_hash);
            CREATE TABLE IF NOT EXISTS embeddings (
                id           INTEGER PRIMARY KEY,
                repo         TEXT NOT NULL,
                content_hash TEXT NOT NULL UNIQUE,
                vector       BLOB NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                symbol, terms, path, body, tokenize = 'unicode61 remove_diacritics 2'
            );
        """)
        self._set_meta(conn, "schema_version", SCHEMA_VERSION)

        # A different embedding model makes every stored vector meaningless
        stored_model = self._get_meta(conn, "embed_model")
        if stored_model and stored_model != Settings.EMBED_MODEL:
            logger.warning(f"Embedding model changed ({stored_model} -> {Settings.EMBED_MODEL}); "
                           "dropping vectors, they will be recomputed on the next index")
            conn.execute("DELETE FROM embeddings")
            conn.execute("DELETE FROM meta WHERE key = 'embed_dim'")
            conn.execute("DROP TABLE IF EXISTS vec_embeddings")
        self._set_meta(conn, "embed_model", Settings.EMBED_MODEL)
        conn.commit()

        if self.use_vec:
            self._sync_vec_index()

    @staticmethod
    def _get_meta(conn, key: str) -> Optional[str]:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _set_meta(conn, key: str, value: str):
        conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def _ensure_vec_table(self, conn, dim: int):
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_embeddings USING vec0("
            f"embedding float[{dim}] distance_metric=cosine, repo text partition key)"
        )

    def _sync_vec_index(self):
        """Rebuild the sqlite-vec index if vectors were written without it (e.g. by a
        Python that can't load extensions)."""
        conn = self._conn()
        dim = self._get_meta(conn, "embed_dim")
        if not dim:
            return
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'vec_embeddings'").fetchone()
        dirty = self._get_meta(conn, "vec_dirty") == "1"
        if has_table and not dirty:
            return
        logger.info("Building sqlite-vec index from stored vectors")
        with self._tx() as tx:
            tx.execute("DROP TABLE IF EXISTS vec_embeddings")
            self._ensure_vec_table(tx, int(dim))
            tx.execute("INSERT INTO vec_embeddings(rowid, embedding, repo) "
                       "SELECT id, vector, repo FROM embeddings")
            self._set_meta(tx, "vec_dirty", "0")

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def file_hashes(self, repo: str) -> dict[str, str]:
        rows = self._conn().execute("SELECT path, file_hash FROM files WHERE repo = ?", (repo,))
        return dict(rows.fetchall())

    def _delete_file_rows(self, conn, repo: str, path: str):
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM chunks WHERE repo = ? AND file_path = ?", (repo, path))]
        if ids:
            conn.executemany("DELETE FROM chunks_fts WHERE rowid = ?", [(i,) for i in ids])
            conn.execute("DELETE FROM chunks WHERE repo = ? AND file_path = ?", (repo, path))

    def replace_file(self, repo: str, path: str, file_hash: str, chunks: list[dict]):
        """Atomically replace every chunk of one file. Chunks need `content_hash`."""
        with self._tx() as conn:
            self._delete_file_rows(conn, repo, path)
            for c in chunks:
                cur = conn.execute(
                    "INSERT INTO chunks (repo, file_path, symbol_name, kind, language, "
                    "line_start, line_end, chunk_text, content_hash) VALUES (?,?,?,?,?,?,?,?,?)",
                    (repo, path, c.get("symbol_name", ""), c.get("kind", "block"),
                     c.get("language", ""), c.get("line_start", 0), c.get("line_end", 0),
                     c.get("chunk_text", ""), c["content_hash"]))
                conn.execute(
                    "INSERT INTO chunks_fts (rowid, symbol, terms, path, body) VALUES (?,?,?,?,?)",
                    (cur.lastrowid, c.get("symbol_name", ""),
                     index_terms(c.get("symbol_name", ""), c.get("chunk_text", "")),
                     path.replace("/", " ").replace(".", " "), c.get("chunk_text", "")))
            conn.execute("INSERT INTO files(repo, path, file_hash) VALUES (?,?,?) "
                         "ON CONFLICT(repo, path) DO UPDATE SET file_hash = excluded.file_hash",
                         (repo, path, file_hash))

    def remove_file(self, repo: str, path: str):
        with self._tx() as conn:
            self._delete_file_rows(conn, repo, path)
            conn.execute("DELETE FROM files WHERE repo = ? AND path = ?", (repo, path))

    def clear_repo(self, repo: str):
        with self._tx() as conn:
            conn.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE repo = ?)",
                         (repo,))
            conn.execute("DELETE FROM chunks WHERE repo = ?", (repo,))
            conn.execute("DELETE FROM files WHERE repo = ?", (repo,))
        self.prune_embeddings()

    def chunks_missing_embeddings(self, repo: str) -> list[tuple[str, str, str, str]]:
        """(content_hash, file_path, symbol_name, chunk_text) for chunks without a vector."""
        rows = self._conn().execute("""
            SELECT c.content_hash, MIN(c.file_path), MIN(c.symbol_name), MIN(c.chunk_text)
            FROM chunks c LEFT JOIN embeddings e ON e.content_hash = c.content_hash
            WHERE c.repo = ? AND e.id IS NULL
            GROUP BY c.content_hash
        """, (repo,))
        return rows.fetchall()

    def put_embeddings(self, repo: str, items: list[tuple[str, list[float]]]):
        """Store vectors for content hashes. Zero or empty vectors are refused."""
        rows = []
        for content_hash, vec in items:
            arr = np.asarray(vec, dtype=np.float32)
            if arr.size == 0 or not np.any(arr):
                continue
            rows.append((content_hash, arr))
        if not rows:
            return
        dim = rows[0][1].size
        with self._tx() as conn:
            stored_dim = self._get_meta(conn, "embed_dim")
            if stored_dim and int(stored_dim) != dim:
                raise ValueError(f"Embedding dimension changed ({stored_dim} -> {dim}); "
                                 "clear the index and re-index")
            if not stored_dim:
                self._set_meta(conn, "embed_dim", str(dim))
            if self.use_vec:
                self._ensure_vec_table(conn, dim)
            else:
                self._set_meta(conn, "vec_dirty", "1")
            for content_hash, arr in rows:
                blob = arr.tobytes()
                cur = conn.execute(
                    "INSERT INTO embeddings(repo, content_hash, vector) VALUES (?,?,?) "
                    "ON CONFLICT(content_hash) DO NOTHING", (repo, content_hash, blob))
                if self.use_vec and cur.rowcount:
                    conn.execute("INSERT INTO vec_embeddings(rowid, embedding, repo) VALUES (?,?,?)",
                                 (cur.lastrowid, blob, repo))

    def prune_embeddings(self):
        """Drop vectors no chunk refers to any more."""
        with self._tx() as conn:
            if self.use_vec and conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='vec_embeddings'").fetchone():
                conn.execute("""DELETE FROM vec_embeddings WHERE rowid IN (
                    SELECT e.id FROM embeddings e
                    WHERE NOT EXISTS (SELECT 1 FROM chunks c WHERE c.content_hash = e.content_hash))""")
            elif not self.use_vec:
                self._set_meta(conn, "vec_dirty", "1")
            conn.execute("""DELETE FROM embeddings WHERE NOT EXISTS (
                SELECT 1 FROM chunks c WHERE c.content_hash = embeddings.content_hash)""")

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def vector_search(self, embedding: list[float], where: str = "1", params: Iterable = (),
                      repo: Optional[str] = None, k: int = 50) -> list[tuple[int, float]]:
        """
        Cosine-similarity search. Returns [(chunk_id, similarity)] best first.
        `where`/`params` is an SQL filter over the `chunks c` alias.
        """
        q = np.asarray(embedding, dtype=np.float32)
        norm = np.linalg.norm(q)
        if q.size == 0 or norm == 0:
            return []
        q = q / norm
        params = tuple(params)
        filtered = where.strip() != "1"
        conn = self._conn()

        if self.use_vec and conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='vec_embeddings'").fetchone():
            qblob = q.tobytes()
            if not filtered:
                # Index KNN over the repo's partition, then map vectors to chunks
                sql = ("SELECT rowid, distance FROM vec_embeddings WHERE embedding MATCH ? AND k = ?"
                       + (" AND repo = ?" if repo else ""))
                hits = conn.execute(sql, (qblob, k, repo) if repo else (qblob, k)).fetchall()
                if not hits:
                    return []
                dist = dict(hits)
                marks = ",".join("?" * len(dist))
                rows = conn.execute(
                    f"SELECT c.id, e.id FROM chunks c JOIN embeddings e ON e.content_hash = c.content_hash "
                    f"WHERE e.id IN ({marks})" + (" AND c.repo = ?" if repo else ""),
                    (*dist.keys(), repo) if repo else tuple(dist.keys())).fetchall()
                out = [(cid, 1.0 - dist[eid]) for cid, eid in rows]
            else:
                # Exact scan over the filtered rows, done in C by sqlite-vec
                repo_sql = " AND c.repo = ?" if repo else ""
                out = conn.execute(
                    f"SELECT c.id, 1.0 - vec_distance_cosine(e.vector, ?) AS sim "
                    f"FROM chunks c JOIN embeddings e ON e.content_hash = c.content_hash "
                    f"WHERE ({where}){repo_sql} ORDER BY sim DESC LIMIT ?",
                    (qblob, *params, *((repo,) if repo else ()), k)).fetchall()
            out.sort(key=lambda t: -t[1])
            return out[:k]

        # numpy fallback: cached, normalised matrix of every stored vector
        emb_ids, matrix = self._matrix()
        if matrix is None:
            return []
        repo_sql = " AND c.repo = ?" if repo else ""
        pairs = conn.execute(
            f"SELECT c.id, e.id FROM chunks c JOIN embeddings e ON e.content_hash = c.content_hash "
            f"WHERE ({where}){repo_sql}", (*params, *((repo,) if repo else ()))).fetchall()
        if not pairs:
            return []
        wanted = np.fromiter((p[1] for p in pairs), dtype=np.int64, count=len(pairs))
        pos = np.minimum(np.searchsorted(emb_ids, wanted), len(emb_ids) - 1)
        known = emb_ids[pos] == wanted          # vectors written after the cache was built
        pairs = [p for p, ok in zip(pairs, known) if ok]
        if not pairs:
            return []
        scores = matrix[pos[known]] @ q
        top = np.argsort(-scores)[:k]
        return [(pairs[i][0], float(scores[i])) for i in top]

    def _matrix(self) -> tuple[np.ndarray, Optional[np.ndarray]]:
        with self._cache_lock:
            if self._matrix_cache and self._matrix_cache[0] == self._version:
                return self._matrix_cache[1], self._matrix_cache[2]
            version = self._version
            rows = self._conn().execute("SELECT id, vector FROM embeddings ORDER BY id").fetchall()
            if not rows:
                return np.array([], dtype=np.int64), None
            ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
            matrix = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(len(rows), -1)
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            matrix = matrix / np.where(norms == 0, 1.0, norms)
            self._matrix_cache = (version, ids, matrix)
            return ids, matrix

    def keyword_search(self, match: str, where: str = "1", params: Iterable = (),
                       repo: Optional[str] = None, k: int = 50) -> list[tuple[int, float]]:
        """BM25 search. Returns [(chunk_id, bm25)] best first (bm25: lower is better)."""
        repo_sql = " AND c.repo = ?" if repo else ""
        try:
            return self._conn().execute(
                f"SELECT c.id, bm25(chunks_fts, 8.0, 3.0, 2.0, 1.0) AS rank "
                f"FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid "
                f"WHERE chunks_fts MATCH ? AND ({where}){repo_sql} ORDER BY rank LIMIT ?",
                (match, *params, *((repo,) if repo else ()), k)).fetchall()
        except sqlite3.OperationalError as e:
            logger.warning(f"Keyword search failed for {match!r}: {e}")
            return []

    def list_chunks(self, where: str = "1", params: Iterable = (), repo: Optional[str] = None,
                    k: int = 50) -> list[int]:
        repo_sql = " AND c.repo = ?" if repo else ""
        rows = self._conn().execute(
            f"SELECT c.id FROM chunks c WHERE ({where}){repo_sql} "
            f"ORDER BY c.file_path, c.line_start LIMIT ?",
            (*params, *((repo,) if repo else ()), k))
        return [r[0] for r in rows]

    def get_chunks(self, ids: list[int]) -> dict[int, dict[str, Any]]:
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = self._conn().execute(
            f"SELECT {', '.join(CHUNK_COLUMNS)} FROM chunks WHERE id IN ({marks})", ids)
        return {r[0]: dict(zip(CHUNK_COLUMNS, r)) for r in rows}

    def vector_scores(self, embedding: list[float], chunk_ids: list[int]) -> dict[int, float]:
        """Cosine similarity of specific chunks to a query vector."""
        if not chunk_ids:
            return {}
        q = np.asarray(embedding, dtype=np.float32)
        norm = np.linalg.norm(q)
        if norm == 0:
            return {}
        q = q / norm
        marks = ",".join("?" * len(chunk_ids))
        rows = self._conn().execute(
            f"SELECT c.id, e.vector FROM chunks c JOIN embeddings e ON e.content_hash = c.content_hash "
            f"WHERE c.id IN ({marks})", chunk_ids).fetchall()
        out = {}
        for cid, blob in rows:
            v = np.frombuffer(blob, dtype=np.float32)
            n = np.linalg.norm(v)
            if n and v.size == q.size:
                out[cid] = float(v @ q / n)
        return out

    def count(self, repo: Optional[str] = None) -> int:
        if repo:
            return self._conn().execute("SELECT COUNT(*) FROM chunks WHERE repo = ?", (repo,)).fetchone()[0]
        return self._conn().execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    def stats(self, repo: Optional[str] = None) -> dict[str, Any]:
        conn = self._conn()
        repo_sql, args = ("WHERE c.repo = ?", (repo,)) if repo else ("", ())
        chunks, embedded = conn.execute(
            f"SELECT COUNT(*), COUNT(e.id) FROM chunks c "
            f"LEFT JOIN embeddings e ON e.content_hash = c.content_hash {repo_sql}", args).fetchone()
        files = conn.execute(
            "SELECT COUNT(*) FROM files" + (" WHERE repo = ?" if repo else ""), args).fetchone()[0]
        return {
            "chunks": chunks,
            "embedded_chunks": embedded,
            "files": files,
            "vector_backend": "sqlite-vec" if self.use_vec else "numpy",
            "db_file": self.db_file,
            "db_bytes": os.path.getsize(self.db_file) if os.path.exists(self.db_file) else 0,
        }


# Back-compat name
LocalVectorDB = CodeLensDB

_db_instance: Optional[CodeLensDB] = None
_db_lock = threading.Lock()


def get_db() -> CodeLensDB:
    global _db_instance
    with _db_lock:
        if _db_instance is None:
            _db_instance = CodeLensDB()
        return _db_instance


def set_db(db: Optional[CodeLensDB]):
    """Swap the process-wide DB (tests)."""
    global _db_instance
    with _db_lock:
        _db_instance = db
