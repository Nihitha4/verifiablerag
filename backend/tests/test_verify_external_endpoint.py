import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from app import app

client = TestClient(app)

def test_health_check():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_verify_external_with_pasted_reference():
    answer_text = "The speed of light in vacuum is exactly 299,792,458 meters per second. The Earth has three moons."
    ref_text = "The speed of light in vacuum is defined as exactly 299,792,458 metres per second. Earth has only one natural satellite, the Moon."

    res = client.post("/verify-external", json={
        "answer": answer_text,
        "question": "What is the speed of light and how many moons does Earth have?",
        "reference_text": ref_text
    })

    assert res.status_code == 200
    data = res.json()
    assert "hallucination_percentage" in data
    assert "hallucination_risk_score" in data
    assert "metrics" in data
    assert "sentences" in data
    assert "claims" in data
    assert data["metrics"]["total"] > 0
    # There should be hallucinated claims (the three moons claim)
    assert data["hallucination_percentage"] > 0


def test_verify_external_empty_answer():
    res = client.post("/verify-external", json={
        "answer": "",
        "reference_text": "Some reference."
    })
    assert res.status_code == 400


if __name__ == "__main__":
    test_health_check()
    test_verify_external_with_pasted_reference()
    test_verify_external_empty_answer()
    print("All endpoint tests passed!")
