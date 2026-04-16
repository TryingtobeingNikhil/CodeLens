export interface IndexRequest {
    workspace_path: string;
    include_patterns?: string[];
    exclude_patterns?: string[];
}

export interface QueryRequest {
    query: string;
    top_k?: number;
}

export interface ChunkResult {
    id: string;
    file_path: string;
    start_line: number;
    end_line: number;
    content: string;
    score: number;
}

export interface IndexStatus {
    status: 'idle' | 'indexing' | 'completed' | 'error';
    total_files: number;
    indexed_files: number;
    error?: string;
}
