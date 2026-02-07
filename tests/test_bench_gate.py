"""Tests for rfsn_swebench.gate — risk-gating and diff analysis."""
from __future__ import annotations

import textwrap

import pytest  # type: ignore[import-not-found]
from hypothesis import (  # type: ignore[import-not-found]
    given,
    settings,
    strategies as st,
    HealthCheck,
)

# Import under test — uses defaults (no policy YAML files present)
from rfsn_swebench.gate import (
    DiffStats,
    _DEFAULT_BANNED_PATTERNS,
    diff_analysis,
    load_policies,
    patch_risk_gate,
    reload_policies,
)


# ---------------------------------------------------------------------------
# diff_analysis unit tests
# ---------------------------------------------------------------------------

SIMPLE_DIFF = textwrap.dedent("""\
    diff --git a/src/foo.py b/src/foo.py
    index abc1234..def5678 100644
    --- a/src/foo.py
    +++ b/src/foo.py
    @@ -1,3 +1,4 @@
     import os
    +import sys
     def main():
    -    pass
    +    print("hello")
""")


def test_diff_analysis_basic_counts():
    stats = diff_analysis(SIMPLE_DIFF)
    assert stats.files_touched == ["src/foo.py"]
    assert stats.added_lines == 2   # +import sys, +print("hello")
    assert stats.deleted_lines == 1  # -pass
    assert stats.new_files == 0
    assert stats.size_bytes > 0


def test_diff_analysis_new_file():
    diff = textwrap.dedent("""\
        diff --git a/new.py b/new.py
        new file mode 100644
        --- /dev/null
        +++ b/new.py
        @@ -0,0 +1,2 @@
        +# new file
        +print("hi")
    """)
    stats = diff_analysis(diff)
    assert stats.new_files == 1
    assert stats.added_lines == 2
    assert "new.py" in stats.files_touched


def test_diff_analysis_empty():
    stats = diff_analysis("")
    assert stats.files_touched == []
    assert stats.added_lines == 0
    assert stats.deleted_lines == 0
    assert stats.new_files == 0


def test_diff_analysis_deleted_test_lines():
    diff = textwrap.dedent("""\
        diff --git a/tests/test_x.py b/tests/test_x.py
        --- a/tests/test_x.py
        +++ b/tests/test_x.py
        @@ -1,5 +1,1 @@
        -def test_one():
        -    assert True
        -def test_two():
        -    assert True
        +pass
    """)
    stats = diff_analysis(diff)
    # "test_one" and "test_two" lines deleted
    assert stats.deleted_test_lines >= 2


# ---------------------------------------------------------------------------
# patch_risk_gate unit tests
# ---------------------------------------------------------------------------

def test_gate_allows_clean_small_diff():
    report = patch_risk_gate(
        SIMPLE_DIFF,
        max_bytes=100_000,
        max_files=10,
        max_new_files=5,
    )
    assert report.decision == "ALLOW"
    assert report.reasons == []


def test_gate_rejects_oversized_patch():
    huge = "+" * 300_000
    report = patch_risk_gate(
        huge,
        max_bytes=250_000,
        max_files=100,
        max_new_files=100,
    )
    assert report.decision == "REJECT"
    assert any("too large" in r for r in report.reasons)


def test_gate_rejects_too_many_files():
    lines = []
    for i in range(30):
        lines.append(f"diff --git a/f{i}.py b/f{i}.py")
        lines.append(f"--- a/f{i}.py")
        lines.append(f"+++ b/f{i}.py")
        lines.append("@@ -1 +1 @@")
        lines.append(f"+# change {i}")
    diff = "\n".join(lines)
    report = patch_risk_gate(
        diff,
        max_bytes=1_000_000,
        max_files=5,
        max_new_files=100,
    )
    assert report.decision == "REJECT"
    assert any("too many files" in r for r in report.reasons)


def test_gate_rejects_too_many_new_files():
    lines = []
    for i in range(10):
        lines.append(f"diff --git a/new{i}.py b/new{i}.py")
        lines.append("new file mode 100644")
        lines.append(f"+++ b/new{i}.py")
        lines.append("@@ -0,0 +1 @@")
        lines.append(f"+# new {i}")
    diff = "\n".join(lines)
    report = patch_risk_gate(
        diff,
        max_bytes=1_000_000,
        max_files=100,
        max_new_files=3,
    )
    assert report.decision == "REJECT"
    assert any("too many new files" in r for r in report.reasons)


@pytest.mark.parametrize(
    "banned_line",
    [
        "+    pytest.skip('not needed')",
        "+@pytest.mark.skip",
        "+@skip",
        "+    xfail(reason='whatever')",
    ],
)
def test_gate_rejects_banned_patterns(banned_line):
    diff = textwrap.dedent(f"""\
        diff --git a/tests/t.py b/tests/t.py
        --- a/tests/t.py
        +++ b/tests/t.py
        @@ -1 +1 @@
        {banned_line}
    """)
    report = patch_risk_gate(
        diff,
        max_bytes=1_000_000,
        max_files=100,
        max_new_files=100,
    )
    assert report.decision == "REJECT"
    assert any("banned pattern" in r for r in report.reasons)


def test_gate_rejects_ci_path():
    diff = textwrap.dedent("""\
        diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
        --- a/.github/workflows/ci.yml
        +++ b/.github/workflows/ci.yml
        @@ -1 +1 @@
        +  - run: echo hi
    """)
    report = patch_risk_gate(
        diff,
        max_bytes=1_000_000,
        max_files=100,
        max_new_files=100,
    )
    assert report.decision == "REJECT"
    assert any("restricted path" in r for r in report.reasons)


def test_gate_rejects_large_test_deletions():
    lines = [
        "diff --git a/tests/t.py b/tests/t.py",
        "--- a/tests/t.py",
        "+++ b/tests/t.py",
        "@@ -1,60 +1,1 @@",
    ]
    for i in range(55):
        lines.append(f"-def test_case_{i}():")
    lines.append("+pass")
    diff = "\n".join(lines)
    report = patch_risk_gate(
        diff,
        max_bytes=1_000_000,
        max_files=100,
        max_new_files=100,
    )
    assert report.decision == "REJECT"
    assert any("test deletions" in r for r in report.reasons)


# ---------------------------------------------------------------------------
# Policy loading
# ---------------------------------------------------------------------------

def test_load_policies_falls_back_to_defaults():
    cfg = load_policies(
        diff_guard_path="/nonexistent/diff_guard.yaml",
        allowlist_path="/nonexistent/tool_allowlist.yaml",
    )
    assert cfg["max_patch_bytes"] == 250_000
    assert cfg["max_files_touched"] == 25
    assert cfg["max_new_files"] == 5
    assert cfg["banned_patterns"] == list(_DEFAULT_BANNED_PATTERNS)


def test_reload_policies_returns_config():
    cfg = reload_policies(
        diff_guard_path="/nonexistent/dg.yaml",
        allowlist_path="/nonexistent/al.yaml",
    )
    assert isinstance(cfg, dict)
    assert "max_patch_bytes" in cfg


# ---------------------------------------------------------------------------
# Fuzz: diff_analysis never crashes
# ---------------------------------------------------------------------------

@settings(
    max_examples=500,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(st.text(min_size=0, max_size=2000))
def test_diff_analysis_never_raises(s):
    stats = diff_analysis(s)
    assert isinstance(stats, DiffStats)
    assert stats.added_lines >= 0
    assert stats.deleted_lines >= 0
    assert stats.new_files >= 0
