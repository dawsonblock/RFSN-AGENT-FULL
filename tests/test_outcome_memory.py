"""Unit tests for rfsn_swebench.outcome_memory."""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from rfsn_swebench.outcome_memory import OutcomeMemory


class TestOutcomeMemory:
    """Tests for the OutcomeMemory JSONL-backed store."""

    def _make_mem(self, tmp_path: str) -> OutcomeMemory:
        return OutcomeMemory(os.path.join(tmp_path, "mem.jsonl"))

    def test_record_creates_file(self, tmp_path):
        mem = self._make_mem(str(tmp_path))
        mem.record("task1", "PASS", "django__django")
        assert os.path.isfile(os.path.join(str(tmp_path), "mem.jsonl"))

    def test_record_and_reload(self, tmp_path):
        path = os.path.join(str(tmp_path), "mem.jsonl")
        mem1 = OutcomeMemory(path)
        mem1.record("task1", "PASS", "django__django", iters_used=3)
        mem1.record(
            "task2",
            "FAIL",
            "django__django",
            error_type="test_fail",
            error_summary="assertion error in view",
        )

        # Load from fresh instance
        mem2 = OutcomeMemory(path)
        assert mem2.total_outcomes == 2

    def test_repo_outcomes_filters_by_family(self, tmp_path):
        mem = self._make_mem(str(tmp_path))
        mem.record("django-11049", "PASS", "django__django")
        mem.record("flask-4045", "FAIL", "flask__flask")
        mem.record("django-12345", "FAIL", "django__django")

        results = mem.get_repo_outcomes("django__django")
        assert len(results) == 2
        assert all("django" in r.repo for r in results)

    def test_common_mistakes(self, tmp_path):
        mem = self._make_mem(str(tmp_path))
        mem.record("t1", "FAIL", "r1", error_summary="assertion error")
        mem.record("t2", "FAIL", "r2", error_summary="assertion error")
        mem.record("t3", "FAIL", "r3", error_summary="syntax error")
        mem.record("t4", "PASS", "r4")  # no error

        mistakes = mem.get_common_mistakes()
        assert mistakes[0] == "assertion error"

    def test_successful_patterns(self, tmp_path):
        mem = self._make_mem(str(tmp_path))
        mem.record("django-1", "PASS", "django__django", files_changed=["models.py"])
        mem.record("django-2", "FAIL", "django__django")
        mem.record("flask-1", "PASS", "flask__flask")

        successes = mem.get_successful_patterns("django__django")
        assert len(successes) == 1
        assert successes[0].task_id == "django-1"

    def test_format_learnings_empty(self, tmp_path):
        mem = self._make_mem(str(tmp_path))
        assert mem.format_learnings("django__django") == ""

    def test_format_learnings_with_data(self, tmp_path):
        mem = self._make_mem(str(tmp_path))
        mem.record(
            "django-1",
            "PASS",
            "django__django",
            files_changed=["views.py"],
            iters_used=2,
        )
        mem.record(
            "django-2", "FAIL", "django__django", error_summary="off-by-one in queryset"
        )
        mem.record("flask-1", "FAIL", "flask__flask", error_summary="common mistake")

        output = mem.format_learnings("django__django")
        assert "Successful Patterns" in output
        assert "views.py" in output
        assert "off-by-one" in output

    def test_pass_rate(self, tmp_path):
        mem = self._make_mem(str(tmp_path))
        mem.record("t1", "PASS", "r1")
        mem.record("t2", "FAIL", "r2")
        mem.record("t3", "PASS", "r3")
        assert mem.pass_rate == pytest.approx(2 / 3)

    def test_empty_store(self, tmp_path):
        mem = self._make_mem(str(tmp_path))
        assert mem.total_outcomes == 0
        assert mem.pass_rate == 0.0
        assert mem.get_common_mistakes() == []

    def test_malformed_lines_skipped(self, tmp_path):
        path = os.path.join(str(tmp_path), "mem.jsonl")
        # Write some valid and invalid lines
        with open(path, "w") as f:
            f.write('{"task_id":"t1","status":"PASS","repo":"r1"}\n')
            f.write("not json\n")
            f.write('{"invalid_fields": true}\n')
            f.write("")

        mem = OutcomeMemory(path)
        assert mem.total_outcomes == 1

    def test_patch_snippet_truncated(self, tmp_path):
        mem = self._make_mem(str(tmp_path))
        long_patch = "x" * 1000
        mem.record("t1", "PASS", "r1", patch_snippet=long_patch)

        # Reload and check truncation
        mem2 = OutcomeMemory(os.path.join(str(tmp_path), "mem.jsonl"))
        assert mem2.total_outcomes == 1  # triggers lazy load
        assert len(mem2._outcomes[0].patch_snippet) <= 500
