"""Tests for stage tracking in learner context_key."""

import os
import sys

# ── Wire up learner_service imports ──────────
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "services",
        "learner_service",
    ),
)

# We only need the pure function; avoid importing
# FastAPI app which needs DuckDB at module level.
# So we extract context_key manually.
import types  # noqa: E402

_learner_path = os.path.join(
    os.path.dirname(__file__),
    "..",
    "services",
    "learner_service",
    "app.py",
)


def _load_context_key():
    """Load context_key function from learner app
    without triggering the full FastAPI app init."""
    import ast

    with open(_learner_path) as f:
        source = f.read()

    tree = ast.parse(source)

    # Find the context_key function source
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "context_key":
            func_source = ast.get_source_segment(
                source,
                node,
            )
            if func_source is None:
                continue
            mod = types.ModuleType("_ck_mod")
            exec(
                compile(func_source, "<ck>", "exec"),
                mod.__dict__,
            )
            return mod.context_key

    raise RuntimeError("Could not find context_key function")


context_key = _load_context_key()

# Precompute repo_short for default "unknown" repo_id.
import hashlib  # noqa: E402

_UNKNOWN_REPO_SHORT = hashlib.sha256(b"unknown").hexdigest()[:8]


# ── Tests ────────────────────────────────────


class TestContextKeyStage:
    """Verify that context_key includes stage."""

    def test_includes_stage_dimension(self):
        meta = {
            "lang": "py",
            "tests": "pytest",
            "framework": "django",
            "failure": "ImportError",
            "stage": "deps",
        }
        ck = context_key(meta)
        parts = ck.split("|")
        assert len(parts) == 6
        assert parts[4] == "deps"
        assert parts[5] == _UNKNOWN_REPO_SHORT

    def test_stage_defaults_to_unknown(self):
        meta = {
            "lang": "py",
            "tests": "pytest",
            "framework": "flask",
            "failure": "none",
        }
        ck = context_key(meta)
        parts = ck.split("|")
        assert parts[4] == "unknown"

    def test_stage_success(self):
        meta = {
            "lang": "py",
            "tests": "pytest",
            "framework": "unknown",
            "failure": "none",
            "stage": "success",
        }
        ck = context_key(meta)
        assert "|success|" in ck
        assert ck == f"py|pytest|unknown|none|success|{_UNKNOWN_REPO_SHORT}"

    def test_stage_gate_reject(self):
        meta = {
            "lang": "py",
            "tests": "pytest",
            "framework": "unknown",
            "failure": "none",
            "stage": "gate_reject",
        }
        ck = context_key(meta)
        parts = ck.split("|")
        assert parts[4] == "gate_reject"

    def test_stage_apply_patch(self):
        meta = {
            "lang": "js",
            "tests": "jest",
            "framework": "react",
            "failure": "SyntaxError",
            "stage": "apply_patch",
        }
        ck = context_key(meta)
        assert ck == (f"js|jest|react|syntaxerror|apply_patch|{_UNKNOWN_REPO_SHORT}")

    def test_stage_tests(self):
        meta = {
            "lang": "py",
            "tests": "pytest",
            "framework": "django",
            "failure": "AssertionError",
            "stage": "tests",
        }
        ck = context_key(meta)
        assert ck == (f"py|pytest|django|assertionerror|tests|{_UNKNOWN_REPO_SHORT}")

    def test_stage_is_lowercased(self):
        meta = {
            "lang": "py",
            "tests": "pytest",
            "framework": "unknown",
            "failure": "none",
            "stage": "GATE_REJECT",
        }
        ck = context_key(meta)
        parts = ck.split("|")
        assert parts[4] == "gate_reject"

    def test_stage_whitespace_stripped(self):
        meta = {
            "lang": "py",
            "tests": "pytest",
            "framework": "unknown",
            "failure": "none",
            "stage": "  tests  ",
        }
        ck = context_key(meta)
        parts = ck.split("|")
        assert parts[4] == "tests"

    def test_empty_stage_treated_as_unknown(self):
        meta = {
            "lang": "py",
            "tests": "pytest",
            "framework": "unknown",
            "failure": "none",
            "stage": "",
        }
        ck = context_key(meta)
        parts = ck.split("|")
        assert parts[4] == "unknown"

    def test_all_five_valid_stages(self):
        """Verify all expected stage values work."""
        stages = [
            "gate_reject",
            "apply_patch",
            "tests",
            "deps",
            "success",
        ]
        for stage in stages:
            meta = {
                "lang": "py",
                "tests": "pytest",
                "framework": "unknown",
                "failure": "none",
                "stage": stage,
            }
            ck = context_key(meta)
            parts = ck.split("|")
            assert parts[4] == stage, f"stage '{stage}' not at position 4: {ck}"

    def test_different_stages_produce_different_keys(
        self,
    ):
        """Same repo context but different stages
        must produce distinct context keys."""
        base = {
            "lang": "py",
            "tests": "pytest",
            "framework": "django",
            "failure": "ImportError",
        }
        keys = set()
        for stage in [
            "gate_reject",
            "tests",
            "apply_patch",
            "deps",
            "success",
        ]:
            meta = {**base, "stage": stage}
            keys.add(context_key(meta))
        assert len(keys) == 5

    def test_repo_id_affects_key(self):
        """Different repo_ids produce different keys."""
        meta_a = {
            "lang": "py",
            "tests": "pytest",
            "framework": "unknown",
            "failure": "none",
            "stage": "tests",
            "repo_id": "repo_a",
        }
        meta_b = {**meta_a, "repo_id": "repo_b"}
        assert context_key(meta_a) != context_key(meta_b)
