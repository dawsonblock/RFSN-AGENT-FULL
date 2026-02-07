<p align="center">
  <h1 align="center">🔧 RFSN Agent</h1>
  <p align="center">
    <strong>Deterministic, policy-gated code repair agent</strong><br>
    Powered by DeepSeek R1 · SWE-bench compatible · Microservice architecture
  </p>
  <p align="center">
    <img src="https://img.shields.io/badge/python-3.9+-blue?logo=python&logoColor=white" alt="Python 3.9+">
    <img src="https://img.shields.io/badge/LLM-DeepSeek_R1-orange?logo=openai&logoColor=white" alt="DeepSeek R1">
    <img src="https://img.shields.io/badge/docker-compose-2496ED?logo=docker&logoColor=white" alt="Docker Compose">
    <img src="https://img.shields.io/badge/lint-0_errors-brightgreen" alt="0 lint errors">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
  </p>
</p>

---

RFSN Agent is an autonomous code repair pipeline that reads a bug report,
proposes a minimal unified-diff patch via LLM, applies it, runs tests, and
iterates — all behind a **deterministic safety gate** that enforces strict
policies on what the LLM is allowed to touch.

It ships two execution modes:

| Mode | What it does |
|------|--------------|
| **Standalone** (`rfsn_swebench`) | Single-binary bench runner. Clone → patch → test → verdict. No services needed. |
| **Microservices** (`docker compose`) | Full stack with Orchestrator, Kernel Gate, Tool Gateway, Executor, Learner, and LLM Service. |

---

## ✅ Demo Results

Both demos run with **DeepSeek R1** (`deepseek-reasoner`) — zero retries, first-iteration PASS:

### Demo 1 — Bug Fix

> `add(a, b)` returns `a + b + 1` (off-by-one). Test expects `3`, gets `4`.

```diff
 def add(a: int, b: int) -> int:
-    return a + b + 1
+    return a + b
```

**Result:** `PASS` · 1 iteration · 0.6s quick tests · risk: `ALLOW`

### Demo 2 — Defensive Code

> `divide(a, b)` raises `ZeroDivisionError`. Test expects `float('inf')`.

```diff
 def divide(a: int, b: int) -> float:
+    if b == 0:
+        return float('inf')
     return a / b
```

**Result:** `PASS` · 1 iteration · 0.7s quick tests · risk: `ALLOW`

### SWE-bench — SymPy #11400

> `ccode(sinc(x))` doesn't work — should emit C piecewise for `sin(x)/x`.

**Result:** `PASS` · patches `sympy/printing/ccode.py` with `_print_sinc` + relational operators

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     rfsn_swebench CLI                       │
│              (standalone bench runner mode)                  │
│  clone → propose (DeepSeek R1) → apply → gate → test       │
└─────────────────────────────────────────────────────────────┘

┌─────────────────── Microservices Mode ──────────────────────┐
│                                                             │
│  ┌───────────┐    ┌──────────┐    ┌──────────┐             │
│  │  Learner  │───▶│  Orch.   │───▶│   LLM    │             │
│  │ (Thompson │    │ (Kernel  │    │ Service  │             │
│  │ Sampling) │    │  Gate)   │    │(DeepSeek)│             │
│  └───────────┘    └────┬─────┘    └──────────┘             │
│                        │                                    │
│                   ┌────▼─────┐    ┌──────────┐             │
│                   │   Tool   │───▶│ Executor │             │
│                   │ Gateway  │    │(Sandbox) │             │
│                   │ (Policy) │    │(No Net)  │             │
│                   └──────────┘    └──────────┘             │
│                                                             │
│  Every step → Kernel Gate (final authority) → Ledger        │
└─────────────────────────────────────────────────────────────┘
```

### Key Components

| Component | Location | Purpose |
|-----------|----------|---------|
| **Kernel Gate** | `services/orchestrator/kernel.py` | Final authority over every step. Schema validation, step-type allowlist, per-type budgets, path safety, risk scoring, banned patterns. |
| **Learner** | `services/learner_service/` | Thompson-sampling strategy selector (S1–S5). DuckDB-backed. Advisory only. |
| **Orchestrator** | `services/orchestrator/app.py` | LLM loop, step execution, learner integration. All steps gate-validated. |
| **Tool Gateway** | `services/tool_gateway/` | Defense-in-depth policy layer. Path validation, per-iteration budgets, diff-guard. |
| **Executor** | `services/executor/` | Docker-sandboxed runner. Network disabled during test execution. |
| **LLM Service** | `services/llm_service/` | DeepSeek proxy with cassette record/replay for deterministic CI. |
| **Bench Runner** | `rfsn_swebench/` | Standalone CLI. SWE-bench compatible task format, replay artifacts, risk gating. |

---

## 🚀 Quick Start

### Standalone Mode (no Docker needed)

```bash
# 1. Install dependencies
pip install -r requirements-bench.txt

# 2. Set your API key
export DEEPSEEK_API_KEY="sk-..."

# 3. Run a demo task
python -m rfsn_swebench.cli \
    --task data/tasks/task_demo_failrepo.json \
    --out result.json \
    --proposer direct \
    --model deepseek-reasoner
```

### Microservices Mode (Docker Compose)

```bash
# 1. Set your API key
export DEEPSEEK_API_KEY="sk-..."

# 2. Start all services
docker compose up --build -d

# 3. Submit a repair job
curl -s http://localhost:8000/run \
  -H 'Content-Type: application/json' \
  -d '{
    "repo_id": "demo_failrepo",
    "task": "Fix the failing test. Minimal diff only.",
    "max_iters": 3,
    "scenario": "demo"
  }'
```

### Run a SWE-bench Task

```bash
python -m rfsn_swebench.cli \
    --task data/tasks/task_sympy__sympy-11400.json \
    --out data/results/result_sympy__sympy-11400.json \
    --proposer direct \
    --model deepseek-reasoner
```

---

## 📁 Project Structure

```
rfsn-agent/
├── rfsn_swebench/              # Standalone bench runner
│   ├── cli.py                  #   CLI entry-point & proposers
│   ├── runner.py               #   Core propose → apply → test loop
│   ├── contracts.py            #   Dataclasses (BenchTask, BenchResult, …)
│   ├── gate.py                 #   Patch risk gating
│   ├── patcher.py              #   Unified diff application
│   ├── repo.py                 #   Git ops (clone, checkout, diff, reset)
│   ├── replay.py               #   Replay artifact management
│   ├── testsel.py              #   Test selection heuristics
│   └── util.py                 #   Shell execution, I/O helpers
│
├── services/
│   ├── orchestrator/           # Core agent loop
│   │   ├── app.py              #   FastAPI /run endpoint
│   │   ├── kernel.py           #   Deterministic safety gate
│   │   ├── ledger.py           #   Hash-chained audit ledger
│   │   └── prompts.py          #   System/user prompt templates
│   ├── learner_service/        # Strategy selector
│   │   ├── app.py              #   FastAPI /suggest, /ingest
│   │   └── store_duckdb.py     #   DuckDB persistence layer
│   ├── tool_gateway/           # Policy enforcement
│   │   ├── app.py              #   FastAPI /run_step
│   │   └── policy.py           #   Path & budget validation
│   ├── executor/               # Sandboxed runner
│   │   └── app.py              #   FastAPI /run
│   └── llm_service/            # LLM proxy + cassettes
│       └── app.py              #   FastAPI /chat
│
├── policies/                   # Declarative policy configs (YAML)
│   ├── gate_policy.yaml        #   Kernel gate: budgets, risk thresholds
│   ├── tool_allowlist.yaml     #   Allowed step types
│   ├── diff_guard.yaml         #   Patch size/content limits
│   ├── deps_policy.yaml        #   Dependency installation policy
│   ├── test_policy.yaml        #   Test execution policy
│   ├── command_templates.yaml  #   Step command templates
│   └── llm_cassette.yaml       #   LLM record/replay config
│
├── fixtures/                   # Demo repos for testing
│   ├── demo_failrepo/          #   Off-by-one bug (add returns a+b+1)
│   ├── demo_refactorbait/      #   Missing zero-division guard
│   ├── demo1/                  #   Passing baseline
│   └── demo_netoff/            #   Network isolation test
│
├── data/
│   ├── tasks/                  #   Task definitions (JSON)
│   └── results/                #   Run results (JSON)
│
├── tests/                      # Test suite
│   ├── test_kernel_step_fuzz.py    # Gate validation + budget tests
│   ├── test_policy_fuzz.py         # Tool gateway policy fuzz
│   ├── test_bench_contracts.py     # Contract dataclass tests
│   ├── test_bench_gate.py          # Risk gate tests
│   └── test_bench_runner.py        # Runner integration tests
│
├── scripts/                    # Automation scripts
│   ├── run_one_task.sh         #   Run a single SWE-bench task
│   ├── run_swebench_batch.py   #   Batch runner for SWE-bench
│   ├── score_swebench.py       #   Score SWE-bench results
│   ├── smoke_test.sh           #   End-to-end smoke test
│   └── setup_fixture.sh        #   Prepare fixture repos
│
├── shared/                     # Shared schemas & utilities
│   ├── bundle_schema.json      #   JSON Schema for step bundles
│   ├── task_schema.json        #   JSON Schema for task definitions
│   └── auth.py                 #   Shared auth middleware
│
├── docker-compose.yml          # Full stack orchestration (6 services)
├── requirements-bench.txt      # Standalone runner dependencies
└── requirements-ci.txt         # CI/test dependencies
```

---

## 🛡️ Safety & Policy

The kernel gate is the **final authority** over every step that executes. No step bypasses it.

### Gate Enforcement

| Check | What it does |
|-------|-------------|
| **Schema validation** | Every step must match the bundle JSON schema |
| **Step-type allowlist** | Only `repo_search`, `repo_read_range`, `apply_patch`, `ensure_deps`, `run_tests` |
| **Per-type budgets** | search ≤ 4 · read ≤ 6 · patch ≤ 2 · deps ≤ 1 · tests ≤ 4 per bundle |
| **Path safety** | Blocks `../` traversal, absolute paths, null bytes, `.git/`, `.env`, `.pem` |
| **Read line cap** | Max 300 lines per `repo_read_range` |
| **Timeout clamping** | Per-type maximums enforced regardless of LLM request |
| **Banned patterns** | 12 patterns: `pytest.skip`, `xfail`, `subprocess.call`, `eval(`, `os.system`, … |
| **Risk scoring** | CI edits +30 · deps +20 · tests +25 · large diffs +10 — rejects at ≥ 60 |
| **Bundle size cap** | Max 15 steps per bundle |
| **Forbidden edits** | CI configs, dependency manifests, test files — all blocked |

### Defense in Depth

```
LLM proposes → Kernel Gate (final authority) → Tool Gateway (2nd layer) → Executor (sandbox)
```

The tool gateway independently enforces its own policy as a second enforcement layer.

---

## 🎓 Learner Service

The learner uses **Thompson sampling** over 5 repair strategies:

| ID | Strategy | Description |
|----|----------|-------------|
| S1 | Focused patch | Minimal single-file fix |
| S2 | Multi-file patch | Cross-module repair |
| S3 | Test-guided | Run tests first, then fix |
| S4 | Search-first | Search codebase, then patch |
| S5 | Defensive | Add guards and error handling |

The learner is **advisory only** — the kernel gate always has final say. Strategy posteriors are persisted in DuckDB and updated via Bayesian inference after each episode.

---

## 📋 Task Format

Tasks use a JSON schema compatible with [SWE-bench](https://github.com/princeton-nlp/SWE-bench):

```json
{
  "task_id": "demo_failrepo",
  "repo_url": "https://github.com/org/repo.git",
  "workdir": "/tmp/work/demo",
  "issue_text": "The add() function returns wrong results...",
  "hints": {
    "failing_tests": ["tests/test_demo.py::test_add"],
    "focus_files": ["src/demo.py"],
    "test_patch": ""
  },
  "commands": {
    "setup": ["pip install -e ."],
    "test_quick": "pytest -x -q tests/",
    "test_full": "pytest -q"
  },
  "limits": {
    "max_iters": 3,
    "max_patch_bytes": 50000,
    "max_files_touched": 5,
    "max_new_files": 0,
    "max_runtime_sec": 300
  }
}
```

---

## 🔁 Replay & Reproducibility

Every run produces a replay directory with content-addressed artifacts:

```
replays/<task_id>_<timestamp>/
├── events.jsonl          # Structured event log (JSON lines)
└── blobs/
    ├── proposal_iter1.*  # Raw LLM proposal (content-hashed filename)
    ├── applied_iter1.*   # Actual git diff applied
    ├── quick_stdout_*    # pytest stdout per iteration
    └── quick_stderr_*    # pytest stderr per iteration
```

For **deterministic CI replay**:

```bash
export RFSN_SEED=1
export LEDGER_FIXED_TS=1.0
# Set policies/llm_cassette.yaml mode: replay
```

---

## 🔌 Service Endpoints

| Service | Port | Endpoints |
|---------|------|-----------|
| Orchestrator | 8000 | `GET /health` · `POST /run` |
| LLM Service | 8001 | `GET /health` · `POST /chat` |
| Tool Gateway | 8002 | `GET /health` · `POST /run_step` |
| Executor | 8003 | `GET /health` · `POST /run` |
| Learner | 8004 | `GET /health` · `POST /suggest` · `POST /ingest` |

---

## 🧪 Testing

```bash
# Run all tests
python -m pytest tests/ -q

# Lint check (0 errors across entire project)
python -m flake8 --max-line-length 79

# Type check (0 errors)
python -m pyright
```

---

## 📊 CLI Reference

```
python -m rfsn_swebench.cli [OPTIONS]

Required:
  --task PATH             Task JSON definition
  --out PATH              Result JSON output

Proposer:
  --proposer MODE         direct | orchestrator | placeholder (auto-detected)
  --model NAME            LLM model name (default: deepseek-chat)
  --base-url URL          LLM API base URL (default: https://api.deepseek.com)
  --api-key KEY           API key (default: $DEEPSEEK_API_KEY)

Services:
  --orchestrator-url URL  RFSN Orchestrator endpoint
  --executor-url URL      RFSN Executor endpoint
  --gateway-url URL       Tool Gateway endpoint

Output:
  --replay-base DIR       Replay artifact directory
  --ledger-path PATH      Hash-chained ledger JSONL file
  --data-dir DIR          Shared data directory (default: /data)
  --scenario TAG          Scenario tag for cassette system
```

---

## 📜 License

MIT
