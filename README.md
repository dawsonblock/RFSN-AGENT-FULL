<div align="center">

# RFSN Agent

### Autonomous Coding-Agent Core — Repair Stage

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/downloads/)
[![Status: Prototype / Repair Stage](https://img.shields.io/badge/Status-Prototype%20%2F%20Repair%20Stage-yellow)](https://github.com/dawsonblock/RFSN-AGENT-FULL)

</div>

---

> ⚠️ **This is NOT production-ready software.**
> It is a partially repaired autonomous coding-agent prototype.
> Do not run against untrusted repositories unless a secure Docker sandbox is
> configured and tested.  See [Security Model](#security-model) below.

---

## What Currently Works

| Component | Status | Notes |
|:---|:---|:---|
| Safety kernel (`rfsn_kernel/`) | ✅ Working | Normalize → Validate → Simulate → Decide pipeline |
| Hard ledger (`hard_ledger.py`) | ✅ Working | HMAC hash-chain audit log |
| Memory immune system | ✅ Fixed | `load()` now correctly restores active entries |
| Tool registry (`tool_registry.py`) | ✅ Working | Single source of truth; wired into validate/normalize |
| `apply_patch` | ✅ Enabled | Requires patch gate |
| `repo_search`, `read_file`, `list_files`, etc. | ✅ Enabled | Read-only tools |
| `run_tests`, `run_cmd_template` | ✅ Enabled | Require sandbox |
| Replay log | ✅ Working | Written by orchestrator on every run |
| Pytest collection | ✅ Fixed | Demo repos excluded via `pytest.ini` |

## What Is Disabled

| Tool | Reason |
|:---|:---|
| `trace_execution` | Used `os.system()` with agent-controlled strings — **critical shell injection**; quarantined |
| `apply_semantic_patch` | API was inconsistent across layers; re-enabled only after all safety tests pass |

## What Is Experimental / Placeholder

- `services/learner_service/self_evolve.py` — stub; does nothing
- `services/learner_service/policy_synth.py` — stub; does nothing
- `services/learner_service/auto_patch.py` — stub; does nothing
- `rfsn_kernel/policy_prover.py` — stub; always returns True
- `rfsn_kernel/symbolic_graph.py` — stub; returns empty dict
- `rfsn_kernel/virtual_time.py` — stub; returns 0
- `cluster/`, `diagnostics/`, `system/`, `stability/` — experimental or unused
- Multi-agent swarm — not part of the active execution path

---

## Security Model

See [SECURITY_MODEL.md](SECURITY_MODEL.md) for the full model.

**Active sandbox mode: `local_dev`** (set via `RFSN_SANDBOX_MODE`).

- Local dev mode is **only safe for trusted repositories**.
- Do NOT run against untrusted code in local dev mode.
- Docker sandbox mode is available but requires a running Docker daemon and
  explicit configuration (`RFSN_EXEC_USE_DOCKER=1`).

**Shell execution policy:**
- No `shell=True` in runtime paths.
- No `os.system()`.
- All command execution uses structured argument lists.
- Agent-controlled content never reaches the shell.
- `trace_execution` is disabled because it violated this policy.

**Patch policy:**
- All patches go through the patch risk gate.
- Test files, CI configs, and dependency manifests are protected.
- No-op patches are rejected (do not silently succeed).

---

## Tool Registry

Every tool is declared in `rfsn_kernel/tool_registry.py`.
No tool can be used unless it appears in the registry with `enabled=True`.

**Enabled tools:** `repo_search`, `repo_read_range`, `read_file`, `list_files`,
`detect_project`, `detect_workdirs`, `apply_patch`, `ensure_deps`, `run_tests`,
`run_cmd_template`, `format_fix`, `generate_repo_map`

**Disabled tools:** `trace_execution` (unsafe), `apply_semantic_patch` (pending validation)

---

## How to Run Tests

```bash
pip install pytest
python -m pytest tests/test_kernel_boot.py -q
python -m pytest tests/test_rfsn_kernel.py -q
python -m pytest tests/test_tool_registry_consistency.py -q
python -m pytest tests/test_semantic_patch_safety.py -q
python -m pytest tests/test_command_safety.py -q
python -m pytest tests/test_executor_dispatch_consistency.py -q
python -m pytest tests/test_orchestrator_minimal_loop.py -q
python -m pytest tests/test_sandbox_mode.py -q
python -m pytest -q   # full suite (excludes demo repos)
```

---

## How to Run a Local Toy Repair

See [RUN_LOCAL_TOY_REPAIR.md](RUN_LOCAL_TOY_REPAIR.md) for step-by-step instructions.

---

## Known Limitations

1. No LLM planner is wired in.  Orchestrator runs in `dry_run` mode without a
   `manual_plan`.
2. Docker sandbox is not enabled by default.  Local dev mode is unsafe for
   untrusted code.
3. `apply_semantic_patch` is disabled until all safety tests pass.
4. `trace_execution` is quarantined; do not re-enable without a safe rewrite.
5. Self-healing and learner modules are experimental stubs.
6. SWE-bench harness exists but no benchmark run has been completed.

---

## Benchmark Status

**SWE-bench:** The harness (`rfsn_swebench/`) exists but **no benchmark run
has been completed**.  Do not claim SWE-bench success.

---

## Project Structure

```
RFSN-AGENT-FULL/
├── rfsn_kernel/            # Safety kernel (canonical)
│   ├── kernel.py           #   HardKernel — core decision loop
│   ├── tool_registry.py    #   Canonical tool registry (single source of truth)
│   ├── dispatcher.py       #   Unified tool dispatcher (warm+cold)
│   ├── normalize.py        #   Proposal normalization
│   ├── validate.py         #   Hard-bound validation
│   ├── hard_ledger.py      #   HMAC hash-chain audit ledger
│   └── memory.py           #   Memory immune system
├── services/
│   ├── executor/           #   Sandboxed execution
│   ├── orchestrator/       #   Task lifecycle + bounded loop
│   ├── tool_gateway/       #   HTTP gateway with policy enforcement
│   └── learner_service/    #   Trajectory store (experimental)
├── rfsn_swebench/          #   SWE-bench harness (not yet benchmarked)
├── autofix/                #   Corrective action executor (shell=True removed)
├── policies/               #   YAML policy definitions
├── tests/                  #   Unit + integration tests
├── CHANGELOG_FIXES.md      #   What was fixed in this repair pass
├── SECURITY_MODEL.md       #   Security model documentation
└── RUN_LOCAL_TOY_REPAIR.md #   How to run a local toy repair
```

---

## License

MIT © 2026 RFSN Project.
