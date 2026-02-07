"""Tests for services/orchestrator/context_fingerprint.py."""
import os

import pytest

# The module under test lives in services/orchestrator/
import sys
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..", "services", "orchestrator",
    ),
)
from context_fingerprint import (  # noqa: E402  # type: ignore[import-not-found]
    detect_framework,
    detect_tests,
    detect_lang,
    failure_class,
    build_context,
)


@pytest.fixture()
def tmp_repo(tmp_path):
    """Create a minimal fake repo directory."""
    return tmp_path


# ── detect_framework ─────────────────────────


def test_detect_framework_from_requirements(tmp_repo):
    (tmp_repo / "requirements.txt").write_text(
        "django>=3.2\ncelery\n"
    )
    assert detect_framework(str(tmp_repo)) == "django"


def test_detect_framework_from_pyproject(tmp_repo):
    (tmp_repo / "pyproject.toml").write_text(
        '[project]\ndependencies = ["flask>=2.0"]\n'
    )
    assert detect_framework(str(tmp_repo)) == "flask"


def test_detect_framework_sympy(tmp_repo):
    (tmp_repo / "setup.py").write_text(
        "install_requires=['sympy']\n"
    )
    assert detect_framework(str(tmp_repo)) == "sympy"


def test_detect_framework_unknown_when_empty(
    tmp_repo,
):
    assert detect_framework(str(tmp_repo)) == "unknown"


def test_detect_framework_nonexistent_path():
    assert (
        detect_framework("/no/such/path/xyz")
        == "unknown"
    )


# ── detect_tests ─────────────────────────────


def test_detect_tests_pytest_ini(tmp_repo):
    (tmp_repo / "pytest.ini").write_text("[pytest]\n")
    assert detect_tests(str(tmp_repo)) == "pytest"


def test_detect_tests_setup_cfg(tmp_repo):
    (tmp_repo / "setup.cfg").write_text(
        "[tool:pytest]\naddopts = -v\n"
    )
    assert detect_tests(str(tmp_repo)) == "pytest"


def test_detect_tests_tox(tmp_repo):
    (tmp_repo / "tox.ini").write_text("[tox]\n")
    # tox.ini present still returns pytest
    # (tox wraps pytest in most Python repos)
    assert detect_tests(str(tmp_repo)) == "pytest"


def test_detect_tests_default(tmp_repo):
    assert detect_tests(str(tmp_repo)) == "pytest"


# ── detect_lang ──────────────────────────────


def test_detect_lang_python(tmp_repo):
    (tmp_repo / "foo.py").write_text("x=1\n")
    assert detect_lang(str(tmp_repo)) == "py"


def test_detect_lang_empty(tmp_repo):
    # No files → unknown
    assert detect_lang(str(tmp_repo)) == "unknown"


# ── failure_class ────────────────────────────


def test_failure_class_extracts_import_error():
    text = (
        "Traceback (most recent call last):\n"
        "  File 'x.py', line 1\n"
        "ImportError: No module named 'foo'\n"
    )
    assert failure_class(text) == "ImportError"


def test_failure_class_extracts_assertion_error():
    text = "AssertionError: 1 != 2"
    assert failure_class(text) == "AssertionError"


def test_failure_class_extracts_type_error():
    text = (
        "TypeError: unsupported operand type(s)\n"
    )
    assert failure_class(text) == "TypeError"


def test_failure_class_empty():
    assert failure_class("") == "none"


def test_failure_class_no_match():
    assert (
        failure_class("everything is fine")
        == "unknown"
    )


# ── build_context ────────────────────────────


def test_build_context_integration(tmp_repo):
    (tmp_repo / "requirements.txt").write_text(
        "flask\n"
    )
    (tmp_repo / "pytest.ini").write_text(
        "[pytest]\n"
    )
    (tmp_repo / "app.py").write_text("x=1\n")
    ctx = build_context(
        str(tmp_repo),
        "ImportError: no module named foo",
    )
    assert ctx["lang"] == "py"
    assert ctx["framework"] == "flask"
    assert ctx["tests"] == "pytest"
    assert ctx["failure"] == "ImportError"


def test_build_context_defaults(tmp_repo):
    ctx = build_context(str(tmp_repo), "")
    assert ctx["lang"] == "unknown"
    assert ctx["framework"] == "unknown"
    assert ctx["tests"] == "pytest"
    assert ctx["failure"] == "none"
