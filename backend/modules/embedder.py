"""
Embedding model wrapper.

Uses a small, free, local sentence-transformers model so the project
needs no embedding-provider API key or billing at all. Swap
EMBED_MODEL_NAME for a bigger model (e.g. "thenlper/gte-large", the one
used in the paper) if you have the GPU/RAM for it.
"""

from sentence_transformers import SentenceTransformer

EMBED_MODEL_NAME = "all-MiniLM-L6-v2"

_model = None


def get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(EMBED_MODEL_NAME)
    return _model


def embed_texts(texts: list[str]) -> list[list[float]]:
    model = get_model()
    embeddings = model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
    return embeddings.tolist()


def embed_query(text: str) -> list[float]:
    return embed_texts([text])[0]
