"""
Vector store wrapper around ChromaDB (local, file-based, free — no
hosted vector DB billing needed). Persists to backend/chroma_db/.
"""

import os
import re
import uuid
import chromadb

from modules.embedder import embed_texts, embed_query

CHROMA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "chroma_db")

_client = None
_collection = None
_SEARCH_STOPWORDS = {
    "a", "about", "an", "and", "are", "as", "at", "be", "does", "for",
    "how", "in", "is", "it", "of", "on", "or", "the", "this", "to", "what",
    "which", "with",
}


def _normalise_search_text(value: str) -> str:
    """Normalise PDF spacing and common Roman-numeral unit headings."""
    roman_units = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5"}
    value = re.sub(
        r"\bU\s*N\s*I\s*T\s+(I{1,3}|IV|V)\b",
        lambda match: f"unit {roman_units[match.group(1).lower()]}",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"([A-Za-z])\s+([0-9])", r"\1\2", value)
    value = re.sub(r"\bunit\s+(i{1,3}|iv|v)\b", lambda match: f"unit {roman_units[match.group(1).lower()]}", value, flags=re.IGNORECASE)
    value = re.sub(r"\bunit\s+([1-5])\b", r"unit\1", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).casefold()


def _lexical_score(query: str, text: str) -> int:
    """Generic term-overlap score: PRESENCE (not raw frequency) of each
    non-stopword query term in the text, capped at 1 per term. This is
    only ever used as a light tiebreaker (see retrieve()) -- a long,
    generic chunk that happens to repeat common words many times must
    not be able to out-rank a chunk that is genuinely, semantically
    on-topic just by word-frequency coincidence."""
    query_terms = [
        term for term in re.findall(r"[a-z0-9]+", _normalise_search_text(query))
        if len(term) > 1 and term not in _SEARCH_STOPWORDS
    ]
    normalised_text = _normalise_search_text(text)
    return sum(
        1 for term in query_terms
        if re.search(rf"\b{re.escape(term)}\b", normalised_text)
    )


def _boost_score(query: str, text: str) -> int:
    """Deliberate, high-confidence boosts for syllabus-style navigation
    queries (unit numbers, learning outcomes, topics) where exact wording
    matters more than semantic similarity. These are intentionally large
    so they CAN override pure semantic ranking -- unlike _lexical_score(),
    which is generic overlap and must stay a tiebreaker only."""
    normalised_text = _normalise_search_text(text)
    query_text = _normalise_search_text(query)
    query_terms = [
        term for term in re.findall(r"[a-z0-9]+", query_text)
        if len(term) > 1 and term not in _SEARCH_STOPWORDS
    ]
    score = 0
    unit_terms = [term for term in query_terms if re.fullmatch(r"unit[1-5]", term)]
    score += sum(10 for term in unit_terms if re.search(rf"\b{term}\b", normalised_text))
    if "outcomes" in query_text and "learning outcomes" in normalised_text:
        score += 15
    if "topics" in query_text and any(term in normalised_text for term in ("topics", "introduction", "tools")):
        score += 5
    return score


def _requested_unit(query: str) -> str | None:
    match = re.search(r"\bunit\s*(1|2|3|4|5)\b", _normalise_search_text(query))
    return f"unit{match.group(1)}" if match else None


def get_collection():
    global _client, _collection
    if _collection is None:
        _client = chromadb.PersistentClient(path=CHROMA_DIR)
        _collection = _client.get_or_create_collection(name="pier_qa_chunks")
    return _collection


def add_document_chunks(doc_id: str, filename: str, chunks: list[dict]) -> int:
    """Embed and store chunks for a document. Returns number of chunks stored."""
    if not chunks:
        return 0

    collection = get_collection()
    texts = [chunk["text"] for chunk in chunks]
    embeddings = embed_texts(texts)
    ids = [f"{doc_id}_{i}_{uuid.uuid4().hex[:6]}" for i in range(len(chunks))]
    metadatas = [
        {
            "doc_id": doc_id,
            "filename": filename,
            "chunk_index": i,
            "page_start": chunk.get("page_start", 0),
            "page_end": chunk.get("page_end", 0),
        }
        for i, chunk in enumerate(chunks)
    ]

    collection.add(
        ids=ids,
        embeddings=embeddings,
        documents=texts,
        metadatas=metadatas,
    )
    return len(chunks)


def retrieve(query: str, top_k: int = 5, doc_id: str | None = None) -> list[dict]:
    """Retrieve and rerank chunks using semantic similarity plus exact terms."""
    collection = get_collection()
    query_embedding = embed_query(query)

    where_filter = {"doc_id": doc_id} if doc_id else None

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=max(top_k * 4, 20),
        where=where_filter,
    )

    hits_by_id = {}
    if results["ids"] and results["ids"][0]:
        for i in range(len(results["ids"][0])):
            text = results["documents"][0][i]
            hit = {
                "id": results["ids"][0][i],
                "text": text,
                "metadata": results["metadatas"][0][i],
                "distance": results["distances"][0][i] if results.get("distances") else None,
                "lexical_score": _lexical_score(query, text),
                "boost_score": _boost_score(query, text),
            }
            hits_by_id[hit["id"]] = hit

    # Exact terms may be buried below the semantic top-k, especially for short
    # queries such as "Unit 1". Search the local metadata/text index as a
    # deterministic recall path and merge those chunks into the candidates.
    # IMPORTANT: pass the same where_filter here so that when a doc_id is
    # supplied we only scan that document's chunks — not the entire collection.
    indexed = collection.get(where=where_filter, include=["documents", "metadatas"])
    indexed_rows = list(zip(indexed.get("documents", []), indexed.get("metadatas", [])))
    requested_unit = _requested_unit(query)
    unit_rows = []
    if requested_unit:
        unit_rows = [
            (text, metadata)
            for text, metadata in indexed_rows
            if re.search(rf"\b{requested_unit}\b", _normalise_search_text(text))
        ]

    for text, metadata in indexed_rows:
        lexical_score = _lexical_score(query, text)
        boost_score = _boost_score(query, text)
        if requested_unit and re.search(rf"\b{requested_unit}\b", _normalise_search_text(text)):
            boost_score += 20
        if lexical_score <= 0 and boost_score <= 0:
            continue
        document_id = metadata.get("doc_id", "")
        chunk_index = metadata.get("chunk_index", "")
        hit_id = f"{document_id}_{chunk_index}"
        if hit_id not in hits_by_id:
            hits_by_id[hit_id] = {
                "id": hit_id,
                "text": text,
                "metadata": metadata,
                "distance": 1.0,
                "lexical_score": lexical_score,
                "boost_score": boost_score,
            }
        else:
            hits_by_id[hit_id]["lexical_score"] = max(hits_by_id[hit_id]["lexical_score"], lexical_score)
            hits_by_id[hit_id]["boost_score"] = max(hits_by_id[hit_id]["boost_score"], boost_score)

    # Unit headings and their learning outcomes often land in separate chunks.
    # Add a small local window around each matching heading so the answer has
    # the unit title, topics, and outcomes together.
    if requested_unit:
        for _, heading_metadata in unit_rows:
            doc_id_for_window = heading_metadata.get("doc_id")
            heading_index = heading_metadata.get("chunk_index")
            for text, metadata in indexed_rows:
                if metadata.get("doc_id") != doc_id_for_window:
                    continue
                if metadata.get("chunk_index", 0) < heading_index:
                    continue
                distance = abs(metadata.get("chunk_index", 0) - heading_index)
                if distance > 4:
                    continue
                boost_score = max(_boost_score(query, text), 40 - distance)
                hit_id = f"{metadata.get('doc_id', '')}_{metadata.get('chunk_index', '')}"
                if hit_id not in hits_by_id:
                    hits_by_id[hit_id] = {
                        "id": hit_id,
                        "text": text,
                        "metadata": metadata,
                        "distance": 1.0,
                        "lexical_score": _lexical_score(query, text),
                        "boost_score": boost_score,
                    }
                else:
                    hits_by_id[hit_id]["boost_score"] = max(hits_by_id[hit_id]["boost_score"], boost_score)

    hits = list(hits_by_id.values())
    # Primary: deliberate navigation boosts (unit/outcomes/topics matches) --
    # these are meant to override semantic ranking. Secondary: actual
    # semantic distance (smaller = more relevant). Tertiary: generic word
    # overlap, as a last-resort tiebreaker only -- it must never be able to
    # rank an irrelevant chunk above a semantically close one, which was the
    # bug (lexical_score used to be the primary key).
    hits.sort(
        key=lambda hit: (hit["boost_score"], -(hit["distance"] or 0), hit["lexical_score"]),
        reverse=True,
    )
    selected = []
    seen_texts = set()
    for hit in hits:
        text_key = _normalise_search_text(hit["text"])
        if text_key in seen_texts:
            continue
        seen_texts.add(text_key)
        selected.append(hit)
        if len(selected) == top_k:
            break
    return selected


def delete_document(doc_id: str) -> dict:
    """Remove all chunks belonging to *doc_id* from the collection.

    Returns {"found": bool, "chunks_removed": int}.
    """
    collection = get_collection()
    existing = collection.get(where={"doc_id": doc_id}, include=[])
    ids = existing.get("ids", [])
    if not ids:
        return {"found": False, "chunks_removed": 0}
    collection.delete(ids=ids)
    return {"found": True, "chunks_removed": len(ids)}


def list_documents() -> list[dict]:
    """Return the distinct documents currently indexed."""
    collection = get_collection()
    all_data = collection.get(include=["metadatas"])
    seen = {}
    for meta in all_data.get("metadatas", []):
        doc_id = meta["doc_id"]
        if doc_id not in seen:
            seen[doc_id] = {"doc_id": doc_id, "filename": meta["filename"], "chunks": 0}
        seen[doc_id]["chunks"] += 1
    return list(seen.values())