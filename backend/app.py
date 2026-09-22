"""
VerifiableRAG backend — FastAPI server.

Endpoints
---------
GET  /health                  — liveness probe
GET  /supported-types         — list indexable file extensions
GET  /documents               — list all indexed documents
POST /upload                  — upload + index any supported file
DELETE /documents/{doc_id}    — remove a document
POST /ask                     — verified RAG question-answer
POST /search                  — semantic chunk search (no LLM)
POST /summarize               — auto-summarise a document
GET  /export/{doc_id}         — export all chunks as plain text

Run with:  uvicorn app:app --reload --port 8000
"""

import os
import shutil
import uuid
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from modules.doc_processor import process_document, is_supported, SUPPORTED_EXTENSIONS
from modules.vectorstore import (
    add_document_chunks,
    list_documents,
    delete_document,
    retrieve,
    get_collection,
)
from modules.claim_verifier import (
    answer_with_verification,
    generate_answer,
    _format_evidence,
    extract_and_verify_claims,
    compute_hallucination_risk_score,
    _contains_unverified_percentage,
    ABSTENTION_MESSAGE,
    ABSTENTION_FALLBACK_MESSAGE,
)
from modules.llm_client import chat, get_client, _model_name, _pace_request
from modules.embedder import warm_up as _warm_up_embedder

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "2000"))
TOP_K      = int(os.getenv("TOP_K", "3"))
# Cap chunks per document to keep indexing fast even for 800-page textbooks.
# 300 chunks × 2000 chars covers ~600 000 chars = ~400 pages of dense text.
MAX_CHUNKS = int(os.getenv("MAX_CHUNKS", "300"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Pre-load the embedding model so the first upload doesn't stall
    # while the ~80 MB model file downloads from HuggingFace.
    try:
        _warm_up_embedder()
    except Exception:
        pass  # non-fatal — model loads lazily on first use if this fails
    yield


app = FastAPI(title="VerifiableRAG", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request models ────────────────────────────────────────────────────────────

class AskRequest(BaseModel):
    question: str
    doc_id: str | None = None
    doc_ids: list[str] | None = None   # multi-doc query
    top_k: int | None = None


class SearchRequest(BaseModel):
    query: str
    doc_id: str | None = None
    doc_ids: list[str] | None = None
    top_k: int = 8


class SummarizeRequest(BaseModel):
    doc_id: str
    style: str = "concise"   # "concise" | "detailed" | "bullets"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pick_doc_id(doc_id: str | None, doc_ids: list[str] | None) -> str | None:
    """
    Normalise the doc scoping from the two request fields.
    - If doc_ids has exactly one entry, treat it like doc_id.
    - If doc_ids has multiple entries or neither field is set, return None
      (global search across all docs, filtered post-retrieval if needed).
    Returns the single doc_id to pass to vectorstore.retrieve(), or None.
    """
    if doc_id:
        return doc_id
    if doc_ids and len(doc_ids) == 1:
        return doc_ids[0]
    return None  # caller must post-filter for multi-doc


def _retrieve_multi(query: str, doc_ids: list[str] | None, top_k: int) -> list[dict]:
    """
    Retrieve chunks across multiple documents and merge + re-sort results.
    When doc_ids is None/empty, falls back to a global search.
    """
    if not doc_ids or len(doc_ids) == 1:
        return retrieve(query, top_k=top_k, doc_id=(doc_ids[0] if doc_ids else None))

    # Retrieve top_k from each doc, then pick the overall best top_k.
    per_doc_k = max(top_k, 3)
    merged: list[dict] = []
    seen_ids: set[str] = set()
    for did in doc_ids:
        for hit in retrieve(query, top_k=per_doc_k, doc_id=did):
            if hit["id"] not in seen_ids:
                merged.append(hit)
                seen_ids.add(hit["id"])

    # Re-sort by the same composite key vectorstore uses.
    merged.sort(
        key=lambda h: (h["boost_score"], -(h["distance"] or 0), h["lexical_score"]),
        reverse=True,
    )
    return merged[:top_k]


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/supported-types")
def supported_types():
    return {"extensions": sorted(SUPPORTED_EXTENSIONS)}


@app.get("/documents")
def get_documents():
    return {"documents": list_documents()}


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    filename = file.filename or ""
    if not is_supported(filename):
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type. Supported: {supported}",
        )

    doc_id    = uuid.uuid4().hex[:12]
    save_path = os.path.join(UPLOAD_DIR, f"{doc_id}_{filename}")

    with open(save_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        chunks = process_document(save_path, chunk_size=CHUNK_SIZE)
        if not chunks:
            raise ValueError("No text could be extracted from this file.")
        # Cap chunk count so very large documents (800-page textbooks) index in
        # reasonable time. Chunks are taken from the start of the document;
        # increase MAX_CHUNKS in .env if you need deeper coverage.
        if len(chunks) > MAX_CHUNKS:
            chunks = chunks[:MAX_CHUNKS]
        num_chunks = add_document_chunks(doc_id, filename, chunks)
    except ValueError as e:
        _cleanup(save_path)
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        _cleanup(save_path)
        raise HTTPException(status_code=500, detail=f"Failed to process file: {e}")

    return {"doc_id": doc_id, "filename": filename, "chunks_indexed": num_chunks}


def _cleanup(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


@app.delete("/documents/{doc_id}")
def delete_doc(doc_id: str):
    result = delete_document(doc_id)
    if not result["found"]:
        raise HTTPException(status_code=404, detail=f"Document '{doc_id}' not found.")
    removed_files = []
    for fname in os.listdir(UPLOAD_DIR):
        if fname.startswith(doc_id):
            try:
                os.remove(os.path.join(UPLOAD_DIR, fname))
                removed_files.append(fname)
            except OSError:
                pass
    return {
        "doc_id": doc_id,
        "chunks_removed": result["chunks_removed"],
        "files_removed": removed_files,
    }


@app.post("/ask")
def ask_question(req: AskRequest):
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty.")

    # Support multi-doc queries by merging evidence before verification.
    doc_ids = req.doc_ids or ([] if not req.doc_id else [req.doc_id])
    effective_top_k = req.top_k or TOP_K

    try:
        if len(doc_ids) <= 1:
            # Single-doc or global path — use existing verified pipeline.
            result = answer_with_verification(
                question=req.question,
                doc_id=doc_ids[0] if doc_ids else None,
                top_k=effective_top_k,
            )
        else:
            # Multi-doc: retrieve across docs, then run verification on merged evidence.
            evidence = _retrieve_multi(req.question, doc_ids, effective_top_k)
            if not evidence:
                result = {
                    "answer": "I don't have enough evidence in the provided document(s) to answer this reliably.",
                    "abstained": True,
                    "claims": [],
                    "sources": [],
                    "rounds": 0,
                    "hallucination_risk_score": 0,
                }
            else:
                # Use the same verified pipeline but pass pre-fetched evidence.
                from modules.claim_verifier import (
                    generate_answer, extract_and_verify_claims,
                    compute_hallucination_risk_score,
                    ABSTENTION_MESSAGE, MAX_CORRECTION_ROUNDS,
                )
                answer = generate_answer(req.question, evidence)
                if not answer or not answer.strip():
                    answer = "I'm not able to find verified information in the document to answer this question."
                if ABSTENTION_MESSAGE in answer:
                    result = {"answer": answer, "abstained": True, "claims": [], "sources": evidence, "rounds": 0, "hallucination_risk_score": 0}
                else:
                    verdicts = extract_and_verify_claims(answer, evidence)
                    supported = [v for v in verdicts if v.get("verdict", "").upper() == "SUPPORTED"]
                    has_bad = any(
                        v.get("verdict", "").upper() in ("UNSUPPORTED", "CONTRADICTED")
                        for v in verdicts
                    )
                    # Only treat as abstained when there are no supported claims AND
                    # no unsupported/contradicted ones (i.e. everything is ABSTAINED).
                    # ABSTAINED claims must NOT be counted as unsupported.
                    is_abstained = not bool(supported) and not has_bad
                    result = {
                        "answer": answer,
                        "abstained": is_abstained,
                        "claims": verdicts,
                        "sources": evidence,
                        "rounds": 0,
                        "hallucination_risk_score": compute_hallucination_risk_score(verdicts, is_abstained),
                    }
    except Exception as e:
        print(f"[ASK] pipeline error: {type(e).__name__}: {e}")
        result = {
            "answer": "I'm not able to find verified information in the document to answer this question.",
            "abstained": True,
            "claims": [],
            "sources": [],
            "rounds": 0,
            "hallucination_risk_score": 0,
        }

    # Hard safety net: never return an empty answer box under any circumstance.
    if not result.get("answer") or not str(result["answer"]).strip():
        result["answer"] = "I'm not able to find verified information in the document to answer this question."
        result["abstained"] = True
        result["hallucination_risk_score"] = 0
    # DETERMINISTIC PERCENTAGE CHECK — temporarily disabled, was over-triggering.
    # elif not result.get("abstained", False):
    #     sources = result.get("sources", [])
    #     if sources and _contains_unverified_percentage(str(result["answer"]), sources):
    #         result["answer"] = ABSTENTION_FALLBACK_MESSAGE
    #         result["abstained"] = True
    #         result["hallucination_risk_score"] = 0

    return result


@app.post("/search")
def search_chunks(req: SearchRequest):
    """
    Pure semantic search — returns raw matching chunks without invoking the LLM.
    Useful for exploring what's in a document before asking questions.
    """
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty.")

    doc_ids = req.doc_ids or ([] if not req.doc_id else [req.doc_id])
    hits = _retrieve_multi(req.query, doc_ids or None, req.top_k)

    return {
        "query": req.query,
        "results": [
            {
                "rank": i + 1,
                "text": h["text"],
                "filename": h["metadata"].get("filename", ""),
                "doc_id": h["metadata"].get("doc_id", ""),
                "page": h["metadata"].get("page_start"),
                "chunk_index": h["metadata"].get("chunk_index"),
                "score": round(1 - (h["distance"] or 1), 4),
            }
            for i, h in enumerate(hits)
        ],
        "total": len(hits),
    }


@app.post("/summarize")
def summarize_document(req: SummarizeRequest):
    """
    Auto-generate a document summary using a wider evidence sample.
    Styles: 'concise' (2-3 para), 'detailed' (full overview), 'bullets' (key points).
    The LLM is grounded — it can only use the retrieved chunks.
    """
    # Fetch a broad sample — use up to 12 chunks spread across the document.
    evidence = retrieve(req.doc_id, top_k=12, doc_id=req.doc_id)
    # Also grab a few chunks from the start (index 0-3) which often contain
    # title / abstract / introduction that summarizers need.
    collection = get_collection()
    intro_data = collection.get(
        where={"doc_id": req.doc_id},
        include=["documents", "metadatas"],
    )
    intro_rows = sorted(
        zip(intro_data.get("documents", []), intro_data.get("metadatas", [])),
        key=lambda x: x[1].get("chunk_index", 999),
    )[:4]
    intro_chunks = [
        {"text": t, "metadata": m} for t, m in intro_rows
        if not any(h["text"] == t for h in evidence)
    ]
    combined = intro_chunks + evidence

    style_instructions = {
        "concise":  "Write a concise 2-3 paragraph summary covering the main topic, key findings, and conclusions.",
        "detailed": "Write a comprehensive overview covering all major sections, themes, and important details.",
        "bullets":  "List the most important points as clear, concise bullet points (use • for each). Group related points.",
    }
    instruction = style_instructions.get(req.style, style_instructions["concise"])

    context = _format_evidence(combined)
    messages = [
        {
            "role": "system",
            "content": (
                "You are a document summarization assistant. "
                "Use ONLY the provided context passages. "
                "Never invent information not present in the context. "
                f"{instruction}"
            ),
        },
        {
            "role": "user",
            "content": f"Context:\n{context}\n\nProvide a summary of this document:",
        },
    ]

    try:
        summary = chat(messages, temperature=0.3,
                       max_tokens=int(os.getenv("LLM_ANSWER_MAX_TOKENS", "800")))
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"LLM error: {e}")

    return {
        "doc_id": req.doc_id,
        "style": req.style,
        "summary": summary,
        "sources_used": len(combined),
    }


@app.get("/export/{doc_id}", response_class=PlainTextResponse)
def export_document(doc_id: str):
    """
    Export all indexed text chunks for a document as a plain-text file,
    ordered by chunk index. Useful for debugging, offline reading, or
    feeding into external tools.
    """
    collection = get_collection()
    data = collection.get(
        where={"doc_id": doc_id},
        include=["documents", "metadatas"],
    )
    rows = list(zip(data.get("documents", []), data.get("metadatas", [])))
    if not rows:
        raise HTTPException(status_code=404, detail=f"No chunks found for doc_id '{doc_id}'.")

    rows.sort(key=lambda x: x[1].get("chunk_index", 0))
    filename = rows[0][1].get("filename", doc_id)

    lines = [f"# {filename}", f"# Exported from VerifiableRAG — {len(rows)} chunks", ""]
    for text, meta in rows:
        page = meta.get("page_start")
        idx  = meta.get("chunk_index", "?")
        lines.append(f"--- Chunk {idx}" + (f" (page {page})" if page else "") + " ---")
        lines.append(text)
        lines.append("")

    return PlainTextResponse(
        content="\n".join(lines),
        headers={"Content-Disposition": f'attachment; filename="{filename}.txt"'},
    )


@app.post("/ask/stream")
def ask_stream(req: AskRequest):
    """
    Server-Sent Events version of /ask.

    Event types sent to the client:
      data: {"type":"token",  "text":"..."}          — answer token(s)
      data: {"type":"verify", "claims":[...], "sources":[...], "abstained":bool, "rounds":int}
      data: {"type":"error",  "message":"..."}
      data: {"type":"done"}
    """
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty.")

    doc_ids = req.doc_ids or ([] if not req.doc_id else [req.doc_id])
    effective_top_k = req.top_k or TOP_K

    import json as _json

    def event(obj: dict) -> str:
        return f"data: {_json.dumps(obj)}\n\n"

    def generate():
        try:
            # ── 1. Retrieve evidence ──────────────────────────────────────────
            evidence = _retrieve_multi(req.question, doc_ids or None, effective_top_k)
            if not evidence:
                yield event({"type": "error", "message": ABSTENTION_MESSAGE})
                yield event({"type": "done"})
                return

            # ── 2. Stream the answer token by token ──────────────────────────
            from modules.claim_verifier import _format_evidence, _is_summary_question
            import os

            # Widen evidence for summary questions
            if _is_summary_question(req.question) and len(doc_ids) <= 1:
                from modules.vectorstore import retrieve as _retrieve
                extra = _retrieve(
                    req.question,
                    top_k=effective_top_k * 3,
                    doc_id=doc_ids[0] if doc_ids else None,
                )
                seen = {e["id"] for e in evidence}
                evidence = evidence + [h for h in extra if h["id"] not in seen]

            context = _format_evidence(evidence)
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are a helpful question-answering assistant. "
                        "Use the provided context passages to answer the question. "
                        "Write a complete, well-structured answer in full sentences. "
                        "If the context only partially covers the question, answer "
                        "from what IS available — do not refuse unless the context "
                        "contains absolutely nothing relevant. "
                        "Never say 'the context says' or describe the sources — just answer directly."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Context:\n{context}\n\nQuestion: {req.question}\n\nAnswer:",
                },
            ]

            client = get_client()
            _pace_request()
            answer_tokens = []

            stream = client.chat.completions.create(
                model=_model_name(),
                messages=messages,
                temperature=0.2,
                max_tokens=int(os.getenv("LLM_ANSWER_MAX_TOKENS", "800")),
                stream=True,
            )
            for chunk in stream:
                delta = chunk.choices[0].delta
                token = getattr(delta, "content", None) or ""
                if token:
                    answer_tokens.append(token)
                    yield event({"type": "token", "text": token})

            full_answer = "".join(answer_tokens).strip()
            if not full_answer:
                full_answer = ABSTENTION_MESSAGE

            # DETERMINISTIC PERCENTAGE CHECK — temporarily disabled, was over-triggering.
            # if _contains_unverified_percentage(full_answer, evidence):
            #     full_answer = ABSTENTION_FALLBACK_MESSAGE

            # ── 3. Verify claims — all-or-nothing: any bad claim → abstain ───
            if ABSTENTION_MESSAGE in full_answer:
                yield event({
                    "type": "verify",
                    "claims": [],
                    "sources": evidence,
                    "abstained": True,
                    "rounds": 0,
                    "hallucination_risk_score": 0,
                })
            else:
                verdicts = extract_and_verify_claims(full_answer, evidence)
                has_bad = any(
                    v.get("verdict", "").upper() in ("UNSUPPORTED", "CONTRADICTED")
                    for v in verdicts
                )
                if has_bad:
                    # Discard answer — show abstention instead of hallucinated content.
                    # Patch the already-streamed text box via a replace token.
                    yield event({"type": "token", "text": "", "replace": ABSTENTION_FALLBACK_MESSAGE})
                    yield event({
                        "type": "verify",
                        "claims": [{
                            "claim": req.question,
                            "verdict": "ABSTAINED",
                            "reason": "Answer contained unsupported claims; system abstains.",
                            "source_ids": [],
                            "quote": "",
                        }],
                        "sources": evidence,
                        "abstained": True,
                        "rounds": 0,
                        "hallucination_risk_score": 0,
                        "answer_override": ABSTENTION_FALLBACK_MESSAGE,
                    })
                else:
                    yield event({
                        "type": "verify",
                        "claims": verdicts,
                        "sources": evidence,
                        "abstained": False,
                        "rounds": 0,
                        "hallucination_risk_score": compute_hallucination_risk_score(verdicts, False),
                    })

            yield event({"type": "done"})

        except Exception as exc:
            yield event({"type": "error", "message": str(exc)})
            yield event({"type": "done"})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
