<p align="center">
  <strong>RFSN Agent</strong><br>
  <em>Deterministic, policy-gated coding agent with upstream learning</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.9%2B-blue" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
  <img src="https://img.shields.io/badge/LLM-DeepSeek%20R1-orange" alt="DeepSeek R1">
  <img src="https://img.shields.io/badge/gate-deterministic-red" alt="Deterministic Gate">
</p>

---

## What is RFSN Agent?

A **microservice-based coding agent** that fixes bugs autonomously. It separates
*proposing* code changes (LLM) from *validating* them (deterministic kernel gate)
and *executing* them (sandboxed container), with an upstream **Thompson-sampling
learner** that improves strategy selection over time.

```
┌──────────┐     ┌───────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────┐
│  Learner │────▶│    LLM    │────▶│ Kernel Gate  │────▶│ Tool Gateway │────▶│ Executor │
│ (advise) │     │ (propose) │     │  (validate)  │     │  (enforce)   │     │  (run)   │
└──────────┘     └───────────┘     └──────────────┘     └──────────────┘     └──────────┘
     ▲                                    │                                       │
     └────────────── outcome ─────────────┴───────────────────────────────────────┘
```

**Key properties:**
- 🔒 **Deterministic gate** — every step is validated against hard policy before execution
- 🧠 **Context-aware learner** — derives repo fingerprint (language, framework, failure class) for per-context strategy selection
- 🎯 **Multi-proposal search** — generates N candidate patches per iteration, picks lowest-risk valid plan
- 📒 **Hash-chained ledger** — every decision is SHA-256 chained for replay auditing
- 🐳 **Sandboxed execution** — patches applied and tests run in isolated Docker containers

---

## Architecture

### Services (5)

| Service | Port | Role |
|---------|------|------|
| **Orchestrator** | 8000 | Run loop: learner → LLM → gate → execute → ledger |
| **LLM Service** | 8001 | DeepSeek R1 chat endpoint with cassette record/replay |
| **Tool Gateway** | 8002 | Policy enforcement, per-iteration budgets, diff-guard |
| **Executor** | 8003 | Sandboxed step runner (Docker, non-root, network-off) |
| **Learner** | 8004 | Thompson-sampling strategy selector (DuckDB-backed) |

### Execution Model (per iteration)

1. **Learner suggests** strategy + constraints (context-aware: language, framework, failure class)
2. **LLM proposes** N candidate bundles of steps (multi-proposal search)
3. **Kernel gate validates** each candidate:
   - Schema validation, step-type allowlist, content bans
   - Patch size/file budgets, CI/test/dep edit forbids
   - Per-step-type budgets (counts, timeouts, read line caps)
   - Read path safety (traversal, blocked prefixes/suffixes)
   - Risk scoring → reject if ≥ threshold
   - Test enforcement + ordering (targeted before suite)
4. **Lowest-risk valid plan** is selected and executed
5. **Executor** runs approved steps in sandboxed container
6. **Learner ingests** outcome with deterministic failure signature

### Policy Files

| File | Purpose |
|------|---------|
| `policies/gate_policy.yaml` | **Single source of truth**: patch budgets, step budgets, risk threshold, read blocklists, test enforcement |
| `policies/tool_allowlist.yaml` | Allowed step types, path globs, byte limits |
| `policies/diff_guard.yaml` | Diff-level guards (blocked dep files, max changed files/lines) |
| `policies/deps_policy.yaml` | Dependency installation policy |
| `policies/test_policy.yaml` | Test execution policy (static analysis templates) |

---

## Quick Start

### Standalone (no Docker, no services)

Run the bench harness directly with DeepSeek R1:

```bash
export DEEPSEEK_API_KEY="sk-..."

# Run on a demo fixture (off-by-one bug fix)
python -m rfsn_swebench.cli \
    --task data/tasks/task_demo_failrepo.json \
    --out data/results/result_demo_failrepo.json \
    --proposer direct \
    --model deepseek-reasoner
```

### Full Microservice Stack

```bash
# 1. Build the blessed sandbox image
docker compose --profile build-blessed build blessed

# 2. Resolve and export digest-pinned image ref (required in strict mode)
export BLESSED_IMAGE="$(docker image inspect ${BLESSED_BUILD_TAG:-rfsn-blessed:0.2} --format '{{index .RepoDigests 0}}')"
export RFSN_STRICT_IMAGE_DIGEST=1

# 3. Start all services
export DEEPSEEK_API_KEY="sk-..."
docker compose up --build -d

# 4. Health check
curl -s -H "Authorization: Bearer ${RFSN_SERVICE_TOKEN}" \
  http://localhost:8000/health | python3 -m json.tool

# 5. Open UI dashboard
#    http://localhost:8000/ui
#    (enter RFSN_SERVICE_TOKEN in the UI to run API actions)

# 6. Run a task via API
curl -s http://localhost:8000/run \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${RFSN_SERVICE_TOKEN}" \
  -d '{
    "repo_id": "demo_failrepo",
    "task": "Fix the failing test. Minimal diff only.",
    "max_iters": 3,
    "scenario": "smoke"
  }'
```

### SWE-bench Mode

```bash
# Direct proposer (standalone)
python -m rfsn_swebench.cli \
    --task data/tasks/task_sympy__sympy-11400.json \
    --out data/results/result_sympy__sympy-11400.json \
    --proposer direct \
    --model deepseek-reasoner

# Via full orchestrator stack
python -m rfsn_swebench.cli \
    --task data/tasks/task_sympy__sympy-11400.json \
    --out result.json \
    --proposer orchestrator \
    --orchestrator-url http://localhost:8000

# Docker Compose bench profile
docker compose --profile bench run rfsn_swebench \
    --task /data/tasks/task_sympy__sympy-11400.json \
    --out /data/results/result.json
```

---

## Demo Fixtures

| Fixture | Bug | What the agent does |
|---------|-----|---------------------|
| `demo_failrepo` | `add(a, b)` returns `a + b + 1` (off-by-one) | Removes the `+ 1` → PASS iter 1 |
| `demo_refactorbait` | `divide(a, b)` raises `ZeroDivisionError` | Adds `if b == 0: return float('inf')` guard → PASS iter 1 |

Both pass in **1 iteration, 0 retries** with DeepSeek R1.

---

## Determinism

```bash
export RFSN_SEED=1
export LEDGER_FIXED_TS=1.0
```

The ledger hash-chains every event with SHA-256 (prev_hash + entry_hash → chain_hash).
Gate decisions, step results, and learner suggestions are all recorded for replay auditing.
Use cassette replay mode for fully deterministic CI.

---

## Project Structure

```
├── services/
│   ├── orchestrator/         # Run loop + hard-kernel integration
│   │   ├── app.py            # FastAPI orchestrator (multi-proposal, context-aware)
│   │   ├── context_fingerprint.py  # Repo + failure fingerprinting
│   │   ├── phase_tracker.py  # Step-phase state tracker
│   │   └── prompts.py        # LLM prompt templates
│   ├── llm_service/          # DeepSeek API wrapper + cassette system
│   ├── tool_gateway/         # Policy enforcement + budget tracking
│   ├── executor/             # Docker-sandboxed step runner
│   └── learner_service/      # Thompson-sampling strategy selector (DuckDB)
├── rfsn_kernel/              # HardKernel gate + hard ledger + replay + tier policy
├── rfsn_swebench/            # Standalone SWE-bench bench runner
│   ├── cli.py                # CLI entry point (3 proposer modes)
│   ├── runner.py             # Core propose → apply → gate → test loop
│   ├── contracts.py          # Data contracts (BenchTask, BenchResult, etc.)
│   ├── gate.py               # Standalone patch risk gate
│   ├── patcher.py            # Unified diff applicator
│   ├── repo.py               # Git operations (clone, checkout, diff, reset)
│   ├── replay.py             # Replay artifact management
│   └── testsel.py            # Test selection heuristics
├── policies/                 # All policy YAML files
├── fixtures/                 # Demo repos for testing
├── data/
│   ├── tasks/                # SWE-bench task definitions
│   └── results/              # Run results
├── shared/                   # JSON schemas
├── tests/                    # Property-based fuzz tests
├── scripts/                  # Bootstrap and CI scripts
└── docker-compose.yml        # Full stack orchestration
```

---

## Gate Policy (what gets enforced)

The hard kernel at `rfsn_kernel/kernel.py` is the **final authority** over
every step that executes. No step bypasses it.

| Check | Detail |
|-------|--------|
| **Execution path** | All side-effecting actions route through `HardKernel.kernel_step(...)` |
| **State machine** | Normalize → Validate → Simulate → Risk → Decide → Execute → Verify → Commit |
| **Tier policy** | Per-run deterministic tier gate from `policies/gate_policy_tiers.yaml` |
| **Risk gate** | Rejects loop/drift/high-risk actions before execution |
| **Simulation** | Predicts success/cost/loop/drift before execution |
| **Tool policy** | `tool_gateway` enforces allowlists, path guards, and patch limits |
| **Replay** | Hash-chained hard ledger supports deterministic replay verification |

---

## Learner

The learner service (`services/learner_service/`) uses **Thompson sampling** over
5 strategy arms:

| Strategy | Approach |
|----------|----------|
| S1 | Search → narrow reads → minimal patch |
| S2 | Patch immediately, targeted tests first |
| S3 | Error-signature driven (stacktrace → code) |
| S4 | Dependency/install first, then patch |
| S5 | Forbid refactor, surgical fix only |

**Context-aware**: derives `lang|framework|tests|failure_class` from repo files
and failure text, so it learns per-context preferences (e.g., Django ImportErrors
get different strategy weights than SymPy AssertionErrors).

Backed by DuckDB at `/data/learner.duckdb` with `strategy_stats` and `episodes`
tables.

---

## Proposer Modes

| Mode | Flag | Description |
|------|------|-------------|
| **Direct** | `--proposer direct` | Calls DeepSeek API directly. Standalone, no services. |
| **Orchestrator** | `--proposer orchestrator` | Full RFSN stack via `/run` endpoint. |
| **Placeholder** | `--proposer placeholder` | Aborts immediately (harness testing). |
| **Auto-detect** | *(default)* | Orchestrator if URL set, direct if API key set. |

---

## Outputs

- **Result JSON** — SWE-bench compatible: `PASS` / `FAIL` / `ABORT` + patch + test logs
- **Replay directory** — `events.jsonl` (structured log) + `blobs/` (content-addressed patches, stdout/stderr)
- **Ledger** — Hash-chained JSONL at `/data/kernel_ledger.jsonl`
- **DuckDB** — Learner statistics at `/data/learner.duckdb`

---

## Development

```bash
# Install dev dependencies
pip install -r requirements-ci.txt

# Run fuzz tests
python -m pytest tests/ -v

# Lint
flake8 --max-line-length 79
pyright
```

---

## License

MIT
