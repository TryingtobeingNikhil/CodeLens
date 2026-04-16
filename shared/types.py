from pydantic import BaseModel
from typing import List, Optional

class IndexRequest(BaseModel):
    workspace_path: str
    include_patterns: Optional[List[str]] = None
    exclude_patterns: Optional[List[str]] = None

class QueryRequest(BaseModel):
    query: str
    top_k: Optional[int] = 10

class ChunkResult(BaseModel):
    id: str
    file_path: str
    start_line: int
    end_line: int
    content: str
    score: float

class IndexStatus(BaseModel):
    status: str
    total_files: int
    indexed_files: int
    error: Optional[str] = None
