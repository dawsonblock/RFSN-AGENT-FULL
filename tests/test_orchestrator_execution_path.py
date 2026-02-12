import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "services" / "orchestrator" / "app.py"
UI_PATH = ROOT / "services" / "orchestrator" / "ui" / "index.html"


def _parse_app() -> ast.Module:
    return ast.parse(
        APP_PATH.read_text(encoding="utf-8"),
    )


def test_run_step_calls_only_in_execution_helper():
    tree = _parse_app()
    caller_paths: list[tuple[str, ...]] = []

    class V(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_FunctionDef(
            self, node: ast.FunctionDef,
        ) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_Call(self, node: ast.Call) -> None:
                if (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "run_step"
                ):
                    caller_paths.append(tuple(self.stack))
                self.generic_visit(node)

    V().visit(tree)

    # Keep one narrow choke point:
    # execute_approved_step() (and its local helpers)
    # are allowed to call run_step().
    assert caller_paths, "expected at least one run_step() call"
    assert all(
        "execute_approved_step" in path
        for path in caller_paths
    )


def test_run_uses_execution_helper():
    tree = _parse_app()
    helper_calls = 0

    class V(ast.NodeVisitor):
        def __init__(self) -> None:
            self.in_run = False

        def visit_FunctionDef(
            self, node: ast.FunctionDef,
        ) -> None:
            prev = self.in_run
            self.in_run = node.name == "run"
            self.generic_visit(node)
            self.in_run = prev

        def visit_Call(self, node: ast.Call) -> None:
            nonlocal helper_calls
            if (
                self.in_run
                and isinstance(node.func, ast.Name)
                and node.func.id
                == "execute_approved_step"
            ):
                helper_calls += 1
            self.generic_visit(node)

    V().visit(tree)
    assert helper_calls >= 3


def test_strict_hard_kernel_is_fail_closed():
    src = APP_PATH.read_text(encoding="utf-8")
    assert "RFSN_REQUIRE_HARD_KERNEL" not in src
    assert '"reason": "hard kernel unavailable"' in src


def test_no_legacy_kernel_gate_in_orchestrator_run_path():
    src = APP_PATH.read_text(encoding="utf-8")
    assert "validate_and_plan(" not in src
    assert "from kernel import Kernel" not in src
    assert "kernel = Kernel(" not in src


def test_no_legacy_ledger_dual_write_path():
    src = APP_PATH.read_text(encoding="utf-8")
    assert "from ledger import Ledger" not in src
    assert '"/data/ledger.jsonl"' not in src
    assert "HARD_LEDGER_PATH" in src


def test_phase_tracker_decoupled_from_legacy_kernel_module():
    src = APP_PATH.read_text(encoding="utf-8")
    assert "from phase_tracker import PhaseTracker" in src
    assert "from kernel import PhaseTracker" not in src


def test_hard_ledger_tail_endpoints_exist():
    src = APP_PATH.read_text(encoding="utf-8")
    assert '@app.get("/ledger/tail")' in src
    assert '@app.get("/ledger/run/{run_id}")' in src


def test_ui_endpoints_exist():
    src = APP_PATH.read_text(encoding="utf-8")
    assert '@app.get("/", response_class=HTMLResponse)' in src
    assert '@app.get("/ui", response_class=HTMLResponse)' in src


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
    src = APP_PATH.read_text(encoding="utf-8")
    assert "SCHEMA ERROR: 'done'" in src
    assert "SCHEMA ERROR: 'intent'" in src
    assert "done=true, step must" in src


def test_repo_import_and_chat_endpoints_exist():
    src = APP_PATH.read_text(encoding="utf-8")
    assert '@app.get("/repos")' in src
    assert '@app.post("/repos/import")' in src
    assert '@app.post("/chat")' in src
    assert '@app.get("/chat/{thread_id}")' in src
    assert '@app.delete("/chat/{thread_id}")' in src
    assert '@app.post("/chat/text")' in src
    assert '@app.get("/chat/text/{thread_id}")' in src
    assert '@app.delete("/chat/text/{thread_id}")' in src
