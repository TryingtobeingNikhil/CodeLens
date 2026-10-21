import json
import os

from backend import main
from tests.conftest import write


def test_rejects_foreign_hosts_and_browser_origins(client):
    assert client.get("/status").status_code == 200
    assert client.get("/status", headers={"host": "evil.example"}).status_code == 403
    assert client.post("/query", json={"query": "x"},
                       headers={"origin": "https://evil.example"}).status_code == 403
    assert "access-control-allow-origin" not in client.options("/query").headers


def test_index_stream_then_query(client, repo):
    write(repo, "auth.py", "def refresh_jwt_token(t):\n    return t\n")
    with client.stream("POST", "/index", json={"repo_path": repo}) as resp:
        events = [json.loads(line[6:]) for line in resp.iter_lines() if line.startswith("data: ")]
    assert events[0]["type"] == "start"
    assert events[-1]["type"] == "complete" and events[-1]["total_chunks"] == 1
    scan = [e for e in events if e.get("phase") == "scan"]
    assert scan[-1]["done"] == scan[-1]["total"] == 1

    res = client.post("/query", json={"query": "refresh token", "repo_path": repo}).json()
    assert res["results"][0]["symbol_name"] == "refresh_jwt_token"
    assert res["total_indexed"] == 1 and "query_ms" in res

    status = client.get("/status", params={"repo_path": repo}).json()
    assert status["indexed_chunks"] == 1 and status["watching"] is True
    assert os.path.realpath(repo) in status["repos"]


def test_bad_inputs(client):
    assert client.post("/query", json={"query": "  "}).status_code == 400
    assert client.post("/index", json={"repo_path": "/definitely/not/here"}).status_code == 400


def test_repo_registry_survives_a_failed_write(client, repo, tmp_path, monkeypatch):
    main.save_repo(repo)

    def crash(*args, **kwargs):
        raise OSError("disk full")

    with monkeypatch.context() as m:
        m.setattr(main.json, "dump", crash)
        main.save_repo(str(tmp_path))
    assert repo in main.load_repos()
