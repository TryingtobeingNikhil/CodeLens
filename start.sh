#!/bin/bash
# ─────────────────────────────────────────────────
#  CodeLens — Quick Start (dev / demo mode)
#  Starts Ollama + FastAPI backend in one command
# ─────────────────────────────────────────────────
set -e

PYTHON=""
OLLAMA_BIN=""

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✅ $*${NC}"; }
warn() { echo -e "${YELLOW}⚠️  $*${NC}"; }
fail() { echo -e "${RED}❌ $*${NC}"; exit 1; }

# Detect Python
for py in /usr/local/bin/python3.11 /usr/local/bin/python3.12 python3.11 python3.12 python3; do
    if command -v "$py" &>/dev/null; then PYTHON="$py"; break; fi
done
[ -z "$PYTHON" ] && fail "Python 3.10+ not found"

# Detect Ollama
for b in "/Applications/Ollama.app/Contents/Resources/ollama" ollama /usr/local/bin/ollama; do
    if [ -x "$b" ] || command -v "$b" &>/dev/null; then OLLAMA_BIN="$b"; break; fi
done
[ -z "$OLLAMA_BIN" ] && fail "Ollama not found — run ./setup.sh first"
export PATH="$(dirname "$OLLAMA_BIN"):$PATH"

# Start Ollama if not running
if ! curl -s http://localhost:11434/api/tags >/dev/null 2>&1; then
    warn "Starting Ollama..."
    "$OLLAMA_BIN" serve >/tmp/ollama-codelens.log 2>&1 &
    sleep 3
fi
ok "Ollama running"

# Check nomic-embed-text
if ! "$OLLAMA_BIN" list 2>/dev/null | grep -q "nomic-embed-text"; then
    warn "Pulling nomic-embed-text..."
    "$OLLAMA_BIN" pull nomic-embed-text
fi
ok "nomic-embed-text ready"

# Init DB dir
mkdir -p ./.vectorai_db

# Start FastAPI backend
echo ""
ok "Starting CodeLens backend on http://localhost:8000"
echo "   POST /index  — index a repo"
echo "   POST /query  — semantic search"
echo "   GET  /status — chunk count + watcher status"
echo "   GET  /health — ollama + db health"
echo ""
exec "$PYTHON" -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
