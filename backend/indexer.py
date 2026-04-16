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
import tree_sitter
import tree_sitter_python
import tree_sitter_typescript
import tree_sitter_javascript
import tree_sitter_go
import tree_sitter_rust
import tree_sitter_java

from backend.db_client import get_db
from backend.config import Settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

LANGUAGES = {
    ".py": tree_sitter.Language(tree_sitter_python.language(), "python"),
    ".ts": tree_sitter.Language(tree_sitter_typescript.language_typescript(), "typescript"),
    ".js": tree_sitter.Language(tree_sitter_javascript.language(), "javascript"),
    ".go": tree_sitter.Language(tree_sitter_go.language(), "go"),
    ".rs": tree_sitter.Language(tree_sitter_rust.language(), "rust"),
    ".java": tree_sitter.Language(tree_sitter_java.language(), "java"),
}

TARGET_NODES = {
    "python": {"function_definition", "class_definition", "decorated_definition"},
    "typescript": {"function_declaration", "method_definition", "class_declaration", "arrow_function"},
    "javascript": {"function_declaration", "method_definition", "class_declaration", "arrow_function"},
    "go": {"function_declaration", "method_declaration", "type_declaration"},
    "rust": {"function_item", "impl_item", "struct_item"},
    "java": {"method_declaration", "class_declaration", "constructor_declaration"}
}

IGNORE_DIRS = {"node_modules", ".git", "vendor", "dist", "__pycache__", "build"}

def md5_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()

def extract_symbol_name(node: tree_sitter.Node, content: str) -> str:
    # Attempt to extract identifier name from child nodes
    for child in node.children:
        if child.type in {"identifier", "name", "type_identifier"}:
            return content[child.start_byte:child.end_byte]
            
    # For arrow_functions assigned to a variable, we extract from the variable_declarator parent
    if node.type == "arrow_function" and node.parent and node.parent.type == "variable_declarator":
        for child in node.parent.children:
            if child.type == "identifier":
                return content[child.start_byte:child.end_byte]

    return node.type

def sliding_window_fallback(file_path: str, content: str) -> List[Dict[str, Any]]:
    lines = content.splitlines()
    chunks = []
    window = 40
    overlap = 10
    step = window - overlap
    
    if not lines:
        return []
        
    for i in range(0, len(lines), step):
        chunk_lines = lines[i:i + window]
        chunk_text = "\n".join(chunk_lines)
        if not chunk_text.strip():
            continue
            
        chunks.append({
            "symbol_name": f"lines_{i + 1}_{i + len(chunk_lines)}",
            "chunk_text": chunk_text,
            "file_path": file_path,
            "line_start": i + 1,
            "line_end": i + len(chunk_lines),
            "language": "unknown",
            "content_hash": md5_hash(chunk_text)
        })
    return chunks

def chunk_file(file_path: str, repo_root: str) -> List[Dict[str, Any]]:
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        logger.warning(f"Could not read {file_path}: {e}")
        return []

    ext = os.path.splitext(file_path)[1]
    rel_path = os.path.relpath(file_path, repo_root)
    
    if ext not in LANGUAGES:
        return sliding_window_fallback(rel_path, content)
        
    language_def = LANGUAGES[ext]
    parser = tree_sitter.Parser()
    parser.set_language(language_def)
    tree = parser.parse(content.encode("utf-8"))
    
    chunks = []
    visited_nodes = set()
    target_types = TARGET_NODES.get(language_def.name, set())
    
    def walk(node: tree_sitter.Node):
        if id(node) in visited_nodes:
            return
        visited_nodes.add(id(node))
        
        is_target = node.type in target_types
        
        # Enforce arrow functions only if assigned to a variable
        if is_target and node.type == "arrow_function":
            parent = node.parent
            if not (parent and parent.type == "variable_declarator"):
                is_target = False
                
        if is_target:
            symbol_name = extract_symbol_name(node, content)
            chunk_text = content[node.start_byte:node.end_byte]
            
            start_row = node.start_point[0] if isinstance(node.start_point, tuple) else getattr(node.start_point, 'row', 0)
            end_row = node.end_point[0] if isinstance(node.end_point, tuple) else getattr(node.end_point, 'row', 0)
            
            chunks.append({
                "symbol_name": symbol_name,
                "chunk_text": chunk_text,
                "file_path": rel_path,
                "line_start": start_row + 1,
                "line_end": end_row + 1,
                "language": language_def.name,
                "content_hash": md5_hash(chunk_text)
            })
            
        for child in node.children:
            walk(child)
            
    walk(tree.root_node)
    
    # Empty fallback
    if not chunks and content.strip():
        return sliding_window_fallback(rel_path, content)
        
    return chunks

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
                ext = os.path.splitext(file)[1]
                if ext in LANGUAGES or ext in {".cpp", ".h", ".c", ".md", ".txt"}:
                    yield os.path.join(root, file)

    async def embed_and_store(self, chunks: List[Dict[str, Any]]):
        if not chunks:
            return

        # Simple incremental indexing strategy: skip duplicates
        check_hashes = hasattr(self.db, "get_existing_hashes")
        existing_hashes = set(self.db.get_existing_hashes([c["content_hash"] for c in chunks])) if check_hashes else set()
        
        filtered_chunks = [c for c in chunks if c["content_hash"] not in existing_hashes]
        if not filtered_chunks:
            return

        batch_size = 20
        total_embedded = 0
        for i in range(0, len(filtered_chunks), batch_size):
            batch = filtered_chunks[i:i + batch_size]
            
            # Batch embedding pipeline natively running parallel logic inside Ollama SDK constraint
            embeddings = []
            for chunk in batch:
                prompt_text = f"search_document: {chunk['chunk_text']}"
                try:
                    resp = await self.ollama_client.embeddings(model=Settings.EMBED_MODEL, prompt=prompt_text)
                    embeddings.append(resp['embedding'])
                except Exception as e:
                    logger.error(f"Ollama Embed Error: {e}")
                    embeddings.append([0.0] * 768)

            self.db.batch_upsert(batch, embeddings)
            total_embedded += len(batch)
            
    def run_full_index(self):
        files = list(self.walk_repo())
        all_chunks = []
        
        with Progress(
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn()
        ) as progress:
            task_files = progress.add_task("Parsing files", total=len(files))
            
            for file_path in files:
                basename = os.path.basename(file_path)
                progress.update(task_files, description=f"[filename] {basename} [chunks]")
                
                chunks = chunk_file(file_path, self.repo_path)
                all_chunks.extend(chunks)
                progress.advance(task_files)

            task_embed = progress.add_task("Embedding chunks", total=len(all_chunks))
            
            async def run_embedding():
                batch_size = 20
                for i in range(0, len(all_chunks), batch_size):
                    batch = all_chunks[i:i + batch_size]
                    progress.update(task_embed, description=f"Embedding [{len(batch)} chunks]")
                    
                    await self.embed_and_store(batch)
                    progress.advance(task_embed, advance=len(batch))
                    
            asyncio.run(run_embedding())
            logger.info(f"Finished indexing {len(files)} files into {len(all_chunks)} chunks.")

    async def reindex_file_async(self, file_path: str):
        rel_path = os.path.relpath(file_path, self.repo_path)
        chunks = chunk_file(file_path, self.repo_path)
        self.db.delete_by_filepath(rel_path)
        await self.embed_and_store(chunks)
        logger.info(f"Dynamically re-indexed updated file <{rel_path}> ({len(chunks)} chunks)")

    def reindex_file(self, file_path: str):
        asyncio.run(self.reindex_file_async(file_path))

    def start_watchdog(self):
        if self.observer is not None:
            return
        handler = RepoEventHandler(self)
        self.observer = Observer()
        self.observer.schedule(handler, self.repo_path, recursive=True)
        self.observer.start()
        logger.info(f"Watchdog started for filesystem modifications on {self.repo_path}")

class RepoEventHandler(FileSystemEventHandler):
    def __init__(self, indexer: Indexer):
        self.indexer = indexer

    def on_modified(self, event):
        if event.is_directory:
            return
            
        ext = os.path.splitext(event.src_path)[1]
        if ext in LANGUAGES:
            # Must avoid nested ignored paths natively 
            if not any(ign in event.src_path.split(os.sep) for ign in IGNORE_DIRS):
                self.indexer.reindex_file(event.src_path)
