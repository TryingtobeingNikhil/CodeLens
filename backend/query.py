import asyncio
import logging
from typing import Dict, Any, List

from ollama import AsyncClient
from backend.db_client import get_db
from backend.config import Settings

logger = logging.getLogger(__name__)
ollama_client = AsyncClient(host=Settings.OLLAMA_HOST)

async def run_query(query: str, top_k: int = 10, explain: bool = False) -> Dict[str, Any]:
    # Formulate strictly with the instruction prefix for retrieval embeddings
    embed_prompt = f"search_query: {query}"
    
    try:
        resp = await ollama_client.embeddings(model=Settings.EMBED_MODEL, prompt=embed_prompt)
        query_tensor = resp['embedding']
    except Exception as e:
        logger.error(f"Failed to embed query: {e}")
        raise
    
    # Execute ANN via embedded VectorAI mechanism
    db = get_db()
    raw_results = db.search(query_tensor, top_k=top_k)
    
    results = []
    for r in raw_results:
        raw_score = r.get("score", 0.0)
        
        # Simple bounded normalization [0-1]. Native metrics might require specific mathematical mapping.
        if raw_score <= 1.0:
            normalized_score = max(0.0, raw_score)
        else:
            normalized_score = 1.0 / (1.0 + raw_score)
            
        r["score"] = normalized_score
        results.append(r)
        
    explanation = None
    if explain and results:
        # Pre-build context explicitly up to top 3 matching chunks
        chunk_snippets = [r.get("chunk_text", "") for r in results[:3]]
        
        # Explicit required formatting mappings for the user prompt
        system_msg = "You are a code navigation assistant. Answer concisely in 2-3 sentences. Reference specific function names and files."
        
        # Re-creating exact variables injected sequentially 
        context_body = ""
        for i, snippet in enumerate(chunk_snippets):
            context_body += f"{snippet}\n"

        user_content = f"Codebase context:\n{context_body}\nQuestion: {query}"
        
        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_content}
        ]
        
        try:
            chat_resp = await ollama_client.chat(model="mistral", messages=messages)
            explanation = chat_resp['message']['content']
        except Exception as e:
            logger.error(f"Mistral chat/explain failed: {e}")
            explanation = f"Error generating explanation: {str(e)}"
        
    return {
        "results": results,
        "explain_text": explanation
    }

# Provide backwards-compatible sync wrapper mapped for Uvicorn generic API logic
def run_query_sync(query: str, top_k: int = 10, explain: bool = False) -> Dict[str, Any]:
    return asyncio.run(run_query(query, top_k, explain))
