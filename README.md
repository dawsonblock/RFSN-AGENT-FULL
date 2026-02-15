<div align="center">

# RFSN Agent v7.0

### Autonomous Software Engineer — Hardened · Resilient · Self-Healing

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/downloads/)
[![Code Style: Black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Status: Production](https://img.shields.io/badge/Status-Production-green)](https://github.com/dawsonblock/RFSN-AGENT-FULL)
[![Security: Hardened](https://img.shields.io/badge/Security-Hardened-critical)](https://github.com/dawsonblock/RFSN-AGENT-FULL)

[Features](#-key-features) · [Architecture](#-architecture) · [Quick Start](#-quick-start) · [Security](#-security-model) · [Verification](#-verification) · [Roadmap](#-roadmap)

</div>

---

## Overview

**RFSN (Recursive Feedback & Safety Network)** is a production-grade autonomous coding agent that solves complex software engineering tasks inside cryptographically audited, sandboxed execution capsules.

Unlike "chat-with-code" tools, RFSN is a **deterministic, policy-gated system** with a full immune response:

| Step | What happens | Module |
|:---|:---|:---|
| **Observe** | AST-aware context slicing extracts relevant code skeletons | `rfsn_swebench/locator.py` |
| **Plan** | MCTS-inspired backtracking explores solution paths | `rfsn_kernel/planner.py` |
| **Execute** | Isolated capsules enforce read-only base + ephemeral workspace | `services/executor/capsule.py` |
| **Learn** | Trajectories harvested, DPO datasets exported | `services/learner_service/` |
| **Heal** | Adaptive hardening responds to failure clusters | `rfsn_kernel/self_healing/` |

---

## Key Features

### 🧠 Cognitive Resilience

- **Frustration Detection** — Identifies infinite loops and stalled steps, auto-triggers rollback
- **MCTS Backtracking** — Explicitly prunes failed branches and explores alternatives
- **Variable Probing** — Injects ephemeral print statements to debug ambiguous failures
- **Defensive Terminal Management** — Streaming process control with strict output limits

### 🛡️ Enterprise Security

- **Indirect Injection Firewalls** — Scans all inputs for jailbreak attempts before the planner
- **Secret Scanning** — Inline SAST prevents API keys from leaking into patches
- **Drift Guard (SHH)** — Auto-repairs configuration if security settings are tampered with
- **Capsule Isolation** — Read-only base, tmpfs workspace, `--cap-drop ALL`, no network by default

### ⚡ Performance & Quality

- **Semantic Patching** — Fuzzy-matching `SEARCH/REPLACE` blocks resilient to whitespace drift
- **AST Locator** — Provides the LLM with focused code skeletons, not raw files
- **Native Prompt Caching** — Reduces latency and cost by caching prefix states across turns

### 🔄 Data Flywheel

- **Trajectory Harvesting** — Every thought, action, and result stored in `learner.duckdb`
- **DPO Export** — Successful/failed runs auto-formatted into preference datasets
- **RAG Playbooks** — Static + dynamic retrieval of historical fix patterns

### 🩺 Self-Healing Core (v7.0)

- **Signal Extraction** — Pattern-matches 12 failure types from raw logs (ImportError, Timeout, OOM, etc.)
- **Traceback Fingerprinting** — SHA-256 hash of normalized tracebacks groups structurally identical crashes
- **Failure Clustering** — Tracks recurrence rate and auto-diagnoses root causes from templates
- **Adaptive Hardening** — Stability score drives execution policy (Fast → Balanced → Hardened)

---

## Architecture

RFSN enforces a strict **Control Plane / Data Plane** separation:

```
┌─────────────────────────────────────────────────────────────────┐
│                        CONTROL PLANE                            │
│                                                                 │
│   ┌──────────┐    ┌──────────────┐    ┌───────────────────┐    │
│   │   User   │───▶│ Orchestrator │───▶│  Safety Kernel    │    │
│   │  GitHub  │    │   (Cortex)   │    │  (Plan/Risk Gate) │    │
│   └──────────┘    └──────┬───────┘    └────────┬──────────┘    │
│                          │                     │               │
│               ┌──────────▼─────────────────────▼──────────┐    │
│               │           Tool Gateway                    │    │
│               └──────────────────┬────────────────────────┘    │
│                                  │                              │
├──────────────────────────────────┼──────────────────────────────┤
│                        DATA PLANE│                              │
│                                  ▼                              │
│   ┌──────────────────────────────────────────────────────┐     │
│   │              Execution Capsule (Docker)              │     │
│   │  ┌────────────┐  ┌────────────┐  ┌───────────────┐  │     │
│   │  │ /mnt/repo  │  │ /work/repo │  │  /work/venv   │  │     │
│   │  │ (read-only)│  │  (tmpfs)   │  │   (bind)      │  │     │
│   │  └────────────┘  └────────────┘  └───────────────┘  │     │
│   │  --cap-drop ALL  --user 1000  --network none        │     │
│   └──────────────────────────────────────────────────────┘     │
│                          │                                      │
│   ┌──────────▼──────────┐    ┌────────────────────────────┐    │
│   │   Hard Ledger       │    │   Self-Healing Core        │    │
│   │  (HMAC + SHA-256    │    │  (Signals → Clusters →     │    │
│   │   hash chain)       │    │   Adaptive Hardening)      │    │
│   └─────────────────────┘    └────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

| Service | Role | Key Tech |
|:---|:---|:---|
| **Orchestrator** | The "Cortex" — manages lifecycle and decisions | FastAPI, Python 3.9+ |
| **Safety Kernel** | The "Conscience" — validates patches & enforces policy | Cryptographic Signatures |
| **Executor** | The "Hands" — runs code in Capsule isolation | Docker, `--read-only`, tmpfs |
| **Learner** | The "Memory" — stores trajectories and strategies | DuckDB, RAG Playbooks |
| **Self-Healing** | The "Immune System" — adapts hardening to stability | Sliding-window, Failure Clusters |

---

## Quick Start

### Prerequisites

- Docker (for sandboxed execution)
- Python 3.9+
- An LLM endpoint (Ollama, vLLM, or OpenAI-compatible)

### 1. Bootstrap & Verify

```bash
# Clone and install
git clone https://github.com/dawsonblock/RFSN-AGENT-FULL.git
cd RFSN-AGENT-FULL
pip install -r requirements-ci.txt

# Verify hardening
python3 scripts/verify_hardening.py
```

### 2. Run a Task

```bash
python3 -m rfsn_swebench.cli \
    --task "Fix the deadlock in connection_pool.py" \
    --repo_path $(pwd) \
    --model qwen2.5-coder:32b
```

### 3. Full Deployment

```bash
docker compose up --build -d
```

### 4. Run Test Suite

```bash
PYTHONPATH=. python3 -m pytest tests/ -v
```

---

## Security Model

RFSN v7.0 is designed for **Zero Trust** execution:

| Layer | Mechanism | Implementation |
|:---|:---|:---|
| **Filesystem** | Read-only base + ephemeral workspace | `Capsule` → `--read-only` + `tmpfs` |
| **Privileges** | Non-root, no capability escalation | `--user 1000:1000`, `--cap-drop ALL` |
| **Network** | Default deny | `--network none` |
| **Audit** | Tamper-evident hash chain | `HardLedger` → SHA-256 + HMAC |
| **Replay** | Physical deterministic snapshots | `physical.py` → env/seeds/filesystem |
| **Config** | Self-healing drift guard | `SHH` → auto-repair on tamper |

---

## Verification

**34 Phase 7 tests**, all passing:

| Phase | Component | Tests |
|:---|:---|---:|
| 7.1 | Execution Containment (Capsule) | 3 |
| 7.2 | Cryptographic Ledger Chain | 5 |
| 7.3 | Self-Healing Core | 16 |
| 7.4 | Physical Deterministic Replay | 10 |

```bash
# Run all Phase 7 tests
PYTHONPATH=. python3 -m pytest tests/test_capsule.py tests/test_ledger_chain.py \
    tests/test_self_healing.py tests/test_physical_replay.py -v
```

---

## Project Structure

```
RFSN-AGENT-FULL/
├── rfsn_kernel/            # Safety kernel, planner, risk engine
│   ├── kernel.py           #   Core decision loop
│   ├── hard_ledger.py      #   Cryptographic hash-chain audit ledger
│   ├── planner.py          #   Multi-step planning engine
│   └── self_healing/       #   Adaptive immune system (v7.0)
│       ├── core.py          #     SelfHealingCore (stability → hardening)
│       ├── signals.py       #     Failure signal extraction
│       └── memory.py        #     Failure clustering + root cause
├── services/
│   ├── executor/           # Sandboxed code execution
│   │   ├── app.py          #   FastAPI executor service
│   │   ├── capsule.py      #   Docker isolation policy (v7.0)
│   │   └── sandbox_pool.py #   Warm sandbox management
│   ├── orchestrator/       # Task lifecycle + UI
│   ├── learner_service/    # DuckDB trajectory store + DPO export
│   └── replay_verifier/    # Physical deterministic replay (v7.0)
├── rfsn_swebench/          # SWE-bench evaluation harness
├── scripts/                # Verification and utility scripts
├── tests/                  # Unit + integration tests
├── policies/               # Security policy definitions
└── docker-compose.yml      # Full deployment manifest
```

---

## Roadmap

- [x] **Phase 1–6: The Master Upgrade** — Performance, Quality, Resilience, Security, GitOps, Data Flywheel
- [x] **Phase 7: Self-Healing & Hardening** — Capsule, Ledger, Immune System, Deterministic Replay
- [ ] **Phase 8: Multi-Agent Swarm** — Architect, Coder, and QA agents collaborating
- [ ] **Phase 9: Self-Hosting CI/CD** — Agent manages its own deployment pipeline

---

## License

MIT © 2026 RFSN Project.
