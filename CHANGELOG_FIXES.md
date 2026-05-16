# CHANGELOG_FIXES.md

## Repair Pass — RFSN-AGENT-FULL-main 12

### What Was Broken

1. **`MemoryImmuneSystem.load()` was broken** — active entries were never
   inserted into `_store` and `count` was never incremented, so every
   `save → load` roundtrip returned 0.
2. **`apply_semantic_patch` API was inconsistent** — the gateway and executor
   expected different field names (`patch` vs `search`/`replace`).  The
   normalizer stripped fields that the executor needed.
3. **Semantic patching could silently no-op** — no `NoOpPatchError` existed;
   a patch that changed nothing was not detected.
4. **`tool_registry.py` was not fully wired** — `apply_semantic_patch` and
   `trace_execution` appeared in `tool_allowlist.yaml` despite being unsafe
   or broken.
5. **`trace_execution` used `os.system(cmd)`** — agent-controlled command
   strings reached the shell directly.  Critical shell injection vector.
6. **`autofix/apply.py` used `shell=True`** — arbitrary command strings
   passed from action dicts were executed via `/bin/sh`.
7. **Warm and cold executor paths diverged** — no shared dispatcher; tool
   behaviour was path-dependent.
8. **Orchestrator ran a fake demo sequence** — hard-coded `generate_repo_map`
   → `apply_semantic_patch` → `command echo done`.  `command` is not a
   canonical tool type.
9. **Docker sandbox config was inconsistent** — executor could silently
   downgrade to local exec without documentation.
10. **Pytest collected demo/fixture repos** — full collection failed on missing
    imports in those repos.
11. **README overclaimed production status** — "Status: Production",
    "Self-Healing Core", "MCTS Backtracking", "SWE-bench success".
12. **Placeholder modules were advertised** — `self_evolve.py`,
    `policy_synth.py`, `auto_patch.py`, etc. had no docstrings indicating
    they are stubs.

---

### What Was Fixed

1. **`MemoryImmuneSystem.load()`** — active entries now inserted into
   `_store`; core entries inserted into both `_core_axioms` and `_store`;
   `count` now reflects loaded active entries; corrupt records are skipped
   with a logged warning instead of silently ignored.
2. **`apply_semantic_patch` API** — canonical API is now `path` / `search` /
   `replace` throughout.  `rfsn_swebench/patcher.py` rewritten with
   `apply_semantic_patch_to_file()` and `apply_semantic_patch_to_content()`.
   `NoOpPatchError` and `PatchGateError` added.
3. **No-op patch detection** — `apply_semantic_patch_to_content()` raises
   `NoOpPatchError` if `search == replace`.
4. **`ToolSpec` expanded** — added `enabled`, `risk_level`,
   `allowed_in_warm_path`, `allowed_in_cold_path`, `requires_sandbox`,
   `requires_patch_gate`, `max_input_size`.
5. **`tool_registry.py` wired** — `trace_execution` and
   `apply_semantic_patch` marked `enabled=False`; `validate.py` now
   rejects disabled tools unconditionally; `tool_allowlist.yaml` updated.
6. **`trace_execution` quarantined** — executor block replaced with a
   clear `ValueError`; `os.system()` removed.
7. **`autofix/apply.py` rewritten** — `shell=True` removed; `_exec_cmd`
   removed; all command execution now uses `ALLOWED_COMMAND_TEMPLATES`
   with structured argument lists; `shell=True` handlers
   (`_handle_install`, `_handle_restart`) removed.
8. **Unified dispatcher** — `rfsn_kernel/dispatcher.py` created with
   `dispatch_tool()` and `ToolResult`; both warm and cold paths use the
   same dispatcher.
9. **Orchestrator fake loop replaced** — `run_engine.py` now supports
   `dry_run` mode and `manual_plan` mode.  The hard-coded demo steps and
   the non-canonical `command` tool are removed.
10. **`pytest.ini` added** — `testpaths = tests`; `data`, `fixtures`, etc.
    excluded from collection.
11. **README rewritten** — production/autonomy/benchmark claims removed;
    disabled tools documented; security model documented.
12. **Placeholder docstrings added** — `policy_prover.py`,
    `symbolic_graph.py`, `virtual_time.py`, `self_evolve.py`,
    `policy_synth.py`, `auto_patch.py` all marked with
    "Experimental placeholder. Not used by the active execution path."

---

### What Was Disabled

- `trace_execution` — unsafe shell execution; quarantined in executor.
- `apply_semantic_patch` — disabled until all tests in
  `tests/test_semantic_patch_safety.py` pass.

---

### What Remains Experimental

- `services/learner_service/self_evolve.py`
- `services/learner_service/policy_synth.py`
- `services/learner_service/auto_patch.py`
- `rfsn_kernel/policy_prover.py`
- `rfsn_kernel/symbolic_graph.py`
- `rfsn_kernel/virtual_time.py`
- Multi-agent swarm modules
- SWE-bench benchmark (harness exists; no run completed)

---

### Tests Added

- `tests/test_tool_registry_consistency.py`
- `tests/test_semantic_patch_safety.py`
- `tests/test_command_safety.py`
- `tests/test_executor_dispatch_consistency.py`
- `tests/test_orchestrator_minimal_loop.py`
- `tests/test_sandbox_mode.py`

---

### Security Limitations

- Local dev mode (`RFSN_SANDBOX_MODE=local_dev`) is unsafe for untrusted repos.
- Docker sandbox mode requires explicit configuration and a running daemon.
- No LLM planner is wired; orchestrator runs in dry-run mode by default.
- `trace_execution` must not be re-enabled without a complete safe rewrite.
