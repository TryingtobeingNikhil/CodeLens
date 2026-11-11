"""Suggestions built from the repo, explain without search, and C/CUDA indexing."""
import pytest

from backend import main, query
from backend.indexer import Indexer
from backend.suggest import suggest
from backend.tree_sitter_parser import extract_chunks
from tests.conftest import write

CUDA = """\
#include <cuda_runtime.h>

__global__ void tiledMatmulKernel(const float* A, const float* B, float* C, int N) {
    __shared__ float tile[16][16];
    int row = blockIdx.y * 16 + threadIdx.y;
}

__device__ float warpReduceSum(float v) { return v; }

int main() {
    tiledMatmulKernel<<<1, 256>>>(nullptr, nullptr, nullptr, 0);
    return 0;
}
"""


def test_cuda_and_c_files_are_chunked_by_function():
    chunks = extract_chunks("day05/matmul.cu", CUDA)
    assert [c["symbol_name"] for c in chunks] == ["tiledMatmulKernel", "warpReduceSum", "main"]
    assert {c["language"] for c in chunks} == {"cuda"}
    assert chunks[0]["line_start"] == 3
    c_chunks = extract_chunks("util.c", "int add(int a, int b) { return a + b; }\n")
    assert c_chunks[0]["symbol_name"] == "add" and c_chunks[0]["language"] == "cpp"


def test_lang_cuda_filter():
    assert query.parse_query("matmul lang:cu").languages == ["cuda"]
    assert query.parse_query("matmul lang:cuda").languages == ["cuda"]


def test_suggestions_come_from_the_repo(db, embedder, repo):
    write(repo, "day05/matmul.cu", CUDA)
    write(repo, "day12/reduce.cu", "__global__ void parallelReductionKernel(float* x) {}\n"
                                   "__global__ void warpShuffleReduction(float* x) {}\n")
    write(repo, "day30/softmax.cu", "__global__ void onlineSoftmaxKernel(float* x) {}\n")
    Indexer(repo, db=db, embedder=embedder).index()

    out = suggest(Indexer(repo).repo, db)
    joined = " ".join(out["suggestions"])
    assert 1 <= len(out["suggestions"]) <= 3
    assert "authentication" not in joined
    assert any(w in joined for w in ("matmul", "reduction", "softmax", "kernel", "warp"))
    assert out["languages"] == ["cuda"]


def test_suggestions_for_unindexed_repo_are_empty(db):
    assert suggest("/not/indexed", db)["suggestions"] == []
    assert suggest(None, db)["suggestions"] == []


def test_explain_snippet_does_not_search(client, monkeypatch):
    seen = {}

    def fake_chat(messages):
        seen["prompt"] = messages[-1]["content"]
        return "It adds two numbers."

    monkeypatch.setattr(query, "_chat", fake_chat)
    monkeypatch.setattr(main, "search", lambda *a, **k: pytest.fail("explain must not search"))
    res = client.post("/explain", json={"code": "def add(a, b):\n    return a + b",
                                         "file_path": "m.py", "language": "python"})
    assert res.status_code == 200
    assert res.json()["explain_text"] == "It adds two numbers."
    assert "def add" in seen["prompt"] and "m.py" in seen["prompt"]


def test_explain_reports_missing_model(client, monkeypatch):
    def down(messages):
        raise query.ExplainError("The model mistral is not pulled. Run: ollama pull mistral")

    monkeypatch.setattr(query, "_chat", down)
    res = client.post("/explain", json={"code": "x = 1"})
    assert res.status_code == 503 and "ollama pull" in res.json()["detail"]
    assert client.post("/explain", json={}).status_code == 400


def test_explain_query_uses_search_results(client, repo, monkeypatch):
    write(repo, "auth.py", "def refresh_jwt_token(t):\n    return t\n")
    with client.stream("POST", "/index", json={"repo_path": repo}) as resp:
        list(resp.iter_lines())
    monkeypatch.setattr(query, "_chat", lambda m: "refresh_jwt_token renews tokens")
    res = client.post("/explain", json={"query": "refresh token", "repo_path": repo})
    assert res.status_code == 200 and "refresh_jwt_token" in res.json()["explain_text"]


def test_explain_query_sends_question_without_filters(client, repo, monkeypatch):
    write(repo, "auth.py", "def refresh_jwt_token(t):\n    return t\n")
    with client.stream("POST", "/index", json={"repo_path": repo}) as resp:
        list(resp.iter_lines())
    sent = []
    monkeypatch.setattr(query, "_chat", lambda m: sent.append(m[-1]["content"]) or "ok")
    client.post("/explain", json={"query": "refresh token lang:py -in:tests", "repo_path": repo})
    assert sent and sent[0].endswith("Question: refresh token")


def test_suggestions_endpoint(client, repo):
    write(repo, "kernels/softmax.cu", "__global__ void onlineSoftmaxKernel(float* x) {}\n")
    with client.stream("POST", "/index", json={"repo_path": repo}) as resp:
        list(resp.iter_lines())
    res = client.get("/suggestions", params={"repo_path": repo}).json()
    assert res["suggestions"] and "cuda" in res["languages"]
