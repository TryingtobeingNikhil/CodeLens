#!/bin/bash
# ============================================================
# CodeLens — One-Command Setup
# Works on macOS (Intel + Apple Silicon) & Linux (ARM/x86)
# ============================================================
set -e

PYTHON=""
OLLAMA_BIN=""

# ---------- Colours ----------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✅ $*${NC}"; }
warn() { echo -e "${YELLOW}⚠️  $*${NC}"; }
fail() { echo -e "${RED}❌ $*${NC}"; exit 1; }

echo ""
echo "  ██████╗ ██████╗ ██████╗ ███████╗██╗     ███████╗███╗   ██╗███████╗"
echo "  ██╔════╝██╔═══██╗██╔══██╗██╔════╝██║     ██╔════╝████╗  ██║██╔════╝"
echo "  ██║     ██║   ██║██║  ██║█████╗  ██║     █████╗  ██╔██╗ ██║███████╗"
echo "  ██║     ██║   ██║██║  ██║██╔══╝  ██║     ██╔══╝  ██║╚██╗██║╚════██║"
echo "  ╚██████╗╚██████╔╝██████╔╝███████╗███████╗███████╗██║ ╚████║███████║"
echo "   ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝╚══════╝╚══════╝╚═╝  ╚═══╝╚══════╝"
echo ""
echo "  Offline Semantic Codebase Search • Powered by Actian VectorAI DB"
echo ""

# ────────────────────────────────────────────────────────────
# 1. Detect Python 3.10+
# ────────────────────────────────────────────────────────────
echo "▶ Detecting Python 3.10+..."
for py in python3.12 python3.11 python3.10 python3; do
    if command -v "$py" &>/dev/null; then
        VER=$("$py" -c "import sys; print(sys.version_info >= (3, 10))" 2>/dev/null)
        if [ "$VER" = "True" ]; then
            PYTHON="$py"
            ok "Found: $py ($($py --version))"
            break
        fi
    fi
done

# Fallback: check /usr/local/bin explicitly (common on macOS)
if [ -z "$PYTHON" ]; then
    for py in /usr/local/bin/python3.11 /usr/local/bin/python3.12 /usr/local/bin/python3.10; do
        if [ -x "$py" ]; then
            PYTHON="$py"; ok "Found: $py ($($py --version))"; break
        fi
    done
fi

[ -z "$PYTHON" ] && fail "Python 3.10+ not found. Install from https://python.org"

# ────────────────────────────────────────────────────────────
# 2. Install / start Ollama
# ────────────────────────────────────────────────────────────
echo "▶ Checking Ollama..."
OLLAMA_CANDIDATES=(
    "ollama"
    "/Applications/Ollama.app/Contents/Resources/ollama"
    "/usr/local/bin/ollama"
    "$HOME/.local/bin/ollama"
)
for b in "${OLLAMA_CANDIDATES[@]}"; do
    if command -v "$b" &>/dev/null || [ -x "$b" ]; then
        OLLAMA_BIN="$b"; ok "Ollama found: $b"; break
    fi
done

if [ -z "$OLLAMA_BIN" ]; then
    warn "Ollama not found — installing..."
    if [[ "$OSTYPE" == "darwin"* ]]; then
        curl -fsSL https://ollama.com/install.sh | sh || fail "Ollama install failed"
        OLLAMA_BIN="/Applications/Ollama.app/Contents/Resources/ollama"
    else
        curl -fsSL https://ollama.com/install.sh | sh || fail "Ollama install failed"
        OLLAMA_BIN="ollama"
    fi
fi

# Add Ollama to PATH for this session
OLLAMA_DIR=$(dirname "$OLLAMA_BIN")
export PATH="$OLLAMA_DIR:$PATH"

# Start Ollama serve if not already running
if ! curl -s http://localhost:11434/api/tags >/dev/null 2>&1; then
    echo "▶ Starting Ollama service..."
    "$OLLAMA_BIN" serve >/tmp/ollama-codelens.log 2>&1 &
    sleep 4
fi

if curl -s http://localhost:11434/api/tags >/dev/null 2>&1; then
    ok "Ollama service running"
else
    fail "Ollama failed to start. Check /tmp/ollama-codelens.log"
fi

# ────────────────────────────────────────────────────────────
# 3. Pull embedding model
# ────────────────────────────────────────────────────────────
echo "▶ Pulling nomic-embed-text (~274 MB, one-time)..."
"$OLLAMA_BIN" pull nomic-embed-text
ok "nomic-embed-text ready"

# ────────────────────────────────────────────────────────────
# 4. Python dependencies
# ────────────────────────────────────────────────────────────
echo "▶ Installing Python dependencies..."
"$PYTHON" -m pip install --quiet --upgrade pip
"$PYTHON" -m pip install --quiet \
    "fastapi>=0.110.0" \
    "uvicorn[standard]>=0.29.0" \
    "pydantic>=2.6.4" \
    "ollama>=0.5.0" \
    "tree-sitter>=0.22.0" \
    tree-sitter-python \
    tree-sitter-typescript \
    tree-sitter-javascript \
    tree-sitter-go \
    tree-sitter-rust \
    tree-sitter-java \
    "watchdog>=4.0.0" \
    "rich>=13.7.1" \
    "numpy>=1.26.0" \
    "python-dotenv>=1.0.1"
ok "Python dependencies installed"

# ────────────────────────────────────────────────────────────
# 5. VectorAI DB directory
# ────────────────────────────────────────────────────────────
echo "▶ Initialising local VectorAI DB..."
mkdir -p ./.vectorai_db
ok "VectorAI DB directory ready at ./.vectorai_db"

# ────────────────────────────────────────────────────────────
# 6. VS Code extension (optional — skip if npm missing)
# ────────────────────────────────────────────────────────────
if command -v npm &>/dev/null; then
    echo "▶ Building VS Code extension..."
    npm install --silent
    npm run build 2>/dev/null || npx tsc -p ./ 2>/dev/null || warn "Extension build failed — install TypeScript with: npm install -g typescript"
    ok "VS Code extension built"
else
    warn "npm not found — skipping extension build (install Node.js to enable)"
fi

# ────────────────────────────────────────────────────────────
# Summary
# ────────────────────────────────────────────────────────────
echo ""
echo "  ════════════════════════════════════════════════════════"
ok "  CodeLens is ready!"
echo "  ════════════════════════════════════════════════════════"
echo ""
echo "  Start the backend:"
echo "    $PYTHON -m uvicorn backend.main:app --host 127.0.0.1 --port 8000"
echo ""
echo "  Index a repo:"
echo "    curl -X POST http://localhost:8000/index \\"
echo "         -H 'Content-Type: application/json' \\"
echo "         -d '{\"repo_path\": \"/path/to/your/repo\"}'"
echo ""
echo "  Query it:"
echo "    curl -X POST http://localhost:8000/query \\"
echo "         -H 'Content-Type: application/json' \\"
echo "         -d '{\"query\": \"where does authentication happen?\", \"top_k\": 5}'"
echo ""
