import os

from backend.indexer import Indexer
from backend.query import search
from tests.conftest import write

AUTH = '''import jwt

def refresh_jwt_token(token):
    """Refresh an expiring session token."""
    return jwt.encode(jwt.decode(token))


def unrelated_math(a, b):
    return a * b
'''

DB_CODE = '''class ConnectionPool:
    def acquire(self):
        """Get a database connection, retrying on connection errors."""
        return self.connect()
'''


def names(res):
    return [(r["file_path"], r["symbol_name"]) for r in res["results"]]


def test_index_and_hybrid_search(db, embedder, repo):
    write(repo, "auth/tokens.py", AUTH)
    write(repo, "db/pool.py", DB_CODE)
    result = Indexer(repo, db=db).index()
    assert result["changed"] == 2 and result["embed_error"] is None
    assert db.stats(os.path.realpath(repo))["embedded_chunks"] == result["total_chunks"]

    res = search("refresh session token", repo=os.path.realpath(repo))
    assert names(res)[0] == ("auth/tokens.py", "refresh_jwt_token")
    assert res["results"][0]["match"] == "both"
    assert 0 < res["results"][0]["score"] <= 1

    # Exact symbol name wins
    res = search("unrelatedMath", repo=os.path.realpath(repo))
    assert names(res)[0][1] == "unrelated_math"


def test_reindex_is_incremental_and_updates_moved_lines(db, embedder, repo):
    path = write(repo, "auth/tokens.py", AUTH)
    ix = Indexer(repo, db=db)
    ix.index()
    embedded_before = embedder.texts_embedded

    assert ix.index()["unchanged"] == 1  # nothing to do
    assert embedder.texts_embedded == embedded_before

    with open(path, "w") as f:
        f.write("\n\n\n# moved\n" + AUTH)
    ix.index()
    hit = next(r for r in search("refresh_jwt_token", repo=ix.repo)["results"]
               if r["symbol_name"] == "refresh_jwt_token")
    assert hit["line_start"] == 7  # was 3: line numbers follow the move
    assert embedder.texts_embedded == embedded_before  # moved, not changed: no re-embedding


def test_duplicate_content_in_two_files_is_kept(db, embedder, repo):
    write(repo, "a.py", AUTH)
    write(repo, "b.py", AUTH)
    Indexer(repo, db=db).index()
    files = {r["file_path"] for r in search("refresh_jwt_token", repo=os.path.realpath(repo))["results"]}
    assert files == {"a.py", "b.py"}


def test_deleted_and_gitignored_files(db, embedder, repo):
    write(repo, ".gitignore", "secret/\n")
    write(repo, "secret/keys.py", "def api_key():\n    return 'x'\n")
    gone = write(repo, "old.py", "def legacy_thing():\n    pass\n")
    ix = Indexer(repo, db=db)
    ix.index()
    assert "secret/keys.py" not in db.file_hashes(ix.repo)
    os.remove(gone)
    assert ix.index()["removed"] == 1
    assert not search("legacy_thing", repo=ix.repo)["results"]


def test_ollama_down_keeps_keyword_search_and_heals_later(db, embedder, repo):
    write(repo, "auth/tokens.py", AUTH)
    embedder.fail = True
    ix = Indexer(repo, db=db)
    result = ix.index()
    assert result["embed_error"] and result["total_chunks"] > 0
    res = search("refresh jwt", repo=ix.repo)
    assert res["warnings"] and res["results"][0]["symbol_name"] == "refresh_jwt_token"
    assert res["results"][0]["match"] == "keyword"

    embedder.fail = False
    assert ix.index()["embedded"] == result["total_chunks"]
    assert search("refresh jwt", repo=ix.repo)["results"][0]["match"] == "both"


def test_filters(db, embedder, repo):
    write(repo, "src/auth.py", AUTH)
    write(repo, "tests/test_auth.py", AUTH.replace("refresh_jwt_token", "test_refresh"))
    write(repo, "web/auth.ts", "export function refreshToken(t: string) { return t; }\n")
    ix = Indexer(repo, db=db)
    ix.index()

    def files(q):
        return {r["file_path"] for r in search(q, repo=ix.repo)["results"]}

    assert files("refresh token -in:tests") == {"src/auth.py", "web/auth.ts"}
    assert files("refresh token in:web") == {"web/auth.ts"}
    assert files("refresh token lang:py") == {"src/auth.py", "tests/test_auth.py"}
    kinds = {r["kind"] for r in search("token kind:function", repo=ix.repo)["results"]}
    assert kinds == {"function"}
    # Filter-only query lists matching chunks
    assert files("lang:ts") == {"web/auth.ts"}


def test_repos_are_isolated(db, embedder, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write(str(a), "x.py", AUTH)
    write(str(b), "y.py", DB_CODE)
    Indexer(str(a), db=db).index()
    Indexer(str(b), db=db).index()
    res = search("token connection", repo=os.path.realpath(str(b)))
    assert {r["file_path"] for r in res["results"]} == {"y.py"}


def test_watcher_update_paths(db, embedder, repo):
    ix = Indexer(repo, db=db)
    ix.index()
    write(repo, "new.py", "def brand_new_feature():\n    pass\n")
    ix.update_paths(["new.py"])
    assert search("brand_new_feature", repo=ix.repo)["results"][0]["file_path"] == "new.py"
    os.remove(os.path.join(repo, "new.py"))
    ix.update_paths(["new.py"])
    assert not search("brand_new_feature", repo=ix.repo)["results"]


def test_vectors_written_without_sqlite_vec_are_indexed_later(tmp_path, embedder, repo):
    from backend.db_client import CodeLensDB, set_db
    path = str(tmp_path / "shared")
    plain = CodeLensDB(path, use_vec=False)
    write(repo, "auth.py", AUTH)
    Indexer(repo, db=plain).index()
    plain.close()

    vec = CodeLensDB(path, use_vec=True)
    if not vec.use_vec:
        return
    set_db(vec)
    try:
        hits = search("refresh session token", repo=os.path.realpath(repo))["results"]
        assert hits[0]["symbol_name"] == "refresh_jwt_token" and hits[0]["match"] == "both"
    finally:
        set_db(None)
        vec.close()


def test_sentence_word_matching_a_helper_name_is_not_boosted(db, embedder, repo):
    write(repo, "text.py", "def split_identifier(ident):\n    \"\"\"Break camelCase names into words.\"\"\"\n    return ident\n")
    write(repo, "tests/helpers.py", "def names(res):\n    return [r for r in res]\n")
    ix = Indexer(repo, db=db)
    ix.index()
    assert search("break camelCase names into words", repo=ix.repo)["results"][0]["symbol_name"] == "split_identifier"
    assert search("names", repo=ix.repo)["results"][0]["symbol_name"] == "names"


def test_watcher_start_failure_is_not_fatal(tmp_path, monkeypatch):
    from backend import indexer as indexer_mod

    def boom(self):
        raise OSError("inotify watch limit reached")

    monkeypatch.setattr(indexer_mod.Observer, "start", boom)
    idx = indexer_mod.Indexer(str(tmp_path), db=object(), embedder=object())
    idx.start_watchdog()
    assert idx.observer is None
