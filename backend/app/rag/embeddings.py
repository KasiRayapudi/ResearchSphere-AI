"""
Embedding Service using SentenceTransformers (local, no API key needed).
Model: all-MiniLM-L6-v2 (384 dimensions, fast, high quality)
"""

import logging

logger = logging.getLogger(__name__)

_model = None


def get_embedding_model():
    """Lazy-load the embedding model (singleton)."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        from app.core.config import settings

        logger.info(f"Loading embedding model: {settings.EMBEDDING_MODEL}")
        _model = SentenceTransformer(settings.EMBEDDING_MODEL)
        logger.info("Embedding model loaded successfully")
    return _model


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Generate embeddings for a list of texts."""
    if not texts:
        return []
    model = get_embedding_model()
    embeddings = model.encode(texts, batch_size=32, show_progress_bar=False)
    return embeddings.tolist()


def embed_query(query: str) -> list[float]:
    """Generate embedding for a single query string."""
    model = get_embedding_model()
    embedding = model.encode([query], show_progress_bar=False)
    return embedding[0].tolist()
