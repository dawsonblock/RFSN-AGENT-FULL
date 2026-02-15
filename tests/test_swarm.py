"""Tests for Phase 8: Multi-Agent Swarm."""

import unittest

from rfsn_kernel.swarm import (
    AgentRole,
    ARCHITECT,
    CODER,
    QA,
    ALL_ROLES,
    Subtask,
    TaskDecomposition,
    PatchProposal,
    ReviewVerdict,
    RevisionRequest,
    Verdict,
    SwarmCoordinator,
)


# ── Mock Agent Functions ──────────────────────────────────────────────


def mock_architect(task, context):
    return TaskDecomposition(
        analysis=f"Analyzed: {task}",
        subtasks=(
            Subtask(description="Fix the bug", target_files=("main.py",)),
            Subtask(description="Add tests", target_files=("test_main.py",)),
        ),
        strategy="fix-then-test",
    )


def mock_coder_ok(subtask, context, revision_req=None):
    rev = revision_req.revision_number if revision_req else 0
    return PatchProposal(
        subtask_index=0,
        diff=f"--- a/main.py\n+++ b/main.py\n@@ revision {rev}",
        reasoning="Applied fix",
        files_modified=("main.py",),
    )


def mock_qa_approve(subtask, patch):
    return ReviewVerdict(
        verdict=Verdict.APPROVE,
        comments="Looks good",
        tests_passed=True,
    )


def mock_qa_reject(subtask, patch):
    return ReviewVerdict(
        verdict=Verdict.REJECT,
        comments="Fundamental flaw",
    )


def mock_qa_request_changes(subtask, patch):
    return ReviewVerdict(
        verdict=Verdict.REQUEST_CHANGES,
        comments="Fix edge case",
        issues=("Missing null check",),
    )


def mock_architect_empty(task, context):
    return TaskDecomposition(analysis="Nothing to do", subtasks=())


# ── Tests ─────────────────────────────────────────────────────────────


class TestRoles(unittest.TestCase):

    def test_architect_can_plan(self):
        self.assertTrue(ARCHITECT.can_do("plan"))
        self.assertTrue(ARCHITECT.can_do("read_file"))

    def test_architect_cannot_write(self):
        self.assertFalse(ARCHITECT.can_do("write_file"))
        self.assertFalse(ARCHITECT.can_do("apply_patch"))

    def test_coder_can_write(self):
        self.assertTrue(CODER.can_do("write_file"))
        self.assertTrue(CODER.can_do("apply_patch"))
        self.assertTrue(CODER.can_do("run_command"))

    def test_coder_cannot_plan(self):
        self.assertFalse(CODER.can_do("plan"))
        self.assertFalse(CODER.can_do("critique"))

    def test_qa_can_critique(self):
        self.assertTrue(QA.can_do("critique"))
        self.assertTrue(QA.can_do("run_tests"))

    def test_qa_cannot_write(self):
        self.assertFalse(QA.can_do("write_file"))
        self.assertFalse(QA.can_do("apply_patch"))

    def test_enforce_raises(self):
        with self.assertRaises(PermissionError):
            ARCHITECT.enforce("write_file")

    def test_enforce_passes(self):
        CODER.enforce("write_file")  # should not raise

    def test_all_roles_dict(self):
        self.assertEqual(len(ALL_ROLES), 3)
        self.assertIn("architect", ALL_ROLES)

    def test_role_serialization(self):
        d = ARCHITECT.to_dict()
        self.assertEqual(d["name"], "architect")
        self.assertIn("read_file", d["allowed_actions"])


class TestProtocol(unittest.TestCase):

    def test_subtask_creation(self):
        st = Subtask(description="Fix bug", target_files=("main.py",))
        self.assertEqual(st.description, "Fix bug")

    def test_decomposition_fingerprint(self):
        td = mock_architect("test", "")
        fp1 = td.fingerprint()
        fp2 = td.fingerprint()
        self.assertEqual(fp1, fp2)
        self.assertEqual(len(fp1), 16)

    def test_patch_proposal_serialization(self):
        pp = PatchProposal(
            subtask_index=0,
            diff="--- a\n+++ b",
            reasoning="fix",
            files_modified=("a.py",),
        )
        d = pp.to_dict()
        self.assertEqual(d["subtask_index"], 0)
        self.assertIn("diff", d)

    def test_review_verdict_serialization(self):
        rv = ReviewVerdict(
            verdict=Verdict.APPROVE,
            comments="LGTM",
        )
        d = rv.to_dict()
        self.assertEqual(d["verdict"], "APPROVE")

    def test_revision_request(self):
        pp = PatchProposal(subtask_index=0, diff="x", reasoning="y")
        rr = RevisionRequest(
            original_patch=pp,
            qa_feedback="Fix edge case",
            revision_number=1,
        )
        d = rr.to_dict()
        self.assertEqual(d["revision_number"], 1)


class TestCoordinatorApprove(unittest.TestCase):

    def test_full_approve_flow(self):
        ledger = []
        coord = SwarmCoordinator(
            architect_fn=mock_architect,
            coder_fn=mock_coder_ok,
            qa_fn=mock_qa_approve,
            ledger_fn=ledger.append,
        )
        result = coord.run("Fix the bug")

        self.assertEqual(result.status, "completed")
        self.assertEqual(len(result.subtask_results), 2)
        for sr in result.subtask_results:
            self.assertEqual(sr.status, "approved")

        # Should have ledger entries
        types = [e["type"] for e in ledger]
        self.assertIn("SWARM_START", types)
        self.assertIn("DECOMPOSITION", types)
        self.assertIn("PATCH_PROPOSAL", types)
        self.assertIn("REVIEW_VERDICT", types)
        self.assertIn("SWARM_END", types)

    def test_approved_patches(self):
        coord = SwarmCoordinator(
            architect_fn=mock_architect,
            coder_fn=mock_coder_ok,
            qa_fn=mock_qa_approve,
        )
        result = coord.run("task")
        patches = result.approved_patches()
        self.assertEqual(len(patches), 2)


class TestCoordinatorReject(unittest.TestCase):

    def test_reject_flow(self):
        coord = SwarmCoordinator(
            architect_fn=mock_architect,
            coder_fn=mock_coder_ok,
            qa_fn=mock_qa_reject,
        )
        result = coord.run("task")
        self.assertEqual(result.status, "failed")
        for sr in result.subtask_results:
            self.assertEqual(sr.status, "rejected")


class TestCoordinatorRevisions(unittest.TestCase):

    def test_max_revisions_escalated(self):
        coord = SwarmCoordinator(
            architect_fn=mock_architect,
            coder_fn=mock_coder_ok,
            qa_fn=mock_qa_request_changes,
            max_revisions=2,
        )
        result = coord.run("task")
        # All subtasks should be escalated after max revisions
        for sr in result.subtask_results:
            self.assertEqual(sr.status, "escalated")
            self.assertLessEqual(sr.revisions, 2)

    def test_revision_then_approve(self):
        """QA requests changes once, then approves on revision."""
        call_count = {"n": 0}

        def qa_revise_then_approve(subtask, patch):
            call_count["n"] += 1
            if call_count["n"] % 2 == 1:
                return ReviewVerdict(
                    verdict=Verdict.REQUEST_CHANGES,
                    comments="Needs fix",
                )
            return ReviewVerdict(
                verdict=Verdict.APPROVE,
                comments="Fixed",
                tests_passed=True,
            )

        coord = SwarmCoordinator(
            architect_fn=mock_architect,
            coder_fn=mock_coder_ok,
            qa_fn=qa_revise_then_approve,
        )
        result = coord.run("task")
        self.assertEqual(result.status, "completed")
        self.assertGreater(result.total_revisions, 0)


class TestCoordinatorEmptyDecomposition(unittest.TestCase):

    def test_empty_decomposition(self):
        coord = SwarmCoordinator(
            architect_fn=mock_architect_empty,
            coder_fn=mock_coder_ok,
            qa_fn=mock_qa_approve,
        )
        result = coord.run("task")
        self.assertEqual(result.status, "failed")
        self.assertEqual(len(result.subtask_results), 0)


class TestCoordinatorLedger(unittest.TestCase):

    def test_ledger_recording(self):
        ledger = []
        coord = SwarmCoordinator(
            architect_fn=mock_architect,
            coder_fn=mock_coder_ok,
            qa_fn=mock_qa_approve,
            ledger_fn=ledger.append,
        )
        coord.run("task")

        # Every entry should have a timestamp
        for entry in ledger:
            self.assertIn("timestamp", entry)
            self.assertIn("type", entry)

    def test_result_to_dict(self):
        coord = SwarmCoordinator(
            architect_fn=mock_architect,
            coder_fn=mock_coder_ok,
            qa_fn=mock_qa_approve,
        )
        result = coord.run("task")
        d = result.to_dict()
        self.assertEqual(d["status"], "completed")
        self.assertEqual(len(d["subtasks"]), 2)


if __name__ == "__main__":
    unittest.main()
