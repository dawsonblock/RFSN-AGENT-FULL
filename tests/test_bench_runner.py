"""Tests for rfsn_swebench.runner — bench_run loop with mock proposer."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap

import pytest  # type: ignore[import-not-found]

from rfsn_swebench.contracts import (
    BenchTask,
    TaskCommands,
    TaskLimits,
)
from rfsn_swebench.runner import _tail, _parse_test_cmd, bench_run, run_tests


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _has_git() -> bool:
    """Check whether git is available on this system."""
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


needs_git = pytest.mark.skipif(not _has_git(), reason="git not available")


def _init_repo(path: str, files: dict[str, str] | None = None) -> None:
    """Initialise a git repo at *path* with optional files committed."""
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test"],
        cwd=path, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=path, capture_output=True, check=True,
    )
    for name, content in (files or {}).items():
        fpath = os.path.join(path, name)
        os.makedirs(os.path.dirname(fpath), exist_ok=True)
        with open(fpath, "w") as f:
            f.write(content)
    subprocess.run(
        ["git", "add", "."],
        cwd=path, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "init", "--allow-empty"],
        cwd=path, capture_output=True, check=True,
    )


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

def test_tail_short():
    assert _tail("hello", 100) == "hello"


def test_tail_long():
    s = "x" * 5000
    assert len(_tail(s, 4000)) == 4000
    assert _tail(s, 4000) == s[-4000:]


def test_parse_test_cmd_targeted():
    tid, target = _parse_test_cmd("pytest -q tests/test_a.py::TestX::test_y")
    assert tid == "pytest_targeted"
    assert "test_a.py::TestX::test_y" in target


def test_parse_test_cmd_suite():
    tid, target = _parse_test_cmd("pytest -q")
    assert tid == "pytest_suite"
    assert target == ""


def test_parse_test_cmd_file_only():
    tid, target = _parse_test_cmd("pytest tests/test_foo.py")
    assert tid == "pytest_targeted"
    assert "test_foo.py" in target


def test_run_tests_echo():
    """run_tests should capture stdout from a simple echo."""
    tr = run_tests("echo hello_world", tempfile.gettempdir(), timeout=10)
    assert tr.exit_code == 0
    assert "hello_world" in tr.stdout_tail


def test_run_tests_exit_code():
    tr = run_tests(
        "python3 -c 'import sys; sys.exit(42)'",
        tempfile.gettempdir(), timeout=10,
    )
    assert tr.exit_code == 42


# ---------------------------------------------------------------------------
# Integration tests (require git)
# ---------------------------------------------------------------------------

@needs_git
def test_bench_run_pass_with_mock_proposer():
    """Mock proposer returns a valid patch → quick + full pass → PASS."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = os.path.join(tmpdir, "repo")
        _init_repo(workdir, {
            "src/demo.py": "def add(a, b):\n    return a + b + 1\n",
            "tests/test_demo.py": textwrap.dedent("""\
                from src.demo import add
                def test_add():
                    assert add(1, 2) == 3
            """),
        })

        # The "fix" patch
        fix_patch = textwrap.dedent("""\
            diff --git a/src/demo.py b/src/demo.py
            --- a/src/demo.py
            +++ b/src/demo.py
            @@ -1,2 +1,2 @@
             def add(a, b):
            -    return a + b + 1
            +    return a + b
        """)

        call_count = 0

        def mock_proposer(task, replay_dir):
            nonlocal call_count
            call_count += 1
            return fix_patch

        task = BenchTask(
            task_id="test-pass",
            repo_url="unused",  # already cloned
            workdir=workdir,
            issue_text="add returns wrong result",
            commands=TaskCommands(
                test_quick=f"python -m pytest {workdir}/tests -q",
                test_full=f"python -m pytest {workdir}/tests -q",
            ),
            limits=TaskLimits(max_iters=3, max_runtime_sec=60),
        )

        result = bench_run(task, mock_proposer, replay_base=tmpdir)

        assert result.status == "PASS"
        assert result.iters >= 1
        assert call_count >= 1
        assert "quick" in result.tests
        assert "full" in result.tests
        assert result.tests["quick"].exit_code == 0
        assert result.tests["full"].exit_code == 0
        assert result.risk.decision == "ALLOW"
        assert os.path.isdir(result.replay_dir)

        # Check replay artifacts
        events_path = os.path.join(result.replay_dir, "events.jsonl")
        assert os.path.isfile(events_path)


@needs_git
def test_bench_run_fail_bad_proposer():
    """Proposer always returns empty patch → all iters skip → FAIL."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = os.path.join(tmpdir, "repo")
        _init_repo(workdir, {"f.py": "x = 1\n"})

        def bad_proposer(task, replay_dir):
            return ""  # empty patch

        task = BenchTask(
            task_id="test-fail",
            repo_url="unused",
            workdir=workdir,
            issue_text="broken",
            commands=TaskCommands(
                test_quick="python3 -c 'import sys; sys.exit(1)'",
                test_full="python3 -c 'import sys; sys.exit(1)'",
            ),
            limits=TaskLimits(max_iters=2, max_runtime_sec=30),
        )

        result = bench_run(task, bad_proposer, replay_base=tmpdir)
        assert result.status == "FAIL"
        assert result.iters == 2


@needs_git
def test_bench_run_abort_on_proposer_exception():
    """Proposer raises every time → all iters skip → FAIL."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = os.path.join(tmpdir, "repo")
        _init_repo(workdir, {"f.py": "x = 1\n"})

        def exploding_proposer(task, replay_dir):
            raise RuntimeError("LLM is down")

        task = BenchTask(
            task_id="test-abort",
            repo_url="unused",
            workdir=workdir,
            issue_text="broken",
            limits=TaskLimits(max_iters=2, max_runtime_sec=30),
        )

        result = bench_run(task, exploding_proposer, replay_base=tmpdir)
        # All iters exhausted via continue → FAIL
        assert result.status == "FAIL"


@needs_git
def test_bench_run_gate_rejects_ci_patch():
    """Proposer returns a patch that touches CI → gate rejects → FAIL."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = os.path.join(tmpdir, "repo")
        _init_repo(workdir, {"src/x.py": "x=1\n"})

        ci_patch = textwrap.dedent("""\
            diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
            new file mode 100644
            --- /dev/null
            +++ b/.github/workflows/ci.yml
            @@ -0,0 +1 @@
            +name: CI
        """)

        def ci_proposer(task, replay_dir):
            return ci_patch

        task = BenchTask(
            task_id="test-gate",
            repo_url="unused",
            workdir=workdir,
            issue_text="broken",
            limits=TaskLimits(max_iters=2, max_runtime_sec=30),
        )

        result = bench_run(task, ci_proposer, replay_base=tmpdir)
        assert result.status == "FAIL"


# ---------------------------------------------------------------------------
# Test CLI load_task with schema validation
# ---------------------------------------------------------------------------

def test_load_task_valid():
    """load_task successfully parses a valid task.json."""
    from rfsn_swebench.cli import load_task

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False,
    ) as f:
        json.dump(
            {
                "task_id": "t-cli",
                "repo_url": "https://github.com/x/y.git",
                "workdir": "/tmp/w",
                "issue_text": "fix it",
                "hints": {"failing_tests": ["tests/test_a.py::test_b"]},
                "commands": {
                    "setup": ["pip install -e ."],
                    "test_quick": "pytest -x",
                },
                "limits": {"max_iters": 4},
            },
            f,
        )
        f.flush()
        task = load_task(f.name)

    assert task.task_id == "t-cli"
    assert task.hints.failing_tests == ["tests/test_a.py::test_b"]
    assert task.limits.max_iters == 4
    os.unlink(f.name)


def test_load_task_invalid_schema():
    """load_task rejects bad types (if jsonschema available)."""
    try:
        import jsonschema  # type: ignore[import-untyped]  # noqa: F401
    except ImportError:
        pytest.skip("jsonschema not installed")

    from rfsn_swebench.cli import load_task

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False,
    ) as f:
        json.dump(
            {
                "task_id": 123,  # wrong type
                "repo_url": "x",
                "workdir": "/tmp/w",
                "issue_text": "y",
            },
            f,
        )
        f.flush()
        with pytest.raises(Exception):  # jsonschema.ValidationError
            load_task(f.name)
    os.unlink(f.name)


# ---------------------------------------------------------------------------
# testsel
# ---------------------------------------------------------------------------

def test_choose_quick_tests_with_hints():
    from rfsn_swebench.testsel import choose_quick_tests
    from rfsn_swebench.contracts import TaskHints

    hints = TaskHints(
        failing_tests=[
            "tests/test_a.py::test_b",
            "tests/test_c.py",
        ],
    )
    cmd = choose_quick_tests(hints, "pytest -q")
    assert "tests/test_a.py::test_b" in cmd
    assert "tests/test_c.py" in cmd
    assert cmd.startswith("pytest -q")


def test_choose_quick_tests_no_hints():
    from rfsn_swebench.testsel import choose_quick_tests
    from rfsn_swebench.contracts import TaskHints  # noqa: F811

    hints = TaskHints()
    cmd = choose_quick_tests(hints, "pytest -q")
    assert cmd == "pytest -q"
