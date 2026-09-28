"""
Claim-Level Hallucination Verification and Self-Correction module.

Pipeline:
    Generate -> Claim Extraction -> Evidence Verification
             -> (if needed) Re-retrieval -> Regeneration / Abstention
"""

import os
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
import re
import unicodedata

from modules.llm_client import chat, safe_json_parse
from modules.vectorstore import retrieve

ABSTENTION_MESSAGE = (
    "I don't have enough evidence in the provided document(s) to answer "
    "this reliably."
)
ABSTENTION_FALLBACK_MESSAGE = (
    "The document does not state this."
)
ABSTENTION_PATTERNS = (
    "does not explicitly provide this information",
    "does not provide enough information",
    "does not explicitly report this improvement",
    "does not state this",
    "not enough evidence",
    "cannot be answered from the provided evidence",
    "i don't have enough evidence",
)

# Regex patterns that force a claim to ABSTAINED regardless of what the
# verification model decided.  These match sentences that *are themselves*
# abstention statements (e.g. "The document does not state the CPU utilisation").
ABSTENTION_REGEX_PATTERNS = [
    r"does not (explicitly )?state",
    r"does not (explicitly )?provide",
    r"do(es)? not contain",
    r"do(es)? not (give|mention|report|specify)",
    r"not specified in the (document|text|passages?)",
    r"not stated in the (document|text|passages?)",
    r"no specific (numeric|percentage|exact) (value|figure|number)",
    r"(passages?|excerpts?|document|text) (supplied |provided )?do(es)? not",
    r"the document does not",
    r"documents? do(es)? not (explicitly )?(state|provide|mention|give|specify)",
    r"(cannot|can't) be (determined|found|verified|confirmed) from (the )?(document|passage|context|text)",
    r"no (information|data|detail|figure|value|mention) (is )?(available|provided|given|found) (in |within )?(the )?(document|passage|context|text)",
    r"not (available|provided|mentioned|specified|given|reported) in (the )?(document|text|passage|context)",
    r"(text|document|passage|source) (does not|do not|doesn't) (report|mention|state|give|contain|specify)",
]

MAX_CORRECTION_ROUNDS = int(os.getenv("MAX_CORRECTION_ROUNDS", "0"))

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


def _is_correct_abstention(answer_text: str) -> bool:
    """Return True only when the answer explicitly refuses to answer and does not assert a numeric or relationship claim."""
    if not answer_text:
        return False

    lowered = answer_text.strip().lower()
    if not any(pattern in lowered for pattern in ABSTENTION_PATTERNS):
        return False

    # A genuine abstention must avoid asserting a specific value, before/after comparison,
    # or improvement/change claim. If the answer text includes a numeric or relationship
    # claim, it is not a correct abstention.
    forbidden_patterns = [
        r"\d+(?:\.\d+)?\s*(?:%|percent|percentage|times|x|points?)",
        r"\b(?:from|to|before|after|by)\b[^.]{0,80}\d+(?:\.\d+)?\s*(?:%|percent|percentage|times|x|points?)",
        r"\b(?:improved|improvement|increase|increased|decrease|decreased|reduction|drop|dropped|gain|rose|fell|change|difference|growth)\b[^.]{0,120}\d+(?:\.\d+)?\s*(?:%|percent|percentage|times|x|points?)",
    ]

    if any(re.search(pattern, lowered) for pattern in forbidden_patterns):
        return False

    # Allow phrasing like "The document does not provide enough information to determine
    # the exact percentage improvement" without asserting a numerical value.
    return True


def _is_pure_abstention_answer(answer_text: str) -> bool:
    """Return True when the answer is only a refusal and contains no separate factual content."""
    if not answer_text or not _is_correct_abstention(answer_text):
        return False
    text = answer_text.strip()
    return text and text.lower().startswith(("the document does not", "i don't have enough evidence", "cannot be answered"))


def _looks_like_derived_numeric_claim(claim_text: str, evidence_chunks: list[dict]) -> bool:
    """Return True when the claim looks like a derived numeric conclusion not explicitly in evidence."""
    if not claim_text or not re.search(r"\d", claim_text):
        return False

    numeric_relation_patterns = [
        r"\b(?:improve|improvement|improved|increased|increase|decrease|decreased|reduction|drop|gain|rise|fell|growth|change)\b",
        r"\b(?:percent|percentage)\b",
        r"\b\d+\s*(?:%|percent|percentage)\b",
        r"\bfrom\s+\d+(?:\.\d+)?\s*(?:%|percent|percentage)?\s+to\s+\d+(?:\.\d+)?\s*(?:%|percent|percentage)?\b",
        r"\b(?:by|of)\s+\d+(?:\.\d+)?\s*(?:%|percent|percentage)\b",
    ]
    approximation_words = [
        r"\b(?:about|approximately|roughly|around)\b",
        r"\b(?:approx|approx\.)\b",
    ]
    if not any(re.search(p, claim_text, flags=re.IGNORECASE) for p in numeric_relation_patterns):
        return False

    evidence_text = " ".join(chunk.get("text", "") for chunk in evidence_chunks).lower()
    normalized_claim = claim_text.lower()

    if normalized_claim in evidence_text:
        return False

    # Approximation language does not salvage unsupported numeric claims.
    if any(re.search(p, claim_text, flags=re.IGNORECASE) for p in approximation_words):
        return True

    # Allow the claim only if the exact relationship is explicitly stated in the evidence.
    for pattern in numeric_relation_patterns:
        if re.search(pattern, claim_text, flags=re.IGNORECASE) and not re.search(pattern, evidence_text, flags=re.IGNORECASE):
            return True

    return False


def _is_exact_value_request(question: str) -> bool:
    """Return True when the user asks for an exact value/improvement that cannot be guessed."""
    q = question.strip().lower()
    if not q:
        return False

    exact_markers = [
        r"\bexact(?:ly)?\b",
        r"\bprecise\b",
        r"\bspecific\b",
        r"\b(?:percentage|percent|value|amount|improvement|increase|decrease|reduction|gain|drop|change|difference)\b",
    ]
    request_markers = [
        r"\bwhat\b",
        r"\bhow much\b",
        r"\bhow many\b",
        r"\bgive me\b",
        r"\btell me\b",
    ]

    return any(re.search(p, q) for p in exact_markers) and any(
        re.search(p, q) for p in request_markers
    )


def _is_numeric_relationship_request(question: str) -> bool:
    """True when the question asks for a change/improvement relationship, not just a value."""
    q = question.strip().lower()
    if not q:
        return False
    return any(
        re.search(p, q)
        for p in [
            r"\b(?:improvement|improve|increase|increased|decrease|decreased|change|difference|gain|drop|reduction)\b",
            r"\b(?:how much|what was the|what is the)\b.*\b(?:improvement|increase|decrease|change|difference)\b",
        ]
    )


def _is_procedural_question(question: str) -> bool:
    """Return True when the user asks for instructions or a sequence of steps."""
    q = question.strip().lower()
    if not q:
        return False
    return any(
        re.search(pattern, q)
        for pattern in [
            r"\bhow\s+(?:do|does|did|can|should|to)\b",
            r"\bwhat\s+are\s+the\s+(?:steps|procedures?|instructions?)\b",
            r"\b(?:steps?|procedure|instructions?|protocol|workflow|process|method)\b",
            r"\bwalk me through\b",
            r"\bguide me\b",
        ]
    )


def _has_numeric_claim_without_support(text: str, evidence_chunks: list[dict]) -> bool:
    """Return True when the text asserts a numeric relationship not explicitly grounded in evidence."""
    if not text:
        return False

    numeric_markers = [
        r"\b\d+(?:\.\d+)?\s*(?:%|percent|percentage)\b",
        r"\b(?:from|to|by)\b[^.]{0,80}\d+(?:\.\d+)?\s*(?:%|percent|percentage)",
        r"\b(?:improve|improvement|improved|increase|increased|decrease|decreased|reduction|drop|gain|rose|fell|growth|change|difference)\b[^.]{0,120}\d+(?:\.\d+)?\s*(?:%|percent|percentage)",
    ]
    if not any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in numeric_markers):
        return False

    if re.search(r"\b(?:about|approximately|roughly|around)\b", text, flags=re.IGNORECASE):
        return True

    evidence_text = " ".join(chunk.get("text", "") for chunk in evidence_chunks)
    for pattern in numeric_markers:
        if re.search(pattern, text, flags=re.IGNORECASE) and not re.search(pattern, evidence_text, flags=re.IGNORECASE):
            return True
    return False


def _evidence_has_explicit_exact_numeric_value(question: str, evidence_chunks: list[dict]) -> bool:
    """Only accept exact numeric answers when the evidence explicitly states the relationship."""
    if not _is_exact_value_request(question) and not _is_numeric_relationship_request(question):
        return True

    evidence_text = " ".join(chunk.get("text", "") for chunk in evidence_chunks)
    # Do not accept a number as an improvement/change unless the source explicitly states
    # the original, resulting number, and their relationship.
    explicit_patterns = [
        r"\b\d+(?:\.\d+)?\s*(?:%|percent|percentage)\b",
        r"\bfrom\s+\d+(?:\.\d+)?\s*(?:%|percent|percentage)?\s+to\s+\d+(?:\.\d+)?\s*(?:%|percent|percentage)?\b",
        r"\b(?:improved|improvement|increased|increase|decreased|decrease|reduction|drop|gain|rose|fell|changed)\b[^.]{0,200}\b\d+(?:\.\d+)?\s*(?:%|percent|percentage)\b",
        r"\b(?:by|of)\s+\d+(?:\.\d+)?\s*(?:%|percent|percentage)\b",
    ]
    # Range/target/example/theoretical values are not explicit measured improvements.
    banned_patterns = [
        r"\b(?:range|ranges|target|targets|example|examples|theoretical|operating condition|conditions)\b",
    ]
    if any(re.search(pattern, evidence_text, flags=re.IGNORECASE) for pattern in banned_patterns):
        return False
    return any(re.search(pattern, evidence_text, flags=re.IGNORECASE) for pattern in explicit_patterns)


def generate_answer(question: str, evidence_chunks: list[dict]) -> str:
    context = _format_evidence(evidence_chunks)
    messages = [
        {
            "role": "system",
            "content": (
                "You are a document-grounded QA assistant. Answer the question using ONLY "
                "the passages provided below.\n\n"
                "Never fill gaps with common practice, background knowledge, or plausible "
                "steps. This is especially important for step-by-step, procedural, method, "
                "protocol, workflow, and 'how do I' questions: include a step only when the "
                "passages explicitly describe or support that step. If the passages do not "
                "contain the requested procedure, say exactly that the document does not "
                "state the procedure and do not provide a substitute procedure.\n\n"
                "For general, descriptive, or conceptual questions (definitions, explanations, "
                "comparisons, purposes, differences) — answer normally and fully if the passages "
                "contain the relevant information. Do not refuse or hedge on these unless the "
                "passages truly contain nothing relevant to the question.\n\n"
                "The ONLY thing you must be strict about is NUMBERS: never state a specific "
                "percentage, statistic, or before/after numeric relationship unless that exact "
                "number appears in the passages. If a numeric figure the question asks for is "
                "not explicitly in the passages, say so plainly for that part only — "
                "e.g. 'The document does not state the exact percentage.' — but still answer "
                "any other, non-numeric part of the question normally.\n\n"
                "Do not invent, estimate, round, or combine separate numbers into a new "
                "relationship that the passages do not state directly.\n\n"
                "Write in full, natural sentences. Do not add unnecessary disclaimers to "
                "answers that are already fully supported by the passages."
            ),
        },
        {
            "role": "user",
            "content": f"Context:\n{context}\n\nQuestion: {question}\n\nAnswer:",
        },
    ]
    answer_max_tokens = int(os.getenv("LLM_ANSWER_MAX_TOKENS", "800"))
    result = chat(messages, temperature=0.2, max_tokens=answer_max_tokens)
    return result if result and result.strip() else ABSTENTION_MESSAGE

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


def _detect_cross_doc_contradictions(verdicts: list[dict], evidence_chunks: list[dict]) -> list[dict]:
    """
    Post-processing pass: flag claims whose cited sources span multiple documents.

    For each SUPPORTED claim, look up the doc_id of every cited source chunk.
    If sources come from 2+ distinct documents, it means the answer is drawing
    on multiple docs for the same claim — a potential contradiction point.
    Set `cross_doc_conflict=True` and list the conflicting doc filenames.

    Additionally, if the evidence pool contains a CONTRADICTED chunk from a
    different doc than the supporting one, escalate to cross_doc_conflict.
    """
    for verdict in verdicts:
        verdict.setdefault("cross_doc_conflict", False)
        verdict.setdefault("conflict_docs", [])

        if verdict.get("verdict", "").upper() not in ("SUPPORTED",):
            continue

        cited_ids = verdict.get("source_ids", [])
        if not cited_ids:
            continue

        # Collect doc_ids for each cited source
        doc_map: dict[str, str] = {}  # source_id -> doc filename
        for sid in cited_ids:
            if sid.startswith("source_") and sid.split("_")[1].isdigit():
                idx = int(sid.split("_")[1]) - 1
                if 0 <= idx < len(evidence_chunks):
                    chunk = evidence_chunks[idx]
                    meta = chunk.get("metadata", {})
                    doc_id = meta.get("doc_id", "")
                    filename = meta.get("filename", doc_id)
                    doc_map[sid] = filename

        unique_docs = list(dict.fromkeys(doc_map.values()))  # preserve order, dedupe
        if len(unique_docs) >= 2:
            verdict["cross_doc_conflict"] = True
            verdict["conflict_docs"] = unique_docs
            continue

        # Check if ANY non-cited chunk in a DIFFERENT doc contradicts this claim
        # by looking for chunks from other docs in the evidence pool
        cited_doc = unique_docs[0] if unique_docs else None
        if not cited_doc:
            continue

        other_doc_names = set()
        for i, chunk in enumerate(evidence_chunks):
            sid = f"source_{i + 1}"
            if sid in cited_ids:
                continue
            meta = chunk.get("metadata", {})
            fname = meta.get("filename", meta.get("doc_id", ""))
            if fname and fname != cited_doc:
                other_doc_names.add(fname)

        if other_doc_names:
            # Evidence pool has chunks from other docs — flag for awareness
            # only when there are 2+ docs in the overall pool (multi-doc query)
            all_pool_docs = {
                c.get("metadata", {}).get("filename", "") for c in evidence_chunks
            }
            if len(all_pool_docs) >= 2:
                verdict["cross_doc_conflict"] = True
                verdict["conflict_docs"] = [cited_doc] + sorted(other_doc_names)

    return verdicts


def _claim_matches_abstention_regex(claim_text: str) -> bool:
    """Return True when the claim sentence is itself an abstention statement (regex hard-override)."""
    return any(
        re.search(p, claim_text, re.IGNORECASE) for p in ABSTENTION_REGEX_PATTERNS
    )


def _postprocess_verdicts(verdicts: list[dict], evidence_chunks: list[dict]) -> list[dict]:
    valid_source_ids = {f"source_{i + 1}" for i in range(len(evidence_chunks))}
    for verdict in verdicts:
        claim_text = verdict.get("claim", "")

        # ── Hard override: if the claim text is itself an abstention statement,
        # force ABSTAINED regardless of what the verification model decided.
        # This guarantees correct scoring for sentences like
        # "The document does not state the CPU utilisation percentage."
        if verdict.get("verdict", "").upper() == "UNSUPPORTED" and _claim_matches_abstention_regex(claim_text):
            print(f"[POSTPROCESS] regex override UNSUPPORTED→ABSTAINED: {claim_text[:80]!r}")
            verdict["verdict"] = "ABSTAINED"
            verdict["reason"] = "Claim is itself an abstention statement; forced to ABSTAINED by regex override."
            verdict["source_ids"] = []
            verdict["quote"] = ""
            continue
        elif verdict.get("verdict", "").upper() == "UNSUPPORTED":
            print(f"[POSTPROCESS] UNSUPPORTED not caught by regex: {claim_text[:80]!r}")

        # A correct abstention is only valid when the answer does not also assert
        # a different unsupported factual answer. All factual claims must still be
        # checked individually before marking the whole answer as abstained.
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

        if _is_correct_abstention(claim_text):
            verdict["verdict"] = "ABSTAINED"
            verdict["reason"] = "The answer correctly refused to provide information that cannot be verified from the retrieved document."
            verdict["source_ids"] = []
            verdict["quote"] = ""
        elif absence_claim:
            verdict["verdict"] = "ABSTAINED"
            verdict["reason"] = "Claim states information is absent — this is an abstention, not a factual assertion."
            verdict["source_ids"] = []
            verdict["quote"] = ""
        elif _looks_like_derived_numeric_claim(claim_text, evidence_chunks):
            verdict["verdict"] = "UNSUPPORTED"
            verdict["reason"] = "The claim derives a numerical relationship not explicitly stated in the evidence."
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


def _normalize_numeric_text(value: str) -> str:
    """Normalize a source or answer string so numeric comparisons are less fragile."""
    if not value:
        return ""
    value = value.lower().strip()
    value = value.replace("percent", "%").replace("percentage", "%")
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"(?<=\d)\s*%", "%", value)
    value = re.sub(r"[^a-z0-9%\.\-]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _strip_unverified_numeric_sentences(answer: str, evidence_chunks: list[dict]) -> str:
    """Remove sentences that assert a number/percentage not present in retrieved evidence.

    Splits on sentence-terminal punctuation (.!?), checks each sentence for
    unverified numeric claims, and replaces offenders with a neutral refusal.
    Also cleans up dangling lead-in fragments (e.g. "In the textbook,") that
    the LLM placed before a numeric sentence that got removed.
    """
    if not answer:
        return answer

    evidence_text = "\n".join(chunk.get("text", "") for chunk in evidence_chunks)
    evidence_norm = _normalize_numeric_text(evidence_text)

    def sentence_has_verifiable_number(sentence: str) -> bool:
        if not re.search(r"\d+(?:\.\d+)?\s*(?:%|percent|percentage)", sentence, flags=re.IGNORECASE):
            return True

        sentence_norm = _normalize_numeric_text(sentence)
        for token in re.findall(r"\d+(?:\.\d+)?%?", sentence_norm):
            if not token:
                continue
            token_norm = token.lower().replace("percent", "%").replace("percentage", "%")
            if token_norm in evidence_norm:
                continue
            stem = token_norm.replace("%", "")
            if any(stem in other for other in [t.replace("%", "") for t in re.findall(r"\d+(?:\.\d+)?%?", evidence_norm)]):
                continue
            return False
        return True

    sentences = re.split(r"(?<=[.!?])\s+", answer.strip())
    cleaned = []
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if re.search(r"\d+(?:\.\d+)?\s*(?:%|percent|percentage)", sentence, flags=re.IGNORECASE):
            if not sentence_has_verifiable_number(sentence):
                cleaned.append("The document does not state this.")
                continue
        cleaned.append(sentence)

    # Remove dangling lead-in fragments: short sentences (≤8 words) that end
    # with a comma and are immediately followed by "The document does not state this."
    # e.g. "In the textbook," left behind when the numeric sentence after it was removed.
    result = []
    PLACEHOLDER = "The document does not state this."
    for i, s in enumerate(cleaned):
        # A dangling fragment: ends with comma (possibly + space), short, no verb asserting a fact
        is_dangling = (
            s.rstrip().endswith(",")
            and len(s.split()) <= 8
            and i + 1 < len(cleaned)
            and cleaned[i + 1] == PLACEHOLDER
        )
        if is_dangling:
            continue  # drop the orphaned lead-in
        result.append(s)

    # Deduplicate consecutive identical placeholders
    deduped = []
    for s in result:
        if deduped and deduped[-1] == PLACEHOLDER and s == PLACEHOLDER:
            continue
        deduped.append(s)

    return " ".join(deduped).strip()


def _repair_unsupported_sentences(answer: str, verdicts: list[dict]) -> str:
    """Replace unsupported/contradicted sentences with a neutral refusal before display."""
    if not answer:
        return answer

    repaired = []
    for sentence in re.split(r"(?<=[.!?])\s+", answer.strip()):
        sentence = sentence.strip()
        if not sentence:
            continue
        verdict = next((v for v in verdicts if v.get("claim", "").strip() == sentence or sentence in v.get("claim", "")), None)
        if verdict and verdict.get("verdict", "").upper() in ("UNSUPPORTED", "CONTRADICTED"):
            repaired.append("The document does not state this")
        else:
            repaired.append(sentence)
    return " ".join(repaired).strip()


def extract_and_verify_claims(
    answer: str,
    evidence_chunks: list[dict],
    question: str = "",
    strip_numeric: bool = True,
) -> list[dict]:
    """Split answer into claims and verify each against evidence in one LLM call."""
    if strip_numeric:
        answer = _strip_unverified_numeric_sentences(answer, evidence_chunks)
    context = _format_evidence(evidence_chunks)
    procedural_instruction = (
        "For procedural questions, mark every step UNSUPPORTED unless the Context "
        "explicitly describes that step. Do not treat general domain knowledge or "
        "a plausible sequence as support.\n"
        if _is_procedural_question(question)
        else ""
    )
    messages = [
        {
            "role": "system",
            "content": (
                "You are a fact-verification assistant.\n"
                "Step 1: split the Answer into short atomic claims (max 20 words each).\n"
                "Step 2: for each claim decide SUPPORTED, CONTRADICTED, UNSUPPORTED, or ABSTAINED "
                "based on the Context. Mark SUPPORTED if the context broadly supports the claim.\n\n"
                "CRITICAL RULE FOR ABSTAINED CLAIMS:\n"
                "Before classifying a claim as UNSUPPORTED, first ask: does this sentence assert "
                "a specific fact that could be true or false? A sentence that states information "
                "is missing, unavailable, not provided, not specified, or not contained in the "
                "source (in ANY phrasing -- e.g. 'does not state', 'do not contain', 'no specific "
                "figure is given', 'not mentioned', 'the text does not report') is NOT asserting "
                "a fact. It is declining to assert one. Classify any such sentence as ABSTAINED, "
                "never UNSUPPORTED, regardless of its exact wording.\n"
                "When you are unsure whether a sentence is an abstention or a factual assertion, "
                "default to ABSTAINED rather than UNSUPPORTED.\n"
                "Only use UNSUPPORTED when the claim asserts a specific fact, number, date, or "
                "relationship that the Context does not contain.\n\n"
                "Critical rule: do not accept derived numerical claims or inferred percentage "
                "changes unless the exact relationship is explicitly stated in the Context. "
                "If the Context does not give the exact percentage or exact numerical relationship, "
                "mark the claim UNSUPPORTED.\n"
                + procedural_instruction
                + "Respond ONLY as compact JSON: "
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
        max_tokens=int(os.getenv("LLM_VERIFY_MAX_TOKENS", "2500")),
    )
    parsed = safe_json_parse(raw, fallback={"verdicts": []})
    verdicts = parsed.get("verdicts", [])

    if _is_correct_abstention(answer):
        return [
            {
                "claim": answer,
                "verdict": "ABSTAINED",
                "reason": "The answer correctly refused to provide information that cannot be verified from the retrieved document without asserting an unsupported factual answer.",
                "source_ids": [],
                "quote": "",
            }
        ]

    # Parse failure or empty verification output means the answer could not be
    # grounded in the provided evidence. Do not silently treat it as supported.
    if not verdicts:
        return [
            {
                "claim": answer,
                "verdict": "UNSUPPORTED",
                "reason": "Verification returned no grounded claims; the answer could not be confirmed from the provided evidence.",
                "source_ids": [],
                "quote": "",
            }
        ]

    for v in verdicts:
        v.setdefault("reason", "")
        v.setdefault("quote", "")
        v.setdefault("source_ids", [])

    processed = _postprocess_verdicts(verdicts, evidence_chunks)
    return _detect_cross_doc_contradictions(processed, evidence_chunks)


def _contains_unverified_percentage(answer: str, evidence_chunks: list[dict]) -> bool:
    """
    Deterministic, non-LLM check: find every percentage figure in the answer.
    If any does not appear verbatim (same digits near % or 'percent') in the
    evidence text, the whole answer is treated as unsupported.

    This cannot be fooled by prompt-following failures — it's plain regex.
    """
    percentages_in_answer = re.findall(r"\d+\s*(?:%|percent)", answer, re.IGNORECASE)
    if not percentages_in_answer:
        return False  # no numeric claim to check

    evidence_text = " ".join(chunk.get("text", "") for chunk in evidence_chunks)

    for pct in percentages_in_answer:
        digits = re.search(r"\d+", pct).group()
        pattern = rf"{digits}\s*(?:%|percent)"
        if not re.search(pattern, evidence_text, re.IGNORECASE):
            print(f"[DETERMIN] percentage {digits}% not found in evidence — blocking answer")
            return True  # digit not in evidence → unsupported

    return False


def compute_hallucination_metrics(verdicts: list[dict], abstained: bool = False) -> dict:
    """
    Compute detailed hallucination percentages and grounding metrics.

    Returns a dictionary:
    - total: total number of claims evaluated
    - supported: count of SUPPORTED claims
    - unsupported: count of UNSUPPORTED claims
    - contradicted: count of CONTRADICTED claims
    - abstained: count of ABSTAINED claims
    - hallucination_count: unsupported + contradicted
    - hallucination_percentage: float (0.0 to 100.0)
    - supported_percentage: float (0.0 to 100.0)
    - unsupported_percentage: float (0.0 to 100.0)
    - contradicted_percentage: float (0.0 to 100.0)
    - abstained_percentage: float (0.0 to 100.0)
    - hallucination_risk_score: int (0 to 100)
    - risk_level: "None" | "Low" | "Moderate" | "High" | "Critical"
    """
    if not verdicts:
        return {
            "total": 0,
            "supported": 0,
            "unsupported": 0,
            "contradicted": 0,
            "abstained": 0,
            "hallucination_count": 0,
            "hallucination_percentage": 0.0,
            "supported_percentage": 0.0,
            "unsupported_percentage": 0.0,
            "contradicted_percentage": 0.0,
            "abstained_percentage": 0.0,
            "hallucination_risk_score": 0,
            "risk_level": "None",
        }

    counts = {
        "SUPPORTED": 0,
        "UNSUPPORTED": 0,
        "CONTRADICTED": 0,
        "ABSTAINED": 0,
    }
    for v in verdicts:
        verdict_str = v.get("verdict", "").upper()
        if verdict_str in counts:
            counts[verdict_str] += 1
        else:
            counts["UNSUPPORTED"] += 1

    total = sum(counts.values())
    bad_count = counts["UNSUPPORTED"] + counts["CONTRADICTED"]

    if total == 0:
        pct = 0.0
    elif abstained and bad_count == 0:
        pct = 0.0
    else:
        pct = round((bad_count / total) * 100.0, 1)

    supp_pct = round((counts["SUPPORTED"] / total) * 100.0, 1) if total > 0 else 0.0
    unsupp_pct = round((counts["UNSUPPORTED"] / total) * 100.0, 1) if total > 0 else 0.0
    contra_pct = round((counts["CONTRADICTED"] / total) * 100.0, 1) if total > 0 else 0.0
    abst_pct = round((counts["ABSTAINED"] / total) * 100.0, 1) if total > 0 else 0.0

    if pct == 0.0:
        risk_level = "None" if (counts["SUPPORTED"] > 0 or counts["ABSTAINED"] > 0) else "Low"
    elif pct <= 20.0:
        risk_level = "Low"
    elif pct <= 50.0:
        risk_level = "Moderate"
    elif pct <= 75.0:
        risk_level = "High"
    else:
        risk_level = "Critical"

    return {
        "total": total,
        "supported": counts["SUPPORTED"],
        "unsupported": counts["UNSUPPORTED"],
        "contradicted": counts["CONTRADICTED"],
        "abstained": counts["ABSTAINED"],
        "hallucination_count": bad_count,
        "hallucination_percentage": pct,
        "supported_percentage": supp_pct,
        "unsupported_percentage": unsupp_pct,
        "contradicted_percentage": contra_pct,
        "abstained_percentage": abst_pct,
        "hallucination_risk_score": int(round(pct)),
        "risk_level": risk_level,
    }


def compute_hallucination_risk_score(verdicts: list[dict], abstained: bool = False) -> int:
    """Return an integer 0–100 representing hallucination risk."""
    metrics = compute_hallucination_metrics(verdicts, abstained=abstained)
    return metrics["hallucination_risk_score"]


def _lexical_fallback_verification(answer: str, evidence_chunks: list[dict]) -> list[dict]:
    """Deterministic fallback claim verification when LLM client is unavailable or times out."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", answer.strip()) if s.strip()]
    if not sentences:
        sentences = [answer.strip()] if answer.strip() else []

    evidence_text = " ".join(c.get("text", "") for c in evidence_chunks)
    verdicts = []
    for s in sentences:
        if _claim_matches_abstention_regex(s) or _is_correct_abstention(s):
            verdicts.append({
                "claim": s,
                "sentence": s,
                "verdict": "ABSTAINED",
                "reason": "Statement is an abstention or notes absent information.",
                "source_ids": [],
                "quote": "",
            })
            continue

        if _looks_like_derived_numeric_claim(s, evidence_chunks) or _has_numeric_claim_without_support(s, evidence_chunks):
            verdicts.append({
                "claim": s,
                "sentence": s,
                "verdict": "UNSUPPORTED",
                "reason": "Contains numeric or statistical assertions not found in the reference evidence.",
                "source_ids": [],
                "quote": "",
            })
            continue

        best_sid = None
        best_overlap = 0.0
        best_chunk_text = ""
        for i, c in enumerate(evidence_chunks):
            chunk_t = c.get("text", "")
            overlap = _quote_overlap_ratio(s, chunk_t)
            if overlap > best_overlap:
                best_overlap = overlap
                best_sid = f"source_{i+1}"
                best_chunk_text = chunk_t

        if best_overlap >= 0.70:
            verdicts.append({
                "claim": s,
                "sentence": s,
                "verdict": "SUPPORTED",
                "reason": f"Grounded in reference context ({int(best_overlap*100)}% term match).",
                "source_ids": [best_sid] if best_sid else [],
                "quote": best_chunk_text[:200] + ("..." if len(best_chunk_text) > 200 else ""),
            })
        else:
            verdicts.append({
                "claim": s,
                "sentence": s,
                "verdict": "UNSUPPORTED",
                "reason": "Not sufficiently grounded in reference evidence (low lexical overlap).",
                "source_ids": [],
                "quote": "",
            })

    return verdicts


def verify_external_llm_answer(
    answer: str,
    evidence_chunks: list[dict],
    question: str = "",
) -> dict:
    """
    Deconstruct an external LLM-generated answer into atomic factual claims,
    evaluate each claim against retrieved or provided reference evidence,
    map results to individual sentences in the answer, and calculate
    comprehensive hallucination risk percentages.
    """
    if not answer or not answer.strip():
        empty_metrics = compute_hallucination_metrics([], abstained=True)
        return {
            "answer": "",
            "question": question,
            "claims": [],
            "sentences": [],
            "metrics": empty_metrics,
            "hallucination_percentage": 0.0,
            "hallucination_risk_score": 0,
            "risk_level": "None",
            "sources": evidence_chunks or [],
            "total_sources": len(evidence_chunks or []),
        }

    raw_sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", answer.strip()) if s.strip()]
    if not raw_sentences:
        raw_sentences = [answer.strip()]

    if not evidence_chunks:
        claims = [
            {
                "claim": s,
                "sentence": s,
                "verdict": "UNSUPPORTED",
                "reason": "No reference evidence was provided to verify this claim against.",
                "source_ids": [],
                "quote": "",
            }
            for s in raw_sentences
        ]
        metrics = compute_hallucination_metrics(claims, abstained=False)
        sentences_res = [
            {
                "sentence_index": idx,
                "text": s,
                "verdict": "UNSUPPORTED",
                "reason": "No reference evidence available to verify this statement.",
                "source_ids": [],
                "quote": "",
                "claims": [claims[idx]],
            }
            for idx, s in enumerate(raw_sentences)
        ]
        return {
            "answer": answer,
            "question": question,
            "claims": claims,
            "sentences": sentences_res,
            "metrics": metrics,
            "hallucination_percentage": metrics["hallucination_percentage"],
            "hallucination_risk_score": metrics["hallucination_risk_score"],
            "risk_level": metrics["risk_level"],
            "sources": [],
            "total_sources": 0,
        }

    context = _format_evidence(evidence_chunks)
    messages = [
        {
            "role": "system",
            "content": (
                "You are an expert Hallucination Auditor and Fact Verification Engine.\n"
                "You are auditing an answer generated by an external LLM against the provided Context passages.\n\n"
                "Your objective:\n"
                "1. Break down the Answer into individual atomic factual claims (each claim should express exactly one distinct fact).\n"
                "2. For each claim, determine the exact verdict against the Context:\n"
                "   - SUPPORTED: The claim is directly stated, verified, or accurately supported by the Context.\n"
                "   - UNSUPPORTED: The claim asserts a fact, statistic, percentage, date, name, or conclusion NOT present in or verified by the Context (Hallucination).\n"
                "   - CONTRADICTED: The claim directly contradicts or conflicts with what the Context states (Direct Hallucination/Error).\n"
                "   - ABSTAINED: The claim merely states that information is not available or makes a neutral non-factual remark.\n"
                "3. Be strict with numbers, dates, and percentages: if a figure is not explicitly verified by the Context, mark it UNSUPPORTED.\n"
                "4. Identify the matching original sentence from the answer, the reason for your verdict, verbatim quote from Context if grounded, and cited source IDs (e.g. ['source_1']).\n\n"
                "Respond ONLY with valid JSON in this format:\n"
                '{"verdicts": [\n'
                '  {"claim": "...", "sentence": "...", "verdict": "SUPPORTED"|"UNSUPPORTED"|"CONTRADICTED"|"ABSTAINED", "reason": "...", "quote": "...", "source_ids": ["source_1"]}\n'
                ']}'
            ),
        },
        {
            "role": "user",
            "content": f"Context:\n{context}\n\nQuestion/Prompt:\n{question or 'General factual inquiry'}\n\nLLM Answer to Verify:\n{answer}",
        },
    ]

    verdicts: list[dict] = []
    try:
        raw = chat(messages, temperature=0.0, json_mode=True, max_tokens=int(os.getenv("LLM_VERIFY_MAX_TOKENS", "1200")))
        parsed = safe_json_parse(raw, fallback={"verdicts": []})
        verdicts = parsed.get("verdicts", [])
    except Exception as exc:
        print(f"[VERIFY-EXTERNAL] LLM verification failed ({exc}), falling back to lexical verification")
        verdicts = _lexical_fallback_verification(answer, evidence_chunks)

    if not verdicts:
        verdicts = _lexical_fallback_verification(answer, evidence_chunks)

    for v in verdicts:
        v.setdefault("reason", "")
        v.setdefault("quote", "")
        v.setdefault("source_ids", [])
        v.setdefault("sentence", "")

    verdicts = _postprocess_verdicts(verdicts, evidence_chunks)
    verdicts = _detect_cross_doc_contradictions(verdicts, evidence_chunks)

    # Map claims to sentences
    sentence_analyses = []
    for idx, sentence in enumerate(raw_sentences):
        matching_claims = []
        for v in verdicts:
            v_sent = v.get("sentence", "").strip()
            v_claim = v.get("claim", "").strip()
            if v_sent and (v_sent in sentence or sentence in v_sent or _quote_overlap_ratio(v_sent, sentence) >= 0.5):
                matching_claims.append(v)
            elif _quote_overlap_ratio(v_claim, sentence) >= 0.4:
                matching_claims.append(v)

        if not matching_claims:
            if len(raw_sentences) == len(verdicts):
                matching_claims = [verdicts[idx]]
            elif len(raw_sentences) == 1 and verdicts:
                matching_claims = verdicts

        if any(c.get("verdict", "").upper() == "CONTRADICTED" for c in matching_claims):
            s_verdict = "CONTRADICTED"
            s_reason = next((c.get("reason") for c in matching_claims if c.get("verdict", "").upper() == "CONTRADICTED"), "Contradicts reference context.")
        elif any(c.get("verdict", "").upper() == "UNSUPPORTED" for c in matching_claims):
            s_verdict = "UNSUPPORTED"
            s_reason = next((c.get("reason") for c in matching_claims if c.get("verdict", "").upper() == "UNSUPPORTED"), "Claim is unverified by reference context.")
        elif any(c.get("verdict", "").upper() == "SUPPORTED" for c in matching_claims):
            s_verdict = "SUPPORTED"
            s_reason = next((c.get("reason") for c in matching_claims if c.get("verdict", "").upper() == "SUPPORTED"), "Supported by reference evidence.")
        elif any(c.get("verdict", "").upper() == "ABSTAINED" for c in matching_claims):
            s_verdict = "ABSTAINED"
            s_reason = "Statement notes missing evidence or is neutral."
        else:
            max_ov = max((_quote_overlap_ratio(sentence, c.get("text", "")) for c in evidence_chunks), default=0.0)
            if max_ov >= 0.65:
                s_verdict = "SUPPORTED"
                s_reason = "Substantial lexical overlap with reference evidence."
            else:
                s_verdict = "UNSUPPORTED"
                s_reason = "No matching grounded claim found in reference evidence."

        all_source_ids = list(dict.fromkeys(sid for c in matching_claims for sid in c.get("source_ids", [])))
        all_quotes = " | ".join(c.get("quote", "") for c in matching_claims if c.get("quote"))

        sentence_analyses.append({
            "sentence_index": idx,
            "text": sentence,
            "verdict": s_verdict,
            "reason": s_reason,
            "source_ids": all_source_ids,
            "quote": all_quotes,
            "claims": matching_claims,
        })

    metrics = compute_hallucination_metrics(verdicts, abstained=False)

    return {
        "answer": answer,
        "question": question,
        "claims": verdicts,
        "sentences": sentence_analyses,
        "metrics": metrics,
        "hallucination_percentage": metrics["hallucination_percentage"],
        "hallucination_risk_score": metrics["hallucination_risk_score"],
        "risk_level": metrics["risk_level"],
        "sources": evidence_chunks,
        "total_sources": len(evidence_chunks),
    }


def answer_with_verification(question: str, doc_id: str | None, top_k: int = 3) -> dict:
    """
    Simplified all-or-nothing pipeline:
      1. Retrieve evidence.
      2. Generate answer.
      3. Verify claims.
      4. If ANY claim is UNSUPPORTED or CONTRADICTED -> discard answer, return abstention.
      5. Otherwise return the answer intact.
    """
    effective_top_k = top_k * _SUMMARY_TOP_K_MULTIPLIER if _is_summary_question(question) else top_k

    evidence = retrieve(question, top_k=effective_top_k, doc_id=doc_id)
    if not evidence:
        empty_metrics = compute_hallucination_metrics([], abstained=True)
        return {
            "answer": ABSTENTION_MESSAGE,
            "abstained": True,
            "claims": [{
                "claim": question,
                "verdict": "ABSTAINED",
                "reason": "No evidence found in the retrieved document.",
                "source_ids": [],
                "quote": "",
            }],
            "sources": [],
            "rounds": 0,
            "hallucination_risk_score": 0,
            "hallucination_percentage": 0.0,
            "metrics": empty_metrics,
        }

    # Pre-generation gate: if numeric value asked for isn't in evidence, abstain
    if (_is_exact_value_request(question) or _is_numeric_relationship_request(question)) \
            and not _evidence_has_explicit_exact_numeric_value(question, evidence):
        abst_metrics = compute_hallucination_metrics([{
            "claim": question,
            "verdict": "ABSTAINED",
            "reason": "The document does not explicitly state the requested numerical value.",
        }], abstained=True)
        return {
            "answer": ABSTENTION_FALLBACK_MESSAGE,
            "abstained": True,
            "claims": [{
                "claim": question,
                "verdict": "ABSTAINED",
                "reason": "The document does not explicitly state the requested numerical value.",
                "source_ids": [],
                "quote": "",
            }],
            "sources": evidence,
            "rounds": 0,
            "hallucination_risk_score": 0,
            "hallucination_percentage": 0.0,
            "metrics": abst_metrics,
        }

    # ── Generate ──────────────────────────────────────────────────────────────
    answer = generate_answer(question, evidence)
    print(f"[GENERATE] {len(answer)} chars: {answer[:120]!r}")

    if not answer or not answer.strip():
        print("[GENERATE] empty — rate limit or LLM error")
        return {
            "answer": ABSTENTION_FALLBACK_MESSAGE,
            "abstained": True,
            "claims": [{
                "claim": question,
                "verdict": "ABSTAINED",
                "reason": "Answer generation returned empty — possible rate limit or LLM error.",
                "source_ids": [],
                "quote": "",
            }],
            "sources": evidence,
            "rounds": 0,
            "hallucination_risk_score": 0,
            "hallucination_percentage": 0.0,
            "metrics": compute_hallucination_metrics([], abstained=True),
        }

    # ── Verify ────────────────────────────────────────────────────────────────
    verdicts = extract_and_verify_claims(answer, evidence, question=question)
    has_bad_claim = any(
        v.get("verdict", "").upper() in ("UNSUPPORTED", "CONTRADICTED")
        for v in verdicts
    )
    if has_bad_claim or not verdicts:
        bad_metrics = compute_hallucination_metrics(verdicts or [], abstained=False)
        return {
            "answer": ABSTENTION_FALLBACK_MESSAGE,
            "abstained": True,
            "claims": verdicts or [{
                "claim": answer,
                "verdict": "UNSUPPORTED",
                "reason": "The answer could not be grounded in the retrieved evidence.",
                "source_ids": [],
                "quote": "",
            }],
            "sources": evidence,
            "rounds": 0,
            "hallucination_risk_score": bad_metrics["hallucination_risk_score"],
            "hallucination_percentage": bad_metrics["hallucination_percentage"],
            "metrics": bad_metrics,
        }

    is_abstained = all(
        v.get("verdict", "").upper() == "ABSTAINED" for v in verdicts
    )
    metrics = compute_hallucination_metrics(verdicts, is_abstained)

    result = {
        "answer": answer,
        "abstained": is_abstained,
        "claims": verdicts,
        "sources": evidence,
        "rounds": 0,
        "hallucination_risk_score": metrics["hallucination_risk_score"],
        "hallucination_percentage": metrics["hallucination_percentage"],
        "metrics": metrics,
    }

    if not result.get("answer") or not result["answer"].strip():
        result["answer"] = "I'm not able to find verified information in the document to answer this question."
        result["abstained"] = True
        result["hallucination_risk_score"] = 0
        result["hallucination_percentage"] = 0.0

    return result
