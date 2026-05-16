# RUN_LOCAL_TOY_REPAIR.md

## Running a Local Toy Repair

> ⚠️ **Warning: Local dev mode is for trusted repos only.**
> Do not run against untrusted code.

---

## Prerequisites

```bash
pip install pytest
cd /path/to/RFSN-AGENT-FULL
```

---

## Step 1 — Create a Toy Repo

```bash
mkdir -p /tmp/toy_repo/src
cat > /tmp/toy_repo/src/utils.py << 'EOF'
def add(a, b):
    return a - b  # bug: should be a + b
EOF

mkdir -p /tmp/toy_repo/tests
cat > /tmp/toy_repo/tests/test_utils.py << 'EOF'
from src.utils import add

def test_add():
    assert add(2, 3) == 5
EOF
```

---

## Step 2 — Verify the Bug

```bash
cd /tmp/toy_repo
python -m pytest tests/test_utils.py -q
# Expected: FAILED (assert -1 == 5)
```

---

## Step 3 — Write a Manual Plan

Create `/tmp/toy_plan.json`:

```json
[
  {
    "type": "read_file",
    "path": "src/utils.py",
    "intent": "Read the buggy file"
  },
  {
    "type": "apply_patch",
    "patch": "--- a/src/utils.py\n+++ b/src/utils.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b  # bug: should be a + b\n+    return a + b\n",
    "intent": "Fix the subtraction bug"
  }
]
```

---

## Step 4 — Set Environment Variables

```bash
export RFSN_SANDBOX_MODE=local_dev
export RFSN_ALLOW_LOCAL_EXEC=1
export RFSN_DEV_MODE=1
export RFSN_EXEC_USE_DOCKER=0
export RFSN_PATCH_GATE_REQUIRED=0   # relaxed for local dev only
export RFSN_AUTH_REQUIRED=0         # relaxed for local dev only
```

---

## Step 5 — Run via Python

```python
# /tmp/run_toy.py
import sys, json
sys.path.insert(0, "/path/to/RFSN-AGENT-FULL")

from rfsn_kernel.kernel import HardKernel
from services.orchestrator.run_engine import RunReq, run_logic

with open("/tmp/toy_plan.json") as f:
    plan = json.load(f)

kernel = HardKernel()

class SimpleLedger:
    def __init__(self):
        self.events = []
    def append(self, event):
        self.events.append(event)
        print(f"  LEDGER: {event.get('type', '?')}")

ledger = SimpleLedger()

req = RunReq(
    repo_id="toy",
    task="Fix the add() function",
    manual_plan=plan,
)

result = run_logic("run-toy-001", req, kernel, ledger)
print(f"\nResult: {result}")

# Write replay log
import json
with open("/tmp/toy_replay.json", "w") as f:
    json.dump({"run_id": "run-toy-001", "events": ledger.events}, f, indent=2)
print("Replay log: /tmp/toy_replay.json")
```

```bash
python /tmp/run_toy.py
```

---

## Step 6 — Verify Expected Output

```
LEDGER: RUN_START
LEDGER: STEP_PLAN
LEDGER: STEP_OK
LEDGER: STEP_PLAN
LEDGER: STEP_OK

Result: {'run_id': 'run-toy-001', 'status': 'completed', 'reason': 'All 2 planned steps completed'}
Replay log: /tmp/toy_replay.json
```

---

## Step 7 — Verify the Patch

```bash
cd /tmp/toy_repo
cat src/utils.py
# Expected: return a + b

python -m pytest tests/test_utils.py -q
# Expected: 1 passed
```

---

## Step 8 — Inspect the Replay Log

```bash
cat /tmp/toy_replay.json | python -m json.tool | head -40
```

The replay log contains:
- `RUN_START` — task and environment metadata
- `SANDBOX_INIT` — sandbox mode
- `STEP_PLAN` — each planned step
- `STEP_OK` / `STEP_FAILED` — execution result

---

## Notes

- **Local dev mode is trusted-only.** Never use this against repos you
  don't control.
- The `apply_patch` step requires a valid unified diff.  If the diff does
  not apply cleanly, it will fail with an error (not silently no-op).
- `apply_semantic_patch` is currently disabled.  Use `apply_patch` instead.
- `trace_execution` is disabled and must not be used.
