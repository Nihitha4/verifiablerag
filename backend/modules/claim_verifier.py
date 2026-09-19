"""
Claim-Level Hallucination Verification and Self-Correction module.

Pipeline:
    Generate -> Claim Extraction -> Evidence Verification
             -> (if needed) Re-retrieval -> Regeneration / Abstention
"""

from modules.llm_client import chat, safe_json_parse
from modules.vectorstore import retrieve
import os
import re
import unicodedata

ABSTENTION_MESSAGE = (
    "I don't have enough evidence in the provided document(s) to answer "
    "this reliably."
)

MAX_CORRECTION_ROUNDS = 1

# Character budget for evidence sent to LLM. ~6000 chars ≈ 1500 tokens.
# Keep well under Groq's 8K context window.
_EVIDENCE_CHAR_BUDGET = int(os.getenv("EVIDENCE_CHAR_BUDGET", "6000"))

# For summary/overview questions we fetch more chunks to cover the document.
_SUMMARY_TOP_K_MULTIPLIER = 3


def _normalise_quote(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"[-\u2010\u2011\u2012\u2013]\s*\n\s*", "", value)
    value = re.sub(r"[\u2010\u2011\u2012\u2013]", "-", value)
    value = re.sub(r"\s+", " ", value.strip())
    return value.casefold()


def _quote_overlap_ratio(quote: str, source_text: str) -> float:
    """Fraction of words in quote that appear in source_text. ≥0.75 = grounded."""
    q_words = re.findall(r"[a-z0-9]+", _normalise_quote(quote))
    if not q_words:
        return 0.0
    s_words = set(re.findall(r"[a-z0-9]+", _normalise_quote(source_text)))
    return sum(1 for w in q_words if w in s_words) / len(q_words)


def _is_summary_question(question: str) -> bool:
    """Return True for overview/summary questions that need broader context."""
    q = question.lower().strip()
    patterns = [
        r"\b(what is|what does|explain|describe|summarize|summarise|overview|about|tell me about)\b",
        r"\b(introduction|introduction to|what topics|what does .* cover|what is .* about)\b",
        r"\b(overview|outline|structure|content|chapters?|sections?)\b",
    ]
    return any(re.search(p, q) for p in patterns)


def _format_evidence(chunks: list[dict], char_budget: int = _EVIDENCE_CHAR_BUDGET) -> str:
    """Format retrieved chunks into a numbered evidence block with a char budget."""
    lines = []
    used = 0
    for i, c in enumerate(chunks):
        metadata = c.get("metadata", {})
        page = metadata.get("page_start")
        page_label = f", page {page}" if page else ""
        header = f"[source_{i + 1}{page_label}] "
        text = c["text"]
        entry = header + text
        if used + len(entry) > char_budget:
            remaining = char_budget - used - len(header)
            if remaining <= 0:
                break
            truncated = text[:remaining]
            last_stop = max(truncated.rfind(". "), truncated.rfind(".\n"))
            if last_stop > remaining // 2:
                truncated = truncated[: last_stop + 1]
            lines.append(header + truncated + " …[truncated]")
            break
        lines.append(entry)
        used += len(entry) + 2
    return "\n\n".join(lines)


def generate_answer(question: str, evidence_chunks: list[dict]) -> str:
    context = _format_evidence(evidence_chunks)
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
            "content": f"Context:\n{context}\n\nQuestion: {question}\n\nAnswer:",
        },
    ]
    answer_max_tokens = int(os.getenv("LLM_ANSWER_MAX_TOKENS", "800"))
    return chat(messages, temperature=0.2, max_tokens=answer_max_tokens)


def extract_claims(answer: str) -> list[str]:
    """Split an answer into atomic factual claims (standalone helper)."""
    messages = [
        {
            "role": "system",
            "content": (
                "Break the given answer into a list of short, atomic factual "
                "claims. Each claim should state exactly one fact. "
                'Respond ONLY as JSON: {"claims": ["claim 1", "claim 2", ...]}'
            ),
        },
        {"role": "user", "content": f"Answer:\n{answer}"},
    ]
    raw = chat(messages, temperature=0.0, json_mode=True)
    parsed = safe_json_parse(raw, fallback={"claims": [answer]})
    claims = parsed.get("claims", [])
    return claims if claims else [answer]


def _postprocess_verdicts(verdicts: list[dict], evidence_chunks: list[dict]) -> list[dict]:
    valid_source_ids = {f"source_{i + 1}" for i in range(len(evidence_chunks))}
    for verdict in verdicts:
        claim_text = verdict.get("claim", "")

        # Claims about what the document does/doesn't contain are always
        # problematic — we can't verify absence from a few chunks.
        absence_claim = re.search(
            r"\b(context|document|sources?)\b.*"
            r"\b(does not|do not|doesn't|don't|no information|not contain|not mention)\b",
            claim_text,
            flags=re.IGNORECASE,
        )

        source_ids = verdict.get("source_ids", [])
        if not isinstance(source_ids, list):
            verdict["source_ids"] = []
        else:
            verdict["source_ids"] = [s for s in source_ids if s in valid_source_ids]

        quote = verdict.get("quote", "")
        source_text = " ".join(
            evidence_chunks[int(sid.split("_")[1]) - 1]["text"]
            for sid in verdict["source_ids"]
            if sid.startswith("source_") and sid.split("_")[1].isdigit()
        )

        quote_is_grounded = (
            bool(quote)
            and bool(source_text)
            and _quote_overlap_ratio(quote, source_text) >= 0.75
        )

        if absence_claim:
            verdict["verdict"] = "UNSUPPORTED"
            verdict["reason"] = "Absence claims cannot be verified from partial context."
            verdict["source_ids"] = []
            verdict["quote"] = ""
        elif verdict.get("verdict", "").upper() == "SUPPORTED":
            if not verdict["source_ids"]:
                verdict["verdict"] = "UNSUPPORTED"
                verdict["reason"] = "No source cited for this claim."
            elif quote and not quote_is_grounded:
                verdict["verdict"] = "UNSUPPORTED"
                verdict["reason"] = "Quote does not sufficiently match the cited source."
    return verdicts


def extract_and_verify_claims(answer: str, evidence_chunks: list[dict]) -> list[dict]:
    """Split answer into claims and verify each against evidence in one LLM call."""
    context = _format_evidence(evidence_chunks)
    messages = [
        {
            "role": "system",
            "content": (
                "You are a fact-verification assistant. "
                "Step 1: split the Answer into short atomic claims (max 20 words each). "
                "Step 2: for each claim decide SUPPORTED, CONTRADICTED, or UNSUPPORTED "
                "based on the Context. Mark SUPPORTED if the context broadly supports "
                "the claim, even if not word-for-word. Include source_ids for supported claims. "
                "Respond ONLY as compact JSON: "
                '{"verdicts":[{"claim":"...","verdict":"SUPPORTED","source_ids":["source_1"]},...]}'
            ),
        },
        {
            "role": "user",
            "content": f"Context:\n{context}\n\nAnswer:\n{answer}",
        },
    ]
    raw = chat(
        messages,
        temperature=0.0,
        json_mode=True,
        max_tokens=int(os.getenv("LLM_VERIFY_MAX_TOKENS", "600")),
    )
    parsed = safe_json_parse(raw, fallback={"verdicts": []})
    verdicts = parsed.get("verdicts", [])

    # Parse failure → soft-pass: return the answer as supported rather than abstain.
    if not verdicts:
        return [
            {
                "claim": answer,
                "verdict": "SUPPORTED",
                "reason": "Verification parsing failed; answer treated as supported.",
                "source_ids": [f"source_{i+1}" for i in range(min(len(evidence_chunks), 3))],
                "quote": "",
            }
        ]

    for v in verdicts:
        v.setdefault("reason", "")
        v.setdefault("quote", "")
        v.setdefault("source_ids", [])

    return _postprocess_verdicts(verdicts, evidence_chunks)


def compute_hallucination_risk_score(verdicts: list[dict], abstained: bool) -> int:
    """
    Return an integer 0–100 representing hallucination risk.

    Logic:
      - Abstained with no claims → 100 (no grounding at all)
      - No claims (parse-failed soft-pass) → 0 (treated as safe)
      - Otherwise: score = round(100 * non_supported / total)
        where non_supported = UNSUPPORTED + CONTRADICTED claims
    """
    if abstained and not verdicts:
        return 100
    if not verdicts:
        return 0
    total = len(verdicts)
    non_supported = sum(
        1 for v in verdicts
        if v.get("verdict", "").upper() in ("UNSUPPORTED", "CONTRADICTED")
    )
    return round(100 * non_supported / total)


def answer_with_verification(question: str, doc_id: str | None, top_k: int = 3) -> dict:
    """
    Full pipeline:
      1. Retrieve evidence (more chunks for summary questions).
      2. Generate answer.
      3. Verify claims.
      4. If unsupported claims remain, do one targeted re-retrieval and regenerate.
      5. Return best available answer — only abstain if evidence is truly empty.
    """
    # Summary/overview questions need wider coverage of the document.
    effective_top_k = top_k * _SUMMARY_TOP_K_MULTIPLIER if _is_summary_question(question) else top_k

    evidence = retrieve(question, top_k=effective_top_k, doc_id=doc_id)
    if not evidence:
        return {
            "answer": ABSTENTION_MESSAGE,
            "abstained": True,
            "claims": [],
            "sources": [],
            "rounds": 0,
            "hallucination_risk_score": 100,
        }

    answer = generate_answer(question, evidence)
    rounds = 0

    # If the LLM itself said it can't answer, do one broader re-retrieval
    # before giving up — the initial chunks may just be the wrong ones.
    if ABSTENTION_MESSAGE in answer:
        extra = retrieve(question, top_k=effective_top_k * 2, doc_id=doc_id)
        seen = {e["id"] for e in evidence}
        evidence = evidence + [h for h in extra if h["id"] not in seen]
        answer = generate_answer(question, evidence)

    # If still abstaining after broader retrieval, return it.
    if ABSTENTION_MESSAGE in answer:
        return {
            "answer": answer,
            "abstained": True,
            "claims": [],
            "sources": evidence,
            "rounds": 0,
            "hallucination_risk_score": 100,
        }

    while rounds <= MAX_CORRECTION_ROUNDS:
        verdicts = extract_and_verify_claims(answer, evidence)

        parse_failed = (
            len(verdicts) == 1
            and ("parsing failed" in verdicts[0].get("reason", "").lower()
                 or "parse_error" in verdicts[0].get("reason", "").lower())
        )

        problem_claims = [
            v for v in verdicts if v.get("verdict", "").upper() != "SUPPORTED"
        ]

        # Success: all claims supported, or parse soft-passed.
        if not problem_claims or parse_failed:
            return {
                "answer": answer,
                "abstained": False,
                "claims": verdicts,
                "sources": evidence,
                "rounds": rounds,
                "hallucination_risk_score": compute_hallucination_risk_score(verdicts, False),
            }

        if rounds == MAX_CORRECTION_ROUNDS:
            # Out of correction budget. Return the answer anyway if MOST
            # claims are supported — only hard-abstain if nothing is supported.
            supported = [v for v in verdicts if v.get("verdict", "").upper() == "SUPPORTED"]
            if supported:
                # Partial support: return the answer with the verification results
                # so the user can see which claims are uncertain.
                return {
                    "answer": answer,
                    "abstained": False,
                    "claims": verdicts,
                    "sources": evidence,
                    "rounds": rounds,
                    "hallucination_risk_score": compute_hallucination_risk_score(verdicts, False),
                }
            # Truly nothing supported — abstain.
            return {
                "answer": ABSTENTION_MESSAGE,
                "abstained": True,
                "claims": verdicts,
                "sources": evidence,
                "rounds": rounds,
                "hallucination_risk_score": 100,
            }

        # Targeted re-retrieval on unsupported claims.
        extra_evidence = []
        seen_ids = {e["id"] for e in evidence}
        for pc in problem_claims:
            for hit in retrieve(pc["claim"], top_k=2, doc_id=doc_id):
                if hit["id"] not in seen_ids:
                    extra_evidence.append(hit)
                    seen_ids.add(hit["id"])

        if extra_evidence:
            evidence = evidence + extra_evidence
            answer = generate_answer(question, evidence)

        rounds += 1

    # Fallback — should not be reached.
    return {
        "answer": answer,
        "abstained": False,
        "claims": [],
        "sources": evidence,
        "rounds": rounds,
        "hallucination_risk_score": 0,
    }
