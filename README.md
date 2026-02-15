<p align="center">
  <strong>RFSN Agent — Hardened v6</strong><br>
  <em>Deterministic, policy-gated coding agent with self-healing security and upstream learning</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.9%2B-blue" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/OS-Apple_Silicon_Native-black" alt="Mac Silicon">
  <img src="https://img.shields.io/badge/Security-Hardened_v6-red" alt="Hardened v6">
  <img src="https://img.shields.io/badge/Status-Verified_Passed-green" alt="Verified Passed">
</p>

---

## Master Upgrade (Phase 1-6 Complete)

**RFSN v6.4** introduces a comprehensive suite of enhancements:

1. **Performance**: Native Prompt Caching, Parallel Speculative Execution.
2. **Code Quality**: AST-Aware Context Slicing, Semantic Patching.
3. **Resilience**: MCTS Backtracking, Frustration Detection, Anti-Looping.
4. **Security**: Indirect Prompt Injection Firewalls, Secret Scanning.
5. **GitOps**: Native GitHub App, Confidence-Triggered HITL.
6. **Data Flywheel**: Automated Trajectory Harvesting, DPO Export.

---

## What is RFSN Agent?

RFSN is a **production-hardened**, microservice-based coding agent designed for autonomous bug repair. It enforces a strict separation between **Proposal** (LLM), **Validation** (Safety Kernel), and **Execution** (Isolated Sandbox).

### Core Components

```
┌──────────┐     ┌───────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────┐
│  Learner │────▶│    LLM    │────▶│ Safety Kernel│────▶│ Tool Gateway │────▶│ Executor │
│ (advise) │     │ (propose) │     │  (SHH/DRV)   │     │ (Enforce)    │     │ (Sandboxed)│
└──────────┘     └───────────┘     └──────────────┘     └──────────────┘     └──────────┘
```

- 🛡️ **Self-Healing Hardening (SHH)** — Continuous detection and repair of configuration drift.
- 🧬 **Deterministic Replay (DRV)** — Cryptographic verification of run equivalence across environments.
- 🔒 **Mandatory Policy Tiers** — Strict budget enforcement on patches, steps, and system resources.
- 📒 **Hash-Chained Ledger** — Audit-trail for every decision, signed with SHA-256.

---

## Architecture v6

### Services

| Service | Port | Role |
|---------|------|------|
| **Orchestrator** | 8000 | The "Cortex". Manages SHH startup checks and ledger integrity. |
| **LLM Service** | 8001 | Inference endpoint with cassette persistence (caching). |
| **Tool Gateway** | 8002 | Policy enforcement, budget tracking, and Diff-Guard. |
| **Executor** | 8003 | Sandboxed runner with per-run Venv isolation and non-root containment. |
| **Learner** | 8004 | Thompson-sampling strategy selector for repo-specific optimization. |

### New Reliability Modules

- **[SHH](services/hardening_guard/)**: Automatically restores security settings (auth, sandboxing) if they drift.
- **[DRV](services/replay_verifier/)**: Verifies that a replayed run produces bit-identical artifacts and traces.
- **[PST Map](docs/perf_security_map.md)**: Allows dialing Performance vs Security without breaking the safety shell.

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

## Security & Hardening Deep-Dive

RFSN v6 has undergone a comprehensive hardening pass:

1. **Mandatory Service Auth**: Every microservice requires bearer token validation.
2. **Kernel-Gated Startup**: Orchestrator aborts if SHH guard detects insecure configuration.
3. **Containment**:
    - `RFSN_VENV_MODE=per_run`: Every task gets a fresh, isolated virtualenv.
    - `WARM_SANDBOX=0`: Default cold-boot ensures no cross-task pollution.
    - Non-root execution with `no-new-privileges` and `read-only` rootfs.
4. **Template Boundary**: Command templates are static and validated against shell metacharacter injection.
5. **Audit Integrity**: `deps_state.json` accurately snapshots the environment for replay.

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

---

## Replay Verification (DRV)

To verify a run against a previously recorded bundle:

```bash
python3 -m services.replay_verifier.verify /data/run_orig /data/run_replay
```

---

## License

MIT
