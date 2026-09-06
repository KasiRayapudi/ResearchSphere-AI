"""
Qdrant Vector Store Service - Real vector storage and similarity search.
"""

import logging
import uuid
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

_client = None


def get_qdrant_client():
    """Lazy-initialize Qdrant client (singleton)."""
    global _client
    if _client is None:
        from qdrant_client import QdrantClient

        _client = QdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT)
        logger.info(f"Connected to Qdrant at {settings.QDRANT_HOST}:{settings.QDRANT_PORT}")

        # Ensure collection exists
        _ensure_collection(_client)

    return _client


def _ensure_collection(client):
    """Create Qdrant collection if it doesn't exist."""
    from qdrant_client.models import Distance, VectorParams

    collections = [c.name for c in client.get_collections().collections]
    if settings.QDRANT_COLLECTION not in collections:
        client.create_collection(
            collection_name=settings.QDRANT_COLLECTION,
            vectors_config=VectorParams(
                size=settings.EMBEDDING_DIMENSION,
                distance=Distance.COSINE,
            ),
        )
        logger.info(f"Created Qdrant collection: {settings.QDRANT_COLLECTION}")


def upsert_chunks(
    chunks: list[dict[str, Any]],
    embeddings: list[list[float]],
    document_id: str,
    workspace_id: str,
) -> list[str]:
    """
    Store chunks and their embeddings in Qdrant.
    Returns list of Qdrant point IDs.
    """
    from qdrant_client.models import PointStruct

    client = get_qdrant_client()
    points = []
    point_ids = []

    # strict=True: a chunk/embedding length mismatch is a bug, and zip's
    # default would silently drop the tail rather than surface it.
    for chunk, embedding in zip(chunks, embeddings, strict=True):
        point_id = str(uuid.uuid4())
        point_ids.append(point_id)

        points.append(
            PointStruct(
                id=point_id,
                vector=embedding,
                payload={
                    "document_id": document_id,
                    "workspace_id": workspace_id,
                    "chunk_index": chunk["index"],
                    "content": chunk["content"],
                    "chunk_db_id": chunk.get("db_id", ""),
                },
            )
        )

    if points:
        client.upsert(collection_name=settings.QDRANT_COLLECTION, points=points)
        logger.info(f"Stored {len(points)} vectors for document {document_id}")

    return point_ids


def search_similar(
    query_embedding: list[float],
    workspace_id: str,
    top_k: int = 5,
    document_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Search Qdrant for similar chunks within a workspace.
    Optionally filter by specific document IDs.
    """
    from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

    client = get_qdrant_client()

    # Build filter: must match workspace
    must_conditions = [FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id))]

    if document_ids:
        must_conditions.append(FieldCondition(key="document_id", match=MatchAny(any=document_ids)))

    search_filter = Filter(must=must_conditions)

    results = client.search(
        collection_name=settings.QDRANT_COLLECTION,
        query_vector=query_embedding,
        query_filter=search_filter,
        limit=top_k,
        with_payload=True,
    )

    return [
        {
            "chunk_id": str(r.id),
            "document_id": r.payload.get("document_id"),
            "chunk_db_id": r.payload.get("chunk_db_id"),
            "content": r.payload.get("content", ""),
            "score": r.score,
            "chunk_index": r.payload.get("chunk_index", 0),
        }
        for r in results
    ]


def delete_document_vectors(document_id: str) -> None:
    """Remove all vectors belonging to a document."""
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    client = get_qdrant_client()
    client.delete(
        collection_name=settings.QDRANT_COLLECTION,
        points_selector=Filter(
            must=[FieldCondition(key="document_id", match=MatchValue(value=document_id))]
        ),
    )
    logger.info(f"Deleted vectors for document {document_id}")
