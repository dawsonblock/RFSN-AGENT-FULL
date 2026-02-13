from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEARNER_APP = ROOT / "services" / "learner_service" / "app.py"
LEARNER_STORE = ROOT / "services" / "learner_service" / "store_duckdb.py"


def test_learner_ingest_reject_guards_present():
    src = LEARNER_APP.read_text(encoding="utf-8")
    assert "duplicate_patch_fingerprint" in src
    assert "tests_reduced" in src
    assert "failures_increased" in src
    assert "forbidden_patch_area" in src
    assert "_should_reject_ingest" in src
    assert "\"patch_fingerprint\": req.patch_hash or \"\"" in src


def test_learner_store_supports_latest_outcome_lookup():
    src = LEARNER_STORE.read_text(encoding="utf-8")
    assert "def latest_outcome(" in src
    assert "ORDER BY ts DESC" in src
    assert "tests_failed" in src
    assert "tests_total" in src
