from pathlib import Path

from rfsn_kernel.failure_kinds import extract_failure_kinds


ROOT = Path(__file__).resolve().parents[1]
GW_APP = ROOT / "services" / "tool_gateway" / "app.py"
EXEC_APP = ROOT / "services" / "executor" / "app.py"
GATE_POLICY = ROOT / "policies" / "gate_policy.yaml"


def test_gate_policy_declares_network_min_tier():
    src = GATE_POLICY.read_text(encoding="utf-8")
    assert "network_min_tier:" in src


def test_gateway_validates_run_tests_template_id():
    src = GW_APP.read_text(encoding="utf-8")
    assert "RUN_TEST_TEMPLATES" in src
    assert "run_tests template_id required" in src
    assert "unknown run_tests template_id" in src


def test_gateway_tier_gates_network_for_ensure_deps():
    src = GW_APP.read_text(encoding="utf-8")
    assert "NETWORK_MIN_TIER" in src
    assert "ensure_deps requires network tier" in src
    assert "s[\"_rfsn_tier\"]" in src
    assert "s[\"_rfsn_allow_network\"]" in src
    assert "s[\"_rfsn_network_reason\"]" in src


def test_gateway_fails_fast_on_template_registry_drift():
    src = GW_APP.read_text(encoding="utf-8")
    assert "Template registry drift detected" in src
    assert "_check_template_registry_drift()" in src


def test_executor_enforces_network_tier_for_ensure_deps():
    src = EXEC_APP.read_text(encoding="utf-8")
    assert "def _require_network_tier(" in src
    assert "networked ensure_deps requires tier" in src
    assert "_require_network_tier(step)" in src


def test_missing_venv_failure_escalates_to_deps_kind():
    kinds = extract_failure_kinds(
        {
            "status": 39,
            "logs": "Missing venv; ensure_deps first",
        }
    )
    assert "deps_install_failed" in kinds
