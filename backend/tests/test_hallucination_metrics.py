import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.claim_verifier import (
    compute_hallucination_metrics,
    compute_hallucination_risk_score,
    verify_external_llm_answer,
)


def test_compute_hallucination_metrics_all_supported():
    verdicts = [
        {"claim": "Fact 1", "verdict": "SUPPORTED"},
        {"claim": "Fact 2", "verdict": "SUPPORTED"},
        {"claim": "Fact 3", "verdict": "SUPPORTED"},
    ]
    metrics = compute_hallucination_metrics(verdicts)
    assert metrics["total"] == 3
    assert metrics["supported"] == 3
    assert metrics["unsupported"] == 0
    assert metrics["contradicted"] == 0
    assert metrics["hallucination_count"] == 0
    assert metrics["hallucination_percentage"] == 0.0
    assert metrics["supported_percentage"] == 100.0
    assert metrics["risk_level"] == "None"
    assert compute_hallucination_risk_score(verdicts) == 0


def test_compute_hallucination_metrics_mixed_hallucination():
    verdicts = [
        {"claim": "Fact 1", "verdict": "SUPPORTED"},
        {"claim": "Fact 2", "verdict": "UNSUPPORTED"},
        {"claim": "Fact 3", "verdict": "SUPPORTED"},
        {"claim": "Fact 4", "verdict": "CONTRADICTED"},
    ]
    metrics = compute_hallucination_metrics(verdicts)
    assert metrics["total"] == 4
    assert metrics["supported"] == 2
    assert metrics["unsupported"] == 1
    assert metrics["contradicted"] == 1
    assert metrics["hallucination_count"] == 2
    # 2 / 4 = 50.0%
    assert metrics["hallucination_percentage"] == 50.0
    assert metrics["supported_percentage"] == 50.0
    assert metrics["unsupported_percentage"] == 25.0
    assert metrics["contradicted_percentage"] == 25.0
    assert metrics["risk_level"] == "Moderate"
    assert compute_hallucination_risk_score(verdicts) == 50


def test_compute_hallucination_metrics_high_risk():
    verdicts = [
        {"claim": "Fact 1", "verdict": "UNSUPPORTED"},
        {"claim": "Fact 2", "verdict": "UNSUPPORTED"},
        {"claim": "Fact 3", "verdict": "CONTRADICTED"},
        {"claim": "Fact 4", "verdict": "SUPPORTED"},
    ]
    # 3 / 4 = 75.0%
    metrics = compute_hallucination_metrics(verdicts)
    assert metrics["hallucination_percentage"] == 75.0
    assert metrics["risk_level"] == "High"


def test_verify_external_llm_answer_no_evidence():
    answer = "The system has 99.9% uptime and was built in 2024."
    res = verify_external_llm_answer(answer, evidence_chunks=[])
    assert res["hallucination_percentage"] == 100.0
    assert res["risk_level"] == "Critical"
    assert len(res["sentences"]) > 0
    assert res["sentences"][0]["verdict"] == "UNSUPPORTED"


def test_verify_external_llm_answer_lexical_grounded():
    evidence = [
        {"text": "Project Apollo achieved lunar landing in 1969 with three astronauts aboard the spacecraft."}
    ]
    answer = "Project Apollo achieved lunar landing in 1969 with three astronauts aboard the spacecraft."
    res = verify_external_llm_answer(answer, evidence_chunks=evidence)
    assert "metrics" in res
    assert "sentences" in res
    assert res["metrics"]["total"] > 0
    assert res["hallucination_percentage"] <= 20.0


if __name__ == "__main__":
    test_compute_hallucination_metrics_all_supported()
    test_compute_hallucination_metrics_mixed_hallucination()
    test_compute_hallucination_metrics_high_risk()
    test_verify_external_llm_answer_no_evidence()
    test_verify_external_llm_answer_lexical_grounded()
    print("All hallucination metrics tests passed!")
