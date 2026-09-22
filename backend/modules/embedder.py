"""
Embedding model wrapper.

Uses a small, free, local sentence-transformers model so the project
needs no embedding-provider API key or billing at all. Swap
EMBED_MODEL_NAME for a bigger model (e.g. "thenlper/gte-large", the one
used in the paper) if you have the GPU/RAM for it.
"""

import os
from sentence_transformers import SentenceTransformer

# paraphrase-MiniLM-L3-v2 is ~3x faster than all-MiniLM-L6-v2 on CPU
# with comparable retrieval quality for RAG use cases (~17MB vs ~80MB).
EMBED_MODEL_NAME = os.getenv("EMBED_MODEL_NAME", "paraphrase-MiniLM-L3-v2")

_model = None


def get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(EMBED_MODEL_NAME)
    return _model


def embed_texts(texts: list[str]) -> list[list[float]]:
    model = get_model()
    # batch_size=32 gives a good CPU throughput/memory balance
    embeddings = model.encode(
        texts,
        batch_size=32,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    return embeddings.tolist()


def embed_query(text: str) -> list[float]:
    return embed_texts([text])[0]


def warm_up() -> None:
    """Pre-load the model so the first upload doesn't stall while downloading."""
    get_model()
