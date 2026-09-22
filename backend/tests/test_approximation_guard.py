import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.claim_verifier import _is_correct_abstention, _looks_like_derived_numeric_claim


def test_approximation_words_do_not_make_claim_supported():
    evidence = [{"text": "The system performance improved over time."}]
    claim = "The system improved by about 80%."
    assert _looks_like_derived_numeric_claim(claim, evidence) is True


def test_exact_numeric_claim_is_supported_only_when_explicit():
    evidence = [{"text": "The system improved by exactly 20%."}]
    claim = "The system improved by 20%."
    assert _looks_like_derived_numeric_claim(claim, evidence) is False


def test_missing_exact_value_is_rejected():
    evidence = [{"text": "The report describes a major improvement in performance."}]
    claim = "The exact percentage improvement was roughly 75%."
    assert _looks_like_derived_numeric_claim(claim, evidence) is True


def test_pure_abstention_is_recognized():
    claim = "The document does not explicitly provide this information."
    assert _is_correct_abstention(claim) is True


def test_mixed_abstention_plus_unsupported_numeric_claim_is_not_a_correct_abstention():
    claim = "The document does not explicitly provide this information, and the CPU utilization was 80%."
    assert _is_correct_abstention(claim) is False
