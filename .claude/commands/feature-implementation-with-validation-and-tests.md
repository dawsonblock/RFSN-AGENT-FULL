---
name: feature-implementation-with-validation-and-tests
description: Workflow command scaffold for feature-implementation-with-validation-and-tests in RFSN-AGENT-FULL.
allowed_tools: ["Bash", "Read", "Write", "Grep", "Glob"]
---

# /feature-implementation-with-validation-and-tests

Use this workflow when working on **feature-implementation-with-validation-and-tests** in `RFSN-AGENT-FULL`.

## Goal

Implements or updates a core feature, updates validation logic, and adds or updates regression tests.

## Common Files

- `rfsn_kernel/kernel.py`
- `rfsn_kernel/tool_registry.py`
- `rfsn_kernel/validate.py`
- `tests/test_kernel_boot.py`

## Suggested Sequence

1. Understand the current state and failure mode before editing.
2. Make the smallest coherent change that satisfies the workflow goal.
3. Run the most relevant verification for touched files.
4. Summarize what changed and what still needs review.

## Typical Commit Signals

- Edit or add core implementation files (e.g., kernel.py, tool_registry.py)
- Update validation logic (e.g., validate.py)
- Add or update regression or unit tests (e.g., tests/test_kernel_boot.py)

## Notes

- Treat this as a scaffold, not a hard-coded script.
- Update the command if the workflow evolves materially.