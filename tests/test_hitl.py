import pytest
from unittest.mock import MagicMock, patch
from services.orchestrator.run_engine import run_logic, RunReq
from services.orchestrator.session_state import get_run_context, ensure_run_context
from services.orchestrator.kernel_bridge import LedgerSink
from rfsn_kernel.kernel import HardKernel


@pytest.fixture
def mock_kernel():
    k = MagicMock(spec=HardKernel)
    mock_res = MagicMock()
    mock_res.approved = True
    mock_res.success = True
    mock_res.outcome.payload = '{"status": 0}'
    mock_res.to_dict.return_value = {"approved": True}
    k.kernel_step.return_value = mock_res
    k.ledger = MagicMock()

    # Mock state
    k.state = MagicMock()
    k.state.resource_state = {}

    return k


def test_hitl_pause_and_approve(mock_kernel):
    run_id = "test-run-hitl"
    req = RunReq(
        repo_id="test-repo", task="test task", max_iters=3, confidence_threshold=0.7
    )
    ledger = LedgerSink(mock_kernel)

    with patch("services.orchestrator.run_engine.sandbox_create") as mock_sc, patch(
        "services.orchestrator.run_engine.sandbox_destroy"
    ):
        mock_sc.return_value = {"id": "mock-sandbox", "ip": "127.0.0.1"}

        import threading
        import time

        # Start a thread to approve it after 0.2 second if it reaches pause
        def approver():
            for _ in range(20):
                time.sleep(0.1)
                ctx = get_run_context(run_id)
                if ctx and ctx.get("status") == "paused":
                    ctx["approval_result"] = "approved"
                    ctx["approval_event"].set()
                    return

        t_app = threading.Thread(target=approver, daemon=True)
        t_app.start()

        run_logic(run_id, req, mock_kernel, ledger)

        found_pause = False
        found_approve = False

        for call in mock_kernel.ledger.append.call_args_list:
            rec = call[0][0]  # LedgerRecord
            typ = rec.metadata.get("type")
            if typ == "HITL_PAUSE":
                found_pause = True
            if typ == "HITL_APPROVE":
                found_approve = True

        assert found_pause, "Did not find HITL_PAUSE event"
        assert found_approve, "Did not find HITL_APPROVE event"


def test_hitl_pause_and_reject(mock_kernel):
    run_id = "test-run-reject"
    req = RunReq(
        repo_id="test-repo", task="test task", max_iters=3, confidence_threshold=0.7
    )
    ledger = LedgerSink(mock_kernel)

    with patch("services.orchestrator.run_engine.sandbox_create") as mock_sc, patch(
        "services.orchestrator.run_engine.sandbox_destroy"
    ):
        mock_sc.return_value = {"id": "mock-sandbox", "ip": "127.0.0.1"}

        import threading
        import time

        def rejector():
            for _ in range(20):
                time.sleep(0.1)
                ctx = get_run_context(run_id)
                if ctx and ctx.get("status") == "paused":
                    ctx["approval_result"] = "rejected"
                    ctx["approval_event"].set()
                    return

        t_rej = threading.Thread(target=rejector, daemon=True)
        t_rej.start()

        res = run_logic(run_id, req, mock_kernel, ledger)

        found_pause = False
        found_reject = False

        for call in mock_kernel.ledger.append.call_args_list:
            rec = call[0][0]
            typ = rec.metadata.get("type")
            if typ == "HITL_PAUSE":
                found_pause = True
            if typ == "HITL_REJECT":
                found_reject = True

        assert found_pause
        assert found_reject
        assert res["status"] == "aborted"
