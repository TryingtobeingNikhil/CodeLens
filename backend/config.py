import os

class Settings:
    VECTORAI_DB_PATH: str = os.getenv("VECTORAI_DB_PATH", "./.vectorai_db")
    OLLAMA_HOST: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    EMBED_MODEL: str = os.getenv("EMBED_MODEL", "nomic-embed-text")
    CHUNK_SIZE: int = int(os.getenv("CHUNK_SIZE", "512"))
    TOP_K: int = int(os.getenv("TOP_K", "10"))
