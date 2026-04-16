#!/bin/bash
set -e

echo "Starting CodeLens offline setup..."

# 1. Install Ollama if not present
if ! command -v ollama &> /dev/null; then
    echo "Ollama could not be found. Please install from https://ollama.com/download"
    exit 1
fi

# 2. Start Ollama in background if not running
if ! curl -s http://localhost:11434/api/tags > /dev/null; then
    echo "Starting Ollama service..."
    ollama serve &
    sleep 5
fi

# 3. Pull the embedding model
echo "Pulling nomic-embed-text model..."
ollama pull nomic-embed-text

# 4. Install Python dependencies
echo "Installing Python dependencies for backend..."
if ! command -v pip3 &> /dev/null; then
    echo "pip3 not found. Please install Python 3."
    exit 1
fi
pip3 install -r <(
cat <<EOF
fastapi>=0.110.0
uvicorn[standard]>=0.29.0
pydantic>=2.6.4
ollama>=0.1.7
tree-sitter>=0.21.0
tree-sitter-python>=0.21.0
tree-sitter-typescript>=0.21.0
tree-sitter-javascript>=0.21.0
tree-sitter-go>=0.21.0
tree-sitter-rust>=0.21.0
tree-sitter-java>=0.21.0
actian-vectorai-db-beta>=0.1.0
python-dotenv>=1.0.1
EOF
)

# 5. Initialize Actian VectorAI DB directory
echo "Initializing local embedded Actian VectorAI DB instance..."
mkdir -p ./.vectorai_db
export VECTORAI_DB_PATH="./.vectorai_db"

# 6. Build the VS Code extension
echo "Building the VS Code extension..."
npm install
npm run build

echo "Setup complete! All dependencies installed."
echo "To run the backend: uvicorn backend.main:app --host 127.0.0.1 --port 8000"
