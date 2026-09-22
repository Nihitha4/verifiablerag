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
                "You are a strict document-grounded QA system. Your #1 priority is to NEVER state a number, percentage, statistic, or before/after relationship that is not written verbatim or near-verbatim in the passages. This rule overrides helpfulness and completeness.\n\n"
                "PROCEDURE — follow every step in order:\n"
                "1. Break the user's question into its individual parts.\n"
                "2. For each part, search the passages for a sentence that explicitly states the answer. 'Explicit' means that exact fact, not a calculation, combination, or inference from two separate numbers.\n"
                "3. For each part, write ONE of only two things:\n"
                "   a) The fact copied faithfully from the passage, OR\n"
                "   b) The exact sentence: 'The document does not state [that specific thing].'\n"
                "   There is no third option. If you are not 100% certain a passage states the exact number or relationship, use option (b).\n"
                "4. SPECIAL RULE FOR PERCENTAGES/STATISTICS: If the question asks for a percentage, improvement, or before/after change, and the passages only contain a general range or unrelated numbers, that is not sufficient. Treat it as unanswerable and use option (b). Do not construct a percentage that is not explicitly written.\n"
                "5. FINAL SELF-CHECK: before outputting anything, re-read every sentence you are about to give. For each one, ask 'Can I point to the exact passage that says this exact thing?' If the answer is no for any sentence, delete it and replace it with 'The document does not state [that specific thing].'\n"
                "6. Multi-part questions: answer each part independently. An unanswerable part does NOT mean you should abstain on parts that are answerable.\n\n"
                "If a numeric claim is missing or unsupported, do not paraphrase it or soften it with 'roughly' or 'approximately'; that still counts as an unsupported claim.\n"
                "Never state percentages or before/after relationships unless the passages write them explicitly.\n"
                "If the answer cannot be grounded in the passages, use: 'The document does not state this.'"
            ),
        },
        {
            "role": "user",
            "content": f"Context:\n{context}\n\nQuestion: {question}\n\nAnswer:",
        },
    ]
    answer_max_tokens = int(os.getenv("LLM_ANSWER_MAX_TOKENS", "800"))
    result = chat(messages, temperature=0.2, max_tokens=answer_max_tokens)
    # Never return empty — an empty string propagates silently and produces 0-claim results.
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


def extract_and_verify_claims(answer: str, evidence_chunks: list[dict]) -> list[dict]:
    """Split answer into claims and verify each against evidence in one LLM call."""
    answer = _strip_unverified_numeric_sentences(answer, evidence_chunks)
    context = _format_evidence(evidence_chunks)
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


def compute_hallucination_risk_score(verdicts: list[dict], abstained: bool) -> int:
    """
    Return an integer 0–100 representing hallucination risk.

    This is derived from the claim counters only; no independent free-form scoring.
    Any asserted unsupported or contradicted claim is treated as high risk.
    A pure correct abstention remains 0 risk.
    """
    if abstained:
        return 0
    if not verdicts:
        return 0
    if any(
        v.get("verdict", "").upper() in ("UNSUPPORTED", "CONTRADICTED")
        for v in verdicts
    ):
        return 100
    total = sum(
        1 for v in verdicts if v.get("verdict", "").upper() in ("SUPPORTED", "UNSUPPORTED", "CONTRADICTED", "ABSTAINED")
    )
    if total == 0:
        return 0
    non_supported = sum(
        1 for v in verdicts
        if v.get("verdict", "").upper() in ("UNSUPPORTED", "CONTRADICTED")
    )
    return round(100 * non_supported / total)


def answer_with_verification(question: str, doc_id: str | None, top_k: int = 3) -> dict:
    """
    Simplified all-or-nothing pipeline:
      1. Retrieve evidence.
      2. Generate answer.
      3. Verify claims.
      4. If ANY claim is UNSUPPORTED or CONTRADICTED → discard answer, return abstention.
      5. Otherwise return the answer intact.

    No sentence-level surgery. The answer is either shown whole or not at all.
    This guarantees zero dangling fragments and zero hallucinated numbers.
    """
    effective_top_k = top_k * _SUMMARY_TOP_K_MULTIPLIER if _is_summary_question(question) else top_k

    evidence = retrieve(question, top_k=effective_top_k, doc_id=doc_id)
    if not evidence:
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
        }

    # Pre-generation gate: if the question itself asks for a numeric value
    # that isn't in the evidence, abstain immediately without generating.
    if (_is_exact_value_request(question) or _is_numeric_relationship_request(question)) \
            and not _evidence_has_explicit_exact_numeric_value(question, evidence):
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
        }

    # ── Verify ────────────────────────────────────────────────────────────────
    verdicts = extract_and_verify_claims(answer, evidence)
    print(f"[VERIFIER] question={question[:80]!r}")
    for v in verdicts:
        print(f"  verdict={v.get('verdict'):12s}  claim={v.get('claim','')[:70]!r}")

    has_bad = any(
        v.get("verdict", "").upper() in ("UNSUPPORTED", "CONTRADICTED")
        for v in verdicts
    )

    if has_bad:
        print("[VERIFIER] bad claim found — abstaining (all-or-nothing)")
        return {
            "answer": ABSTENTION_FALLBACK_MESSAGE,
            "abstained": True,
            "claims": [{
                "claim": question,
                "verdict": "ABSTAINED",
                "reason": "The answer contained an unsupported claim; the system abstains rather than show partial or hallucinated content.",
                "source_ids": [],
                "quote": "",
            }],
            "sources": evidence,
            "rounds": 0,
            "hallucination_risk_score": 0,
        }

    # All claims SUPPORTED or ABSTAINED — safe to show.
    print("[VERIFIER] all clean — returning answer")
    result = {
        "answer": answer,
        "abstained": False,
        "claims": verdicts,
        "sources": evidence,
        "rounds": 0,
        "hallucination_risk_score": compute_hallucination_risk_score(verdicts, False),
    }

    # Hard fallback: physically impossible to return an empty answer box.
    if not result.get("answer") or not result["answer"].strip():
        result["answer"] = "I'm not able to find verified information in the document to answer this question."
        result["abstained"] = True
        result["hallucination_risk_score"] = 0

    return result
