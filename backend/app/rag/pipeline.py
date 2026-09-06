"""
RAG Pipeline - Complete Retrieval-Augmented Generation using Gemini API.
No direct LLM answer without retrieval. All responses are grounded in documents.
"""
import logging
import time
from typing import List, Dict, Any, Optional, AsyncGenerator

import google.generativeai as genai

from app.core.config import settings
from app.rag.embeddings import embed_query
from app.rag.vector_store import search_similar

logger = logging.getLogger(__name__)

# Configure Gemini
genai.configure(api_key=settings.GEMINI_API_KEY)


SYSTEM_PROMPT = """You are ResearchSphere AI, an expert research assistant. 
You MUST answer based ONLY on the provided document context. 
If the context doesn't contain enough information to answer the question, say so clearly.
Always cite your sources using [Source N] notation.
Format your response in clear markdown with proper headers, bullet points, and code blocks where relevant.
Be comprehensive, accurate, and professional."""


def build_context_prompt(question: str, chunks: List[Dict[str, Any]]) -> str:
    """Build the RAG prompt with retrieved context chunks."""
    context_parts = []
    for i, chunk in enumerate(chunks, 1):
        context_parts.append(
            f"[Source {i}] (Document ID: {chunk['document_id']}, Score: {chunk['score']:.3f})\n"
            f"{chunk['content']}\n"
        )
    
    context_text = "\n---\n".join(context_parts)
    
    return f"""Based on the following document excerpts, answer the question below.

CONTEXT:
{context_text}

QUESTION: {question}

INSTRUCTIONS:
- Answer based ONLY on the provided context
- Use [Source N] citations inline where you reference information
- If the context is insufficient, clearly state what information is missing
- Be comprehensive and well-structured"""


async def stream_rag_response(
    question: str,
    workspace_id: str,
    document_ids: Optional[List[str]] = None,
    top_k: int = 5,
) -> AsyncGenerator[str, None]:
    """
    Full RAG pipeline with streaming:
    1. Embed the question
    2. Search Qdrant for relevant chunks
    3. Build grounded prompt
    4. Stream Gemini response
    
    Yields tokens as they arrive.
    """
    start_time = time.time()
    
    # Step 1: Embed question
    logger.info(f"RAG: Embedding question for workspace {workspace_id}")
    query_embedding = embed_query(question)
    
    # Step 2: Retrieve relevant chunks from Qdrant
    logger.info(f"RAG: Searching Qdrant (top_k={top_k})")
    retrieved_chunks = search_similar(
        query_embedding=query_embedding,
        workspace_id=workspace_id,
        top_k=top_k,
        document_ids=document_ids,
    )
    
    if not retrieved_chunks:
        yield "No relevant documents found in this workspace. Please upload and index documents first."
        return
    
    # Step 3: Build RAG prompt
    prompt = build_context_prompt(question, retrieved_chunks)
    
    # Step 4: Stream from Gemini
    model = genai.GenerativeModel(
        model_name="gemini-1.5-flash",
        system_instruction=SYSTEM_PROMPT,
    )
    
    logger.info("RAG: Streaming Gemini response")
    response = model.generate_content(prompt, stream=True)
    
    full_text = ""
    for chunk in response:
        if chunk.text:
            full_text += chunk.text
            yield chunk.text
    
    elapsed = int((time.time() - start_time) * 1000)
    logger.info(f"RAG: Complete in {elapsed}ms, retrieved {len(retrieved_chunks)} chunks")
    
    # Yield sources metadata as a special marker
    import json
    sources_data = json.dumps({
        "__sources__": retrieved_chunks,
        "__response_time_ms__": elapsed,
    })
    yield f"\n\n__SOURCES_JSON__{sources_data}__END_SOURCES__"


async def get_rag_response(
    question: str,
    workspace_id: str,
    document_ids: Optional[List[str]] = None,
    top_k: int = 5,
) -> Dict[str, Any]:
    """
    Non-streaming RAG pipeline. Returns complete answer with sources.
    """
    start_time = time.time()
    
    # Embed question
    query_embedding = embed_query(question)
    
    # Retrieve chunks
    retrieved_chunks = search_similar(
        query_embedding=query_embedding,
        workspace_id=workspace_id,
        top_k=top_k,
        document_ids=document_ids,
    )
    
    if not retrieved_chunks:
        return {
            "answer": "No relevant documents found. Please upload and index documents first.",
            "sources": [],
            "response_time_ms": 0,
        }
    
    # Build prompt
    prompt = build_context_prompt(question, retrieved_chunks)
    
    # Call Gemini
    model = genai.GenerativeModel(
        model_name="gemini-1.5-flash",
        system_instruction=SYSTEM_PROMPT,
    )
    
    response = model.generate_content(prompt)
    answer = response.text
    
    elapsed = int((time.time() - start_time) * 1000)
    
    return {
        "answer": answer,
        "sources": retrieved_chunks,
        "response_time_ms": elapsed,
    }
