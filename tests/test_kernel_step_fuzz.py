from hypothesis import (  # type: ignore[import-not-found]
    given,
    strategies as st,
    settings,
    HealthCheck,
)
from services.orchestrator.kernel import (
    Kernel,
    _is_safe_read_path,
)  # type: ignore[import-not-found]

k = Kernel(
    "shared/bundle_schema.json",
    "policies/tool_allowlist.yaml",
    "policies/gate_policy.yaml",
)
STEP_TYPE = st.text(
    st.characters(
        min_codepoint=32, max_codepoint=126,
    ),
    min_size=0, max_size=40,
)
PATH_STR = st.text(
    st.characters(
        min_codepoint=1, max_codepoint=126,
    ),
    min_size=0, max_size=80,
)


@settings(
    max_examples=1500,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(STEP_TYPE)
def test_kernel_rejects_non_allowlisted_step_types(step_type):
    bundle = {
        "intent": "fuzz",
        "bundle_id": "b-fuzz0001",
        "steps": [{"id": "s1", "type": step_type}],
        "acceptance": {"tests_green": True, "no_new_failures": True},
    }
    errs = k.validate_and_plan(bundle)["errors"]
    allow = {
        "repo_search", "repo_read_range",
        "apply_patch", "ensure_deps",
        "run_tests",
    }
    if step_type not in allow:
        assert errs


def test_budget_exceeded_repo_search():
    """Gate rejects > 4 repo_search steps."""
    steps = [
        {
            "id": f"s{i}",
            "type": "repo_search",
            "pattern": "foo",
        }
        for i in range(1, 6)
    ]
    bundle = {
        "intent": "fuzz",
        "bundle_id": "b-budget1",
        "steps": steps,
        "acceptance": {
            "tests_green": True,
            "no_new_failures": True,
        },
    }
    d = k.validate_and_plan(bundle)
    assert not d["ok"]
    codes = [e["code"] for e in d["errors"]]
    assert "BUDGET_EXCEEDED" in codes


def test_budget_exceeded_repo_read():
    """Gate rejects > 6 repo_read_range steps."""
    steps = [
        {
            "id": f"r{i}",
            "type": "repo_read_range",
            "path": "src/foo.py",
            "line_start": 1,
            "line_end": 10,
        }
        for i in range(1, 8)
    ]
    bundle = {
        "intent": "fuzz",
        "bundle_id": "b-budget2",
        "steps": steps,
        "acceptance": {
            "tests_green": True,
            "no_new_failures": True,
        },
    }
    d = k.validate_and_plan(bundle)
    assert not d["ok"]
    codes = [e["code"] for e in d["errors"]]
    assert "BUDGET_EXCEEDED" in codes


def test_read_path_blocked_traversal():
    """Gate blocks path traversal in read."""
    bundle = {
        "intent": "fuzz",
        "bundle_id": "b-trav",
        "steps": [{
            "id": "r1",
            "type": "repo_read_range",
            "path": "../../etc/passwd",
            "line_start": 1,
            "line_end": 5,
        }],
        "acceptance": {
            "tests_green": True,
            "no_new_failures": True,
        },
    }
    d = k.validate_and_plan(bundle)
    assert not d["ok"]
    codes = [e["code"] for e in d["errors"]]
    assert "READ_PATH_BLOCKED" in codes


def test_read_path_blocked_git():
    """Gate blocks .git/ reads."""
    bundle = {
        "intent": "fuzz",
        "bundle_id": "b-git",
        "steps": [{
            "id": "r1",
            "type": "repo_read_range",
            "path": ".git/config",
            "line_start": 1,
            "line_end": 5,
        }],
        "acceptance": {
            "tests_green": True,
            "no_new_failures": True,
        },
    }
    d = k.validate_and_plan(bundle)
    assert not d["ok"]
    codes = [e["code"] for e in d["errors"]]
    assert "READ_PATH_BLOCKED" in codes


def test_read_range_too_large():
    """Gate rejects read > 300 lines."""
    bundle = {
        "intent": "fuzz",
        "bundle_id": "b-bigread",
        "steps": [{
            "id": "r1",
            "type": "repo_read_range",
            "path": "src/foo.py",
            "line_start": 1,
            "line_end": 500,
        }],
        "acceptance": {
            "tests_green": True,
            "no_new_failures": True,
        },
    }
    d = k.validate_and_plan(bundle)
    assert not d["ok"]
    codes = [e["code"] for e in d["errors"]]
    assert "READ_RANGE_TOO_LARGE" in codes


def test_bundle_too_large():
    """Gate rejects bundles with > 15 steps."""
    steps = [
        {
            "id": f"s{i}",
            "type": "repo_search",
            "pattern": "x",
        }
        for i in range(1, 17)
    ]
    bundle = {
        "intent": "fuzz",
        "bundle_id": "b-big",
        "steps": steps,
        "acceptance": {
            "tests_green": True,
            "no_new_failures": True,
        },
    }
    d = k.validate_and_plan(bundle)
    assert not d["ok"]
    codes = [e["code"] for e in d["errors"]]
    assert "BUNDLE_TOO_LARGE" in codes


def test_timeout_clamped():
    """Gate clamps step timeout to policy max."""
    bundle = {
        "intent": "fuzz",
        "bundle_id": "b-clamp",
        "steps": [{
            "id": "s1",
            "type": "repo_search",
            "pattern": "foo",
            "timeout_s": 9999,
        }],
        "acceptance": {
            "tests_green": True,
            "no_new_failures": True,
        },
    }
    d = k.validate_and_plan(bundle)
    assert d["ok"]
    step = d["approved_steps"][0]
    assert step["timeout_s"] <= 30


def test_budget_usage_reported():
    """Gate reports budget_usage counts."""
    bundle = {
        "intent": "fuzz",
        "bundle_id": "b-usage",
        "steps": [
            {
                "id": "s1",
                "type": "repo_search",
                "pattern": "a",
            },
            {
                "id": "s2",
                "type": "repo_search",
                "pattern": "b",
            },
            {
                "id": "r1",
                "type": "repo_read_range",
                "path": "src/x.py",
                "line_start": 1,
                "line_end": 10,
            },
        ],
        "acceptance": {
            "tests_green": True,
            "no_new_failures": True,
        },
    }
    d = k.validate_and_plan(bundle)
    assert d["ok"]
    usage = d["budget_usage"]
    assert usage["repo_search"] == 2
    assert usage["repo_read_range"] == 1


@settings(
    max_examples=1000,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(PATH_STR)
def test_safe_read_path_blocks_traversal(p):
    """No path with .. components passes."""
    import os
    norm = os.path.normpath(p)
    if ".." in norm.split(os.sep):
        assert not _is_safe_read_path(
            p, [], [],
        )
    if p.startswith("/") or p.startswith("~"):
        assert not _is_safe_read_path(
            p, [], [],
        )
