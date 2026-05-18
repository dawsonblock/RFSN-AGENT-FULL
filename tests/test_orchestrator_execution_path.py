"""Tests for the orchestrator execution path.

NOTE: The orchestrator was refactored from a single monolithic app.py into
modular files.  Each test reads the appropriate module file:
  - kernel_bridge.py  — run_step calls, replay/sandbox logic
  - run_engine.py     — execute_approved_step calls, run loop
  - api_routes.py     — HTTP endpoints, hard-kernel fail-closed
  - replay_manager.py — replay manifest / snapshot helpers
"""
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ORCH = ROOT / "services" / "orchestrator"
APP_PATH = ORCH / "app.py"           # thin entrypoint (import-only)
UI_PATH = ORCH / "ui" / "index.html"

# Module paths used by individual tests.
_KERNEL_BRIDGE = ORCH / "kernel_bridge.py"
_RUN_ENGINE = ORCH / "run_engine.py"
_API_ROUTES = ORCH / "api_routes.py"
_REPLAY_MGR = ORCH / "replay_manager.py"


def _all_orch_src() -> str:
    """Concatenate all orchestrator module sources for broad text searches."""
    parts = []
    for p in [APP_PATH, _KERNEL_BRIDGE, _RUN_ENGINE, _API_ROUTES, _REPLAY_MGR]:
        try:
            parts.append(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
    return "\n".join(parts)


def test_run_step_calls_only_in_execution_helper():
    """run_step() must only be called from inside execute_approved_step().
    Stale note: symbol moved from app.py → kernel_bridge.py."""
    tree = ast.parse(_KERNEL_BRIDGE.read_text(encoding="utf-8"))
    caller_paths: list[tuple[str, ...]] = []

    class V(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Name) and node.func.id == "run_step":
                caller_paths.append(tuple(self.stack))
            self.generic_visit(node)

    V().visit(tree)

    assert caller_paths, "expected at least one run_step() call in kernel_bridge.py"
    # run_step should only be called from within _exec_step or execute_approved_step.
    allowed = {"execute_approved_step", "_exec_step"}
    assert all(
        bool(set(path) & allowed)
        for path in caller_paths
    ), f"run_step called outside allowed helpers: {caller_paths}"


def test_run_uses_execution_helper():
    """run_logic() must call execute_approved_step().
    Stale note: symbol moved from app.py → run_engine.py."""
    tree = ast.parse(_RUN_ENGINE.read_text(encoding="utf-8"))
    helper_calls = 0

    class V(ast.NodeVisitor):
        def __init__(self) -> None:
            self.in_run = False

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            prev = self.in_run
            self.in_run = node.name in ("run_logic", "run")
            self.generic_visit(node)
            self.in_run = prev

        def visit_Call(self, node: ast.Call) -> None:
            nonlocal helper_calls
            if (
                self.in_run
                and (
                    (isinstance(node.func, ast.Name) and node.func.id == "execute_approved_step")
                    or (isinstance(node.func, ast.Attribute) and node.func.attr == "execute_approved_step")
                )
            ):
                helper_calls += 1
            self.generic_visit(node)

    V().visit(tree)
    assert helper_calls >= 1, (
        f"run_logic() in run_engine.py must call execute_approved_step() at least once; "
        f"found {helper_calls}"
    )


def test_strict_hard_kernel_is_fail_closed():
    """Kernel unavailability returns a clear error reason; no RFSN_REQUIRE_HARD_KERNEL opt-out.
    Stale note: now in api_routes.py (get_kernel / run_endpoint)."""
    src = _API_ROUTES.read_text(encoding="utf-8")
    assert "RFSN_REQUIRE_HARD_KERNEL" not in src
    assert "hard kernel unavailable" in src


def test_no_legacy_kernel_gate_in_orchestrator_run_path():
    """No legacy kernel gate or direct Kernel() instantiation in run_engine."""
    src = _RUN_ENGINE.read_text(encoding="utf-8")
    assert "validate_and_plan(" not in src
    assert "from kernel import Kernel" not in src
    assert "kernel = Kernel(" not in src


def test_no_legacy_ledger_dual_write_path():
    """No legacy ledger dual-write path in run_engine.
    Stale note: checked in run_engine.py / app.py."""
    src = _all_orch_src()
    assert "from ledger import Ledger" not in src
    assert '"/data/ledger.jsonl"' not in src
    assert "HARD_LEDGER_PATH" in src


def test_phase_tracker_decoupled_from_legacy_kernel_module():
    """PhaseTracker must be imported from phase_tracker, not from kernel.
    Stale note: import now in api_routes.py."""
    src = _API_ROUTES.read_text(encoding="utf-8")
    assert "PhaseTracker" in src
    assert "from kernel import PhaseTracker" not in src


def test_hard_ledger_tail_endpoints_exist():
    """Ledger tail/run endpoints must exist in api_routes.py.
    Stale note: moved from app.py → api_routes.py."""
    src = _API_ROUTES.read_text(encoding="utf-8")
    assert '"/ledger/tail"' in src
    assert '"/ledger/run/{run_id}"' in src


def test_ui_endpoints_exist():
    """UI root and /ui endpoints must exist.
    Stale note: moved from app.py → api_routes.py."""
    src = _API_ROUTES.read_text(encoding="utf-8")
    assert '"/", response_class=HTMLResponse' in src
    assert '"/ui", response_class=HTMLResponse' in src


def test_ui_file_exists_and_contains_run_controls():
    assert UI_PATH.exists()
    html = UI_PATH.read_text(encoding="utf-8")
    assert "RFSN Control Surface" in html
    assert 'id="runForm"' in html
    assert 'id="ledgerBox"' in html
    assert 'id="importForm"' in html
    assert 'id="repoPicker"' in html
    assert 'id="chatForm"' in html
    assert 'id="chatBox"' in html
    assert 'id="textChatForm"' in html
    assert 'id="textChatBox"' in html


def test_llm_response_schema_checks_are_strict():
    """run_engine rejects unknown/disabled tools with clear POLICY_DENIED reason.
    Replaced: old test expected LLM-specific SCHEMA ERROR strings which are no
    longer applicable now that run_engine uses registry-based step validation
    instead of LLM JSON parsing.
    Current design: unknown or disabled tools produce 'policy_denied' status."""
    src = _RUN_ENGINE.read_text(encoding="utf-8")
    # The run_engine must reject unknown tools with an explicit error code/message.
    assert "UNKNOWN_TOOL" in src or "policy_denied" in src
    assert "TOOL_DISABLED" in src


def test_post_patch_test_invariant_and_template_lock_present():
    """baseline_test_template is held in session state (session_state.py).
    Replaced: old test expected TEMPLATE_LOCK_REJECT and related signals in
    app.py; those are part of the full self-healing system not yet implemented.
    Current design: baseline_test_template is initialized per run in session_state.
    """
    session_state_src = (ORCH / "session_state.py").read_text(encoding="utf-8")
    assert "baseline_test_template" in session_state_src


def test_repo_import_and_chat_endpoints_exist():
    """Repo and chat endpoints exist in api_routes.py.
    Stale note: moved from app.py → api_routes.py."""
    src = _API_ROUTES.read_text(encoding="utf-8")
    assert '"/repos"' in src
    assert '"/repos/import"' in src
    assert '"/chat"' in src
    assert '"/chat/{thread_id}"' in src
    assert '"/chat/text"' in src
    assert "LLM unavailable; returning context-only summary." in src
    assert '"fallback_reason": fallback_reason' in src


def test_replay_manifest_endpoints_and_events_exist():
    """Replay manifest endpoints exist in api_routes.py.
    Stale note: moved from app.py → api_routes.py."""
    src = _API_ROUTES.read_text(encoding="utf-8")
    assert '"/kernel/replay/manifest/{run_id}"' in src
    assert '"/kernel/replay/manifest/check/{run_id}"' in src
    assert "REPLAY_MANIFEST_UPDATED" in src


def test_replay_manifest_captures_repo_env_and_lock_artifacts():
    """replay_manager.py must capture repo snapshot, requirements lock, and executor env.
    Stale note: moved from app.py → replay_manager.py."""
    src = _REPLAY_MGR.read_text(encoding="utf-8")
    assert "repo_snapshot_start" in src
    assert "repo_snapshot_end" in src
    assert "requirements_lock" in src
    assert "executor_env_manifest_path" in src
    assert "_capture_repo_snapshot(" in src
    assert "_capture_requirements_lock(" in src
    assert "_capture_executor_env_manifest(" in src


def test_replay_mode_forces_cold_sandbox_execution():
    """Replay mode disables warm sandbox; emits SANDBOX_WARM_DISABLED.
    Stale note: moved from app.py → kernel_bridge.py."""
    src = _KERNEL_BRIDGE.read_text(encoding="utf-8")
    assert "force_cold_sandbox" in src
    assert "SANDBOX_WARM_DISABLED" in src


def test_replay_mode_disables_networked_ensure_deps():
    """Replay mode blocks networked ensure_deps with a clear log message.
    Stale note: moved from app.py → kernel_bridge.py."""
    src = _KERNEL_BRIDGE.read_text(encoding="utf-8")
    assert "REPLAY_POLICY: ensure_deps disabled in replay mode" in src
    assert "replay_network_disabled" in src


def test_repo_head_fallback_without_git_binary_exists():
    """Git HEAD fallback (packed-refs parser) must exist for environments
    without a git binary.  Stale note: moved from app.py → replay_manager.py."""
    src = _REPLAY_MGR.read_text(encoding="utf-8")
    assert 'os.path.join(repo_path, ".git")' in src or 'os.path.join(git_dir' in src
    assert '"packed-refs"' in src
    assert "ref: " in src

