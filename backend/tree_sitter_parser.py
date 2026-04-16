import os
import tree_sitter
from pathlib import Path
import tree_sitter_python
import tree_sitter_typescript
import tree_sitter_javascript
import tree_sitter_go
import tree_sitter_rust
import tree_sitter_java

# Safely load CPP if installed
try:
    import tree_sitter_cpp
    has_cpp = True
except ImportError:
    has_cpp = False

LANGUAGES = {
    ".py": tree_sitter.Language(tree_sitter_python.language(), "python"),
    ".ts": tree_sitter.Language(tree_sitter_typescript.language_typescript(), "typescript"),
    ".js": tree_sitter.Language(tree_sitter_javascript.language(), "javascript"),
    ".go": tree_sitter.Language(tree_sitter_go.language(), "go"),
    ".rs": tree_sitter.Language(tree_sitter_rust.language(), "rust"),
    ".java": tree_sitter.Language(tree_sitter_java.language(), "java"),
}
if has_cpp:
    LANGUAGES[".cpp"] = tree_sitter.Language(tree_sitter_cpp.language(), "cpp")

TARGET_NODE_TYPES = {
    "python": ["function_definition", "class_definition"],
    "typescript": ["function_declaration", "method_definition", "class_declaration", "arrow_function", "interface_declaration"],
    "javascript": ["function_declaration", "method_definition", "class_declaration", "arrow_function"],
    "go": ["function_declaration", "method_declaration"],
    "rust": ["function_item", "mod_item", "impl_item"],
    "java": ["method_declaration", "class_declaration"],
    "cpp": ["function_definition", "class_specifier", "struct_specifier"]
}

def extract_chunks(file_path: str, content: str):
    ext = os.path.splitext(file_path)[1]
    if ext not in LANGUAGES:
        return []
        
    language = LANGUAGES[ext]
    parser = tree_sitter.Parser()
    parser.set_language(language)
    
    tree = parser.parse(content.encode("utf-8"))
    chunks = []
    
    def walk(node):
        target_types = TARGET_NODE_TYPES.get(language.name, [])
        
        if node.type in target_types:
            name_node = None
            for child in node.children:
                if child.type in ["identifier", "name", "type_identifier"]:
                    name_node = child
                    break
            
            symbol_name = name_node.text.decode('utf-8') if name_node else node.type
            chunk_text = content[node.start_byte:node.end_byte]
            
            start_row = node.start_point[0] if isinstance(node.start_point, tuple) else getattr(node.start_point, 'row', 0)
            end_row = node.end_point[0] if isinstance(node.end_point, tuple) else getattr(node.end_point, 'row', 0)
            
            chunks.append({
                "file_path": file_path,
                "line_start": start_row + 1,
                "line_end": end_row + 1,
                "symbol_name": symbol_name,
                "language": language.name,
                "chunk_text": chunk_text
            })
            
        for child in node.children:
            walk(child)
            
    walk(tree.root_node)
    
    # Fallback to whole file if no chunks found
    if not chunks and len(content.strip()) > 0:
        chunks.append({
             "file_path": file_path,
             "line_start": 1,
             "line_end": len(content.splitlines()),
             "symbol_name": "unknown_file_content",
             "language": language.name,
             "chunk_text": content[:5000] # Safe truncation fallback
        })
    
    return chunks
