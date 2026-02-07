"""Integration tests for the orchestrator kernel gate.

Tests cover: multi-proposal selection logic,
test ordering enforcement, patch content bans,
risk scoring, and edge-case bundles.
"""
import copy
import json
import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..", "services", "orchestrator",
    ),
)
from kernel import (  # type: ignore[import-not-found]
    Kernel,
    _extract_touched_files,
    _count_diff_lines,
    _is_safe_read_path,
)

k = Kernel(
    "shared/bundle_schema.json",
    "policies/tool_allowlist.yaml",
    "policies/gate_policy.yaml",
)


# ── helper ───────────────────────────────────

def _base_bundle(
    steps=None, intent="test", bid="b-test01",
):
    return {
        "intent": intent,
        "bundle_id": bid,
        "steps": steps or [],
        "acceptance": {
            "tests_green": True,
            "no_new_failures": True,
        },
    }


# ── test ordering enforcement ────────────────


def test_enforced_targeted_before_suite():
    """When a patch exists, gate must inject
    targeted + suite in correct order."""
    b = _base_bundle([
        {
            "id": "p1",
            "type": "apply_patch",
            "patch": (
                "--- a/src/x.py\n"
                "+++ b/src/x.py\n"
                "@@ -1 +1 @@\n"
                "-old\n"
                "+new\n"
            ),
        },
    ])
    d = k.validate_and_plan(b)
    assert d["ok"]
    types = [
        s.get("template_id")
        for s in d["approved_steps"]
        if s.get("type") == "run_tests"
    ]
    # targeted must come before suite
    assert types == [
        "pytest_targeted", "pytest_suite",
    ]


def test_no_duplicate_test_injection():
    """When bundle already has both test types,
    gate should NOT inject extras."""
    b = _base_bundle([
        {
            "id": "p1",
            "type": "apply_patch",
            "patch": (
                "--- a/src/x.py\n"
                "+++ b/src/x.py\n"
                "@@ -1 +1 @@\n"
                "-old\n"
                "+new\n"
            ),
        },
        {
            "id": "t1",
            "type": "run_tests",
            "template_id": "pytest_targeted",
            "template_params": {"target": "tests"},
            "timeout_s": 120,
        },
        {
            "id": "t2",
            "type": "run_tests",
            "template_id": "pytest_suite",
            "template_params": {"target": ""},
            "timeout_s": 300,
        },
    ])
    d = k.validate_and_plan(b)
    assert d["ok"]
    assert d["enforced_steps"] == []
    test_steps = [
        s for s in d["approved_steps"]
        if s.get("type") == "run_tests"
    ]
    assert len(test_steps) == 2


def test_ordering_patch_before_tests():
    """apply_patch must come before any tests."""
    b = _base_bundle([
        {
            "id": "t1",
            "type": "run_tests",
            "template_id": "pytest_targeted",
            "template_params": {"target": "tests"},
            "timeout_s": 120,
        },
        {
            "id": "p1",
            "type": "apply_patch",
            "patch": (
                "--- a/src/x.py\n"
                "+++ b/src/x.py\n"
                "@@ -1 +1 @@\n"
                "-old\n"
                "+new\n"
            ),
        },
    ])
    d = k.validate_and_plan(b)
    assert d["ok"]
    types = [
        s["type"] for s in d["approved_steps"]
    ]
    patch_idx = types.index("apply_patch")
    test_idxs = [
        i for i, t in enumerate(types)
        if t == "run_tests"
    ]
    for ti in test_idxs:
        assert ti > patch_idx


# ── patch content bans ───────────────────────


BANNED_SNIPPETS = [
    "--- a/t.py\n+++ b/t.py\n@@ -1 +1 @@\n"
    "-ok\n+pytest.skip('hack')\n",
    "--- a/t.py\n+++ b/t.py\n@@ -1 +1 @@\n"
    "-ok\n+@pytest.mark.skip\n",
    "--- a/t.py\n+++ b/t.py\n@@ -1 +1 @@\n"
    "-ok\n+eval('evil')\n",
    "--- a/t.py\n+++ b/t.py\n@@ -1 +1 @@\n"
    "-ok\n+exec('evil')\n",
    "--- a/t.py\n+++ b/t.py\n@@ -1 +1 @@\n"
    "-ok\n+subprocess.run(['ls'])\n",
]


@pytest.mark.parametrize("patch", BANNED_SNIPPETS)
def test_banned_patch_patterns(patch):
    b = _base_bundle([{
        "id": "p1",
        "type": "apply_patch",
        "patch": patch,
    }])
    d = k.validate_and_plan(b)
    assert not d["ok"]
    codes = [e["code"] for e in d["errors"]]
    assert "PATCH_CONTENT_BLOCKED" in codes


# ── risk scoring ─────────────────────────────


def test_large_patch_increases_risk():
    """Patch > 120 lines adds risk."""
    lines = "".join(
        f"+line{i}\n" for i in range(130)
    )
    patch = (
        "--- a/src/x.py\n+++ b/src/x.py\n"
        "@@ -1,1 +1,130 @@\n" + lines
    )
    b = _base_bundle([{
        "id": "p1",
        "type": "apply_patch",
        "patch": patch,
    }])
    d = k.validate_and_plan(b)
    assert d["risk_score"] >= 10


def test_ci_edit_high_risk():
    """Editing CI paths yields high risk (and rejection)."""
    patch = (
        "--- a/.github/workflows/ci.yml\n"
        "+++ b/.github/workflows/ci.yml\n"
        "@@ -1 +1 @@\n"
        "-old\n+new\n"
    )
    b = _base_bundle([{
        "id": "p1",
        "type": "apply_patch",
        "patch": patch,
    }])
    d = k.validate_and_plan(b)
    assert not d["ok"]
    codes = [e["code"] for e in d["errors"]]
    assert "PATCH_FORBIDDEN_CI_PATH" in codes


def test_test_edit_rejected():
    """Editing test files is forbidden."""
    patch = (
        "--- a/tests/test_foo.py\n"
        "+++ b/tests/test_foo.py\n"
        "@@ -1 +1 @@\n"
        "-old\n+new\n"
    )
    b = _base_bundle([{
        "id": "p1",
        "type": "apply_patch",
        "patch": patch,
    }])
    d = k.validate_and_plan(b)
    assert not d["ok"]
    codes = [e["code"] for e in d["errors"]]
    assert "PATCH_FORBIDDEN_TEST_EDIT" in codes


def test_dep_manifest_edit_rejected():
    """Editing requirements.txt is forbidden."""
    patch = (
        "--- a/requirements.txt\n"
        "+++ b/requirements.txt\n"
        "@@ -1 +1 @@\n"
        "-old\n+new\n"
    )
    b = _base_bundle([{
        "id": "p1",
        "type": "apply_patch",
        "patch": patch,
    }])
    d = k.validate_and_plan(b)
    assert not d["ok"]
    codes = [e["code"] for e in d["errors"]]
    assert "PATCH_FORBIDDEN_DEP_MANIFEST" in codes


# ── edge cases ───────────────────────────────


def test_empty_steps_accepted():
    """Bundle with no steps should pass gate."""
    b = _base_bundle([])
    d = k.validate_and_plan(b)
    assert d["ok"]
    assert d["approved_steps"] == []


def test_search_only_no_test_injection():
    """Search-only bundle (no patch) shouldn't get
    test steps injected."""
    b = _base_bundle([{
        "id": "s1",
        "type": "repo_search",
        "pattern": "def foo",
    }])
    d = k.validate_and_plan(b)
    assert d["ok"]
    assert d["enforced_steps"] == []


def test_ensure_deps_accepted():
    """ensure_deps step should pass."""
    b = _base_bundle([{
        "id": "d1",
        "type": "ensure_deps",
        "manifest": "requirements.txt",
        "timeout_s": 120,
    }])
    d = k.validate_and_plan(b)
    assert d["ok"]


# ── helper functions ─────────────────────────


def test_extract_touched_files():
    diff = (
        "--- a/old.py\n"
        "+++ b/src/new.py\n"
        "@@ -1 +1 @@\n"
        "-x\n+y\n"
    )
    assert _extract_touched_files(diff) == {
        "src/new.py",
    }


def test_count_diff_lines():
    diff = (
        "--- a/x.py\n+++ b/x.py\n"
        "@@ -1,2 +1,3 @@\n"
        " ctx\n-old1\n-old2\n+new1\n+new2\n+new3\n"
    )
    add, rem = _count_diff_lines(diff)
    assert add == 3
    assert rem == 2


def test_safe_read_path_rejects_null_byte():
    assert not _is_safe_read_path(
        "src/\x00evil.py", [], [],
    )
