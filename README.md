<p align="center">
  <strong>RFSN Agent — Hardened v6</strong><br>
  <em>Policy-gated coding agent with security-hardened execution and upstream learning</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.9%2B-blue" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/OS-Apple_Silicon_Native-black" alt="Mac Silicon">
  <img src="https://img.shields.io/badge/Security-Hardened_v6-red" alt="Hardened v6">
</p>

---

## Overview

**RFSN v6.4** is a multi-service coding agent designed for autonomous bug repair with strong safety guarantees.

### What Works (Production-Ready)

1. **Safety Kernel**: All tool execution flows through a single `kernel_step()` choke-point with 8-phase validation pipeline.
2. **Tier-Based Budget Enforcement**: Configurable per-tier limits on steps, patch size, files touched, and lines changed.
3. **Hash-Chained Ledger**: SHA-256 audit trail for every kernel decision with tamper detection.
4. **Hardened Executor**: Docker sandbox with non-root execution, read-only rootfs, capability dropping, and network isolation.
5. **Command Auditing**: Pre-execution syscall monitoring blocks dangerous shell patterns (rm -rf, fork bombs, etc.).
6. **Failure Escalation**: Automatic strategy rotation after 3 consecutive failures.
7. **AST Linting**: Static analysis for unreachable code and infinite loops.
8. **State-Space Search**: BFS/DFS exploration of decision trees with guard-clause checking.
9. **Fuzz-Tested Security**: Hypothesis property-based testing for path traversal and crash resistance.
10. **Data Flywheel**: Automated trajectory harvesting, DPO export.

### What's In Progress

1. **Replay Verification**: Infrastructure modules (hashing, drift scoring, snapshots, signatures) are complete; end-to-end re-execution replay is not yet implemented.
2. **Preflight Hardening Checks**: Validates security config on startup with auto-repair of env vars. Runtime drift detection is planned but not implemented.
3. **Consensus**: Single-node log operations work; multi-node Raft AppendEntries RPC is not yet implemented.
4. **SWE-bench Results**: Harness is well-built; no verified passes on real SWE-bench Lite tasks yet.

---

## What is RFSN Agent?

RFSN is a microservice-based coding agent designed for autonomous bug repair. It enforces a strict separation between **Proposal** (LLM), **Validation** (Safety Kernel), and **Execution** (Isolated Sandbox).

### Core Components

```
┌──────────┐     ┌───────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────┐
│  Learner │────▶│    LLM    │────▶│ Safety Kernel│────▶│ Tool Gateway │────▶│ Executor │
│ (advise) │     │ (propose) │     │  (validate)  │     │ (enforce)    │     │(sandboxed)│
└──────────┘     └───────────┘     └──────────────┘     └──────────────┘     └──────────┘
```

- 🔒 **Mandatory Policy Tiers** — Strict budget enforcement on patches, steps, and system resources.
- 📒 **Hash-Chained Ledger** — Audit trail for every decision, signed with SHA-256.
- 🛡️ **Preflight Hardening** — Validates security configuration on startup with env-var auto-repair.
- 🔍 **Replay Infrastructure** — Hash-based artifact comparison with drift scoring (full re-execution pending).

---

## Architecture v6

### Services

| Service | Port | Role | Status |
|---------|------|------|--------|
| **Orchestrator** | 8000 | Manages startup checks and ledger integrity. | Production |
| **LLM Service** | 8001 | Inference endpoint with cassette caching. | Production |
| **Tool Gateway** | 8002 | Policy enforcement, budget tracking, Diff-Guard. | Production |
| **Executor** | 8003 | Sandboxed runner with per-run venv isolation and command auditing. | Production |
| **Learner** | 8004 | Thompson-sampling strategy selector. | Production |
| **Replay Verifier** | — | Artifact hashing, drift scoring, snapshot comparison. | Beta |
| **Hardening Guard** | — | Preflight security config validation. | Beta |
| **Consensus** | — | Single-node Raft-lite log. | Planned |

---

## Local LLM Integration (Mac Silicon)

Optimized for **Apple Silicon (M1/M2/M3)** using MLX and Metal.

| Model Recommendation | Tier | Setup |
|----------------------|------|-------|
| **Qwen2.5-Coder-32B** | **SOTA** | `ollama run qwen2.5-coder:32b` |
| **DeepSeek-R1 (Distill)** | **Reasoning** | `ollama run deepseek-r1:32b` |
| **Llama-3.3-70B** | **Expert** | (Requires 64GB+ RAM) |

**Integration**: Set `LLM_URL` to your local endpoint (Ollama: `http://host.docker.internal:11434`) and configure the `model` flag in CLI.

---

## Security & Hardening

1. **Mandatory Service Auth**: Every microservice requires bearer token validation.
2. **Kernel-Gated Startup**: Orchestrator aborts if hardening guard detects insecure configuration.
3. **Containment**:
    - `RFSN_VENV_MODE=per_run`: Every task gets a fresh, isolated virtualenv.
    - `WARM_SANDBOX=0`: Default cold-boot ensures no cross-task pollution.
    - Non-root execution with `no-new-privileges` and `read-only` rootfs.
4. **Command Auditing**: Syscall monitor blocks dangerous patterns (rm -rf, fork bombs, mkfs) before execution.
5. **Template Boundary**: Command templates are static and validated against shell metacharacter injection.
6. **Audit Integrity**: `deps_state.json` snapshots the environment for replay.

---

## Quick Start

### 1. Hardening Bootstrap

```bash
# Verify the system state before running
python3 scripts/verify_hardening.py
```

### 2. Standalone Mode

```bash
python3 -m rfsn_swebench.cli \
    --task data/tasks/task_demo_failrepo.json \
    --proposer direct \
    --model qwen2.5-coder:32b
```

### 3. Full Stack (Composition)

```bash
# Digest-pinned build for security
docker compose up --build -d
```

### 4. Demo Mode (Orchestrator)

```bash
# Run with hardcoded demo steps for development/testing
RFSN_DEMO_MODE=1 docker compose up --build -d
```

---

## Replay Verification

To compare artifacts between runs (note: full re-execution replay is not yet implemented):

```bash
python3 -m services.replay_verifier.verify /data/run_orig /data/run_replay
```

---

## License

MIT
