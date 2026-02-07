# RFSN Agent (Kernel + Microservices) — Combined Build

This repo is a **deterministic, policy-gated coding agent** built as microservices:

- `orchestrator` (kernel gate + loop + ledger)
- `tool_gateway` (policy enforcement + budgets + step router)
- `executor` (sandboxed runner; deps stage may allow network; run stage defaults network-off)
- `llm_service` (DeepSeek v3 + cassette record/replay)

## Quick start

```bash
export DEEPSEEK_API_KEY="..."
docker compose up --build
```

Health endpoints:

- Orchestrator: `http://localhost:8000/health`
- LLM service: `http://localhost:8001/health`
- Tool gateway: `http://localhost:8002/health`
- Executor: `http://localhost:8003/health`

Run:

```bash
curl -s http://localhost:8000/run \
  -H 'Content-Type: application/json' \
  -d '{"repo_id":"demo1","task":"Fix failing pytest. Minimal diff. Do not change deps.","max_iters":3,"scenario":"one_fail_one_fix"}'
```

## Determinism

Set:

```bash
export RFSN_SEED=1
export LEDGER_FIXED_TS=1.0
```

Use cassette replay (recommended for CI) via `policies/llm_cassette.yaml`.

## Full Stack Wiring

### Prerequisites

1. **Build the blessed sandbox image** (required by the executor):

```bash
docker build -t rfsn-blessed:0.2 -f blessed.Dockerfile .
# or via compose profile:
docker compose --profile build-blessed build blessed
```

1. **Prepare a fixture repo** into `data/repos/<repo_id>/`:

```bash
./scripts/setup_fixture.sh demo_failrepo
```

1. **Start the stack**:

```bash
export DEEPSEEK_API_KEY="..."
docker compose up --build -d
```

1. **Smoke test**:

```bash
./scripts/smoke_test.sh demo_failrepo
```

### Service Endpoints

| Service | Port | Endpoints |
| --------- | ------ | ----------- |
| Orchestrator | 8000 | `GET /health`, `POST /run` |
| LLM Service | 8001 | `GET /health`, `POST /chat` |
| Tool Gateway | 8002 | `GET /health`, `POST /run_step` |
| Executor | 8003 | `GET /health`, `POST /run` |

### Orchestrator `POST /run`

```json
{
  "repo_id": "demo_failrepo",
  "task": "Fix the failing test. Minimal diff only.",
  "max_iters": 3,
  "scenario": "smoke_test"
}
```

### Tool Gateway `POST /run_step`

Routes through policy enforcement (step type allowlist, path validation,
per-iteration budgets, diff-guard) before forwarding to the executor.

```json
{
  "repo_id": "demo_failrepo",
  "iter": 1,
  "step": {
    "id": "step_001",
    "type": "run_tests",
    "template_id": "pytest_targeted",
    "template_params": {"target": "tests/test_demo.py::test_add"},
    "timeout_s": 120
  }
}
```

**Step types**: `ensure_deps`, `repo_search`, `repo_read_range`, `apply_patch`, `run_tests`

### Data Directory Layout

```text
data/
├── repos/<repo_id>/       # Repo snapshots (git init'd)
├── venv/<repo_id>/        # Per-repo virtualenvs (created by ensure_deps)
├── wheels/<repo_id>/      # Pip cache for --require-hashes
├── artifacts/<repo_id>/   # Step outputs
└── cassettes/             # LLM cassette JSONL files
```

### Cassette Replay

Set `policies/llm_cassette.yaml` mode to `replay` for deterministic CI.
Cassettes are keyed by SHA-256 of the canonical request payload and stored
per-repo per-scenario as JSONL in `data/cassettes/`.

## Notes

- This build is a coherent, runnable skeleton with policy gates, determinism hooks, tests, CI, and fixtures.
- You still need to wire your own repo snapshotting logic and any additional step types you want.

---

## SWE-bench Bench Runner (`rfsn_swebench`)

A standalone, deterministic **patch → test → iterate** harness that produces
SWE-bench-compatible outputs (unified diff + test logs + verdict).  It can run
independently or delegate to the full RFSN microservice stack.

### Quick start (bench runner)

```bash
# 1. Create a task definition
cat > task.json <<'EOF'
{
  "task_id": "demo-001",
  "repo_url": "https://github.com/org/repo.git",
  "repo_ref": "main",
  "workdir": "/tmp/bench_demo",
  "issue_text": "The add() function returns wrong results",
  "hints": {
    "failing_tests": ["tests/test_demo.py::test_add"]
  },
  "commands": {
    "setup": ["pip install -e ."],
    "test_quick": "pytest -q",
    "test_full": "pytest -q"
  },
  "limits": {
    "max_iters": 8,
    "max_patch_bytes": 250000,
    "max_files_touched": 25,
    "max_new_files": 5,
    "max_runtime_sec": 1800
  }
}
EOF

# 2. Run with direct DeepSeek proposer (standalone, no services needed)
export DEEPSEEK_API_KEY="..."
python -m rfsn_swebench.cli \
    --task task.json \
    --out result.json \
    --proposer direct

# 3. Or run via the full RFSN Orchestrator stack
docker compose up -d
python -m rfsn_swebench.cli \
    --task task.json \
    --out result.json \
    --proposer orchestrator \
    --orchestrator-url http://localhost:8000

# 4. Or run as a Docker Compose profile
docker compose --profile bench run rfsn_swebench \
    --task /data/task.json --out /data/result.json
```

### Proposer modes

| Flag | Description |
| ------ | ------------- |
| `--proposer direct` | Calls DeepSeek API directly (requires `DEEPSEEK_API_KEY`). Standalone, no services needed. |
| `--proposer orchestrator` | Delegates to the RFSN Orchestrator `/run` endpoint. Requires the full stack. |
| `--proposer placeholder` | Aborts immediately. Useful for testing the harness itself. |
| *(auto-detect)* | If `--orchestrator-url` is set → orchestrator. If `DEEPSEEK_API_KEY` is set → direct. Otherwise → placeholder. |

### Executor bridge

By default, tests run locally via `subprocess`.  Pass `--executor-url` to
route test execution through the RFSN Executor service (Docker-sandboxed,
venv-managed, network-disabled).  Add `--gateway-url` to route through the
Tool Gateway for full policy enforcement:

```bash
# Direct to executor (no policy enforcement)
python -m rfsn_swebench.cli \
    --task task.json --out result.json \
    --proposer direct \
    --executor-url http://localhost:8003

# Through tool gateway (recommended — gets budgets + diff-guard)
python -m rfsn_swebench.cli \
    --task task.json --out result.json \
    --proposer direct \
    --executor-url http://localhost:8003 \
    --gateway-url http://localhost:8002
```

### Outputs

- **`result.json`** — SWE-bench-compatible verdict (`PASS` / `FAIL` / `ABORT`).
- **Replay directory** — `<replay-base>/replays/<task_id>_<timestamp>/`
  - `events.jsonl` — structured event log (task start, proposals, test results, risk decisions).
  - `blobs/` — content-addressed patches, stdout/stderr tails per iteration.

### Risk gating

Patches are evaluated by `rfsn_swebench.gate.patch_risk_gate()` which blocks:

- Oversized patches (bytes, files, new files, added/deleted lines)
- Edits to CI/CD configs or dependency manifests
- Banned code patterns (`pytest.skip`, `xfail`, `@skip`, etc.)
- Large test deletions (>50 lines)

Limits are loaded from `policies/diff_guard.yaml` and `policies/tool_allowlist.yaml`
when available, falling back to sensible defaults.
