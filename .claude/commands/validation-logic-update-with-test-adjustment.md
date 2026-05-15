---
name: validation-logic-update-with-test-adjustment
description: Workflow command scaffold for validation-logic-update-with-test-adjustment in RFSN-AGENT-FULL.
allowed_tools: ["Bash", "Read", "Write", "Grep", "Glob"]
---

# /validation-logic-update-with-test-adjustment

Use this workflow when working on **validation-logic-update-with-test-adjustment** in `RFSN-AGENT-FULL`.

## Goal

Refines or fixes validation logic and updates related tests to ensure correctness.

## Common Files

- `rfsn_kernel/validate.py`
- `tests/test_kernel_boot.py`

## Suggested Sequence

1. Understand the current state and failure mode before editing.
2. Make the smallest coherent change that satisfies the workflow goal.
3. Run the most relevant verification for touched files.
4. Summarize what changed and what still needs review.

## Typical Commit Signals

- Edit validation logic (e.g., validate.py)
- Update or strengthen related tests (e.g., tests/test_kernel_boot.py)

## Notes

- Treat this as a scaffold, not a hard-coded script.
- Update the command if the workflow evolves materially.