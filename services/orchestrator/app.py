import hashlib
import json
import os
import random
import time
from typing import Optional

from fastapi import FastAPI, HTTPException  # type: ignore[import-not-found]
from pydantic import BaseModel  # type: ignore[import-not-found]
import requests  # type: ignore[import-untyped]
import yaml  # type: ignore[import-untyped]

from context_fingerprint import (  # type: ignore[import-not-found]
    build_context,
    parse_failure_signature,
    compute_dense_reward,
    extract_test_nodes,
)
try:
    from phase_tracker import PhaseTracker  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    from services.orchestrator.phase_tracker import (  # type: ignore[import-not-found]
        PhaseTracker,
    )
from prompts import (  # type: ignore[import-not-found]
    SYSTEM,
    USER_TEMPLATE,
    TRANSCRIPT_TEMPLATE,
    DONE_PROMPT,
)

# ── Hard RFSN Kernel (v2) ─────────────────────
import sys as _sys
_sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )),
))
try:
    from rfsn_kernel.kernel import (
        HardKernel,
    )
    from rfsn_kernel.hard_ledger import (
        LedgerRecord,
    )
    from rfsn_kernel.state import (
        Outcome,
    )
    from rfsn_kernel.planner import (
        HierarchicalPlanner,
    )
    from rfsn_kernel.memory import (
        MemoryImmuneSystem,
        MemoryEntry,
    )
    from rfsn_kernel.replay import (
        ReplayRunner,
        snapshot_environment,
    )
    _HAS_HARD_KERNEL = True
except ImportError:
    _HAS_HARD_KERNEL = False

import sys
sys.path.insert(0, "/shared")
try:
    from auth import (  # type: ignore[import-not-found]
        ServiceAuthMiddleware,
        auth_headers,
    )
    _HAS_AUTH = True
except ImportError:
    _HAS_AUTH = False
    def auth_headers(): return {}

app = FastAPI()
if _HAS_AUTH:
    app.add_middleware(
        ServiceAuthMiddleware  # type: ignore[possibly-unbound]
    )

LLM_URL = os.getenv("LLM_URL", "http://llm_service:8001")
TOOL_GATEWAY_URL = os.getenv("TOOL_GATEWAY_URL", "http://tool_gateway:8002")
EXECUTOR_URL = os.getenv("EXECUTOR_URL", "http://executor:8003")
LEARNER_URL = os.getenv("LEARNER_URL", "http://learner_service:8004")
HARD_LEDGER_PATH = os.getenv(
    "RFSN_HARD_LEDGER_PATH",
    "/data/kernel_ledger.jsonl",
)
SEED = os.getenv("RFSN_SEED", "1")
WARM_SANDBOX = os.getenv("RFSN_WARM_SANDBOX", "1") == "1"


def _load_yaml(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (FileNotFoundError, PermissionError) as exc:
        print(
            f"FATAL: Cannot load policy file {path}:"
            f" {exc}",
            flush=True,
        )
        raise SystemExit(1) from exc


DEPS_POLICY = _load_yaml("/policies/deps_policy.yaml")
TEST_POLICY = _load_yaml("/policies/test_policy.yaml")
GATE_POLICY = _load_yaml("/policies/gate_policy.yaml")


# ── Compiled policy hash (determinism anchor) ─
# Hash all policy files at startup so every ledger
# entry can reference the exact policy version.
def _compile_policy_hash() -> str:
    """Hash all policy YAML files to a single hex digest."""
    h = hashlib.sha256()
    for name in sorted([
        "command_templates.yaml",
        "deps_policy.yaml",
        "diff_guard.yaml",
        "gate_policy.yaml",
        "llm_cassette.yaml",
        "test_policy.yaml",
        "tool_allowlist.yaml",
    ]):
        path = f"/policies/{name}"
        try:
            with open(path, "rb") as f:
                h.update(f.read())
        except FileNotFoundError:
            h.update(name.encode())
    return h.hexdigest()[:16]


POLICY_HASH = _compile_policy_hash()

# ── Episode determinism ──────────────────────
# Seed Python random from RFSN_SEED so that any
# random tie-breaking is reproducible.
_EPISODE_SEED = int(
    hashlib.sha256(SEED.encode()).hexdigest()[:8],
    16,
)
random.seed(_EPISODE_SEED)

# ── Strict JSON parse (fail-closed) ─────────

_REQUIRED_KEYS = {"step", "done", "intent"}


def _repair_json(text: str) -> dict | None:
    """Strict JSON parser for execution contract."""
    if not text:
        return None
    raw = text.strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        return None
    return None


def _event_hash(event: dict) -> str:
    blob = json.dumps(
        event,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _event_record(event: dict) -> "LedgerRecord":
    event_type = str(event.get("type", "EVENT"))
    run_id = str(event.get("run_id", ""))
    ev_hash = _event_hash(event)
    state_hash = hashlib.sha256(
        f"event_state:{ev_hash}".encode("utf-8"),
    ).hexdigest()
    return LedgerRecord(
        proposal_hash=ev_hash,
        simulation={},
        risk={},
        decision="REJECT",
        decision_reason=f"event:{event_type}",
        outcome_hash=None,
        state_hash=state_hash,
        metadata={
            "record_type": "orchestrator_event",
            "event_type": event_type,
            "run_id": run_id,
            "action": f"event:{event_type}",
            "intent": "orchestrator_event",
            "event": event,
        },
    )


class _LedgerSink:
    """Route orchestrator events into the hard ledger chain."""

    def __init__(self, kernel: Optional["HardKernel"]):
        self._kernel = kernel

    def append(self, event: dict) -> None:
        if not event:
            return
        if not (_HAS_HARD_KERNEL and self._kernel):
            return
        self._kernel.ledger.append(_event_record(event))

    def verify_chain(self) -> dict:
        if not (_HAS_HARD_KERNEL and self._kernel):
            return {
                "ok": False,
                "entries": 0,
                "errors": [{
                    "line": 0,
                    "error": "hard kernel unavailable",
                }],
            }
        return self._kernel.ledger.verify_chain()

# ── Hard kernel v2 (simulation + risk + replay) ─
if _HAS_HARD_KERNEL:
    _hard_kernel = HardKernel(
        ledger_path=HARD_LEDGER_PATH,
        policy={
            "risk_max": 0.65,
            "success_min": 0.15,
            "loop_max": 0.8,
            "drift_max": 0.85,
            "risk_lambda": 0.7,
            "max_total_steps": 200,
            "history_max": 500,
            "rng_seed": _EPISODE_SEED,
            "policy_hash": POLICY_HASH,
            "fail_cluster_threshold": 8,
        },
    )
    _planner = HierarchicalPlanner(
        max_stagnation=5,
        max_escalations=3,
    )
    _memory = MemoryImmuneSystem(
        quality_min=0.3,
        risk_max=0.7,
        contradiction_max=0.6,
        max_entries=2000,
    )
else:
    _hard_kernel = None  # type: ignore[assignment]
    _planner = None      # type: ignore[assignment]
    _memory = None       # type: ignore[assignment]

ledger = _LedgerSink(_hard_kernel)


def stable_id(
    prefix: str, *parts: str, n: int = 10,
) -> str:
    h = hashlib.sha256(
        ("|".join(parts)).encode("utf-8")
    ).hexdigest()
    return f"{prefix}-{h[:n]}"


def venv_exists(repo_id: str) -> bool:
    return os.path.exists(
        f"/data/venv/{repo_id}/bin/activate"
    )


def is_tests_only_task(task: str) -> bool:
    t = (task or "").lower()
    triggers = [
        "run pytest", "run tests",
        "confirm green", "make no changes",
        "tests only", "no changes",
    ]
    return (
        any(x in t for x in triggers)
        and ("fix" not in t)
        and ("patch" not in t)
        and ("edit" not in t)
    )


class RunReq(BaseModel):
    repo_id: str
    task: str
    max_iters: int = 3
    scenario: Optional[str] = None


@app.get("/health")
def health():
    """Deep health: check all downstream deps."""
    deps = {}
    for name, url in [
        ("llm_service", LLM_URL),
        ("tool_gateway", TOOL_GATEWAY_URL),
        ("learner_service", LEARNER_URL),
    ]:
        try:
            r = requests.get(
                f"{url}/health",
                timeout=3,
            )
            deps[name] = r.status_code == 200
        except Exception:
            deps[name] = False
    all_ok = all(deps.values())
    return {
        "ok": all_ok,
        "deps": deps,
        "kernel_loaded": (
            _HAS_HARD_KERNEL
            and _hard_kernel is not None
        ),
        "policies": {
            "deps": bool(DEPS_POLICY),
            "test": bool(TEST_POLICY),
        },
    }


# ── Run metrics (in-memory, per-process) ─────
_METRICS: dict = {
    "runs_total": 0,
    "runs_ok": 0,
    "runs_fail": 0,
    "llm_calls": 0,
    "llm_retries": 0,
    "gate_rejections": 0,
    "steps_executed": 0,
}


@app.get("/metrics")
def metrics():
    return _METRICS


@app.get("/ledger/verify")
def ledger_verify():
    """Verify integrity of the append-only ledger."""
    return ledger.verify_chain()


@app.get("/kernel/stats")
def kernel_stats():
    """Hard kernel v2 statistics."""
    if not _HAS_HARD_KERNEL:
        return {"available": False}
    return {
        "available": True,
        "kernel": _hard_kernel.get_stats(),
        "planner": _planner.get_stats(),
        "memory": _memory.get_stats(),
    }


@app.get("/kernel/replay/verify")
def kernel_replay_verify(run_id: Optional[str] = None):
    """Verify hard kernel ledger chain."""
    if not _HAS_HARD_KERNEL:
        return {"available": False}
    runner = ReplayRunner(HARD_LEDGER_PATH)
    out = runner.replay_verify(run_id=run_id).to_dict()
    out["run_id"] = run_id or ""
    return out


@app.get("/kernel/replay/trace")
def kernel_replay_trace(run_id: Optional[str] = None):
    """Extract decision trace from hard kernel."""
    if not _HAS_HARD_KERNEL:
        return {"available": False}
    runner = ReplayRunner(HARD_LEDGER_PATH)
    trace = runner.extract_decision_trace(run_id=run_id)
    return {
        "run_id": run_id or "",
        "count": len(trace),
        "trace": trace,
    }


def _sandbox_create(run_id: str, repo_id: str):
    """Ask executor to spin up a warm sandbox."""
    if not WARM_SANDBOX:
        return None
    try:
        r = requests.post(
            f"{EXECUTOR_URL}/sandbox/create",
            json={"run_id": run_id, "repo_id": repo_id},
            headers=auth_headers(),
            timeout=30,
        )
        if r.status_code == 200:
            return r.json()
    except Exception as exc:
        print(
            f"WARN: sandbox create failed: {exc}",
            flush=True,
        )
    return None


def _sandbox_destroy(run_id: str, repo_id: str):
    """Tear down the warm sandbox for a run."""
    if not WARM_SANDBOX:
        return None
    try:
        r = requests.post(
            f"{EXECUTOR_URL}/sandbox/destroy",
            json={"run_id": run_id, "repo_id": repo_id},
            headers=auth_headers(),
            timeout=15,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def run_step(
    repo_id: str, it: int, step: dict,
    run_id: str | None = None,
):
    payload = {
        "repo_id": repo_id,
        "iter": it,
        "step": step,
    }
    if run_id and WARM_SANDBOX:
        payload["run_id"] = run_id
    r = requests.post(
        f"{TOOL_GATEWAY_URL}/run_step",
        json=payload,
        headers=auth_headers(),
        timeout=300,
    )
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    return r.json()


def execute_approved_step(
    repo_id: str,
    it: int,
    step: dict,
    run_id: str,
    *,
    context_hash: str = "",
    intent: str = "",
    bundle_id: str = "",
    step_num: Optional[int] = None,
) -> dict:
    """Execute a kernel-approved step through the hard kernel.

    Returns a dict:
      {
        "ok": bool,
        "out": dict | None,
        "reason": str,
        "hard_kernel": bool,
      }
    """
    if _HAS_HARD_KERNEL and _hard_kernel:
        if _memory:
            _hard_kernel.state.memory_version = (
                _memory.memory_version
            )
        if _hard_kernel.state.resource_state.get(
            "run_id", "",
        ) != run_id:
            _hard_kernel.state.resource_state[
                "run_id"
            ] = run_id

        def _exec_step(s: dict) -> Outcome:
            """Execution callback for hard kernel."""
            r = run_step(
                repo_id, it, s, run_id,
            )
            ok = r.get("status", 1) == 0
            return Outcome(
                success=ok,
                exit_code=r.get(
                    "status", 1,
                ),
                payload=str(
                    r.get("payload", ""),
                )[:3000],
                logs=str(
                    r.get("logs", ""),
                )[:5000],
                duration_sec=float(
                    r.get("seconds", 0),
                ),
            )

        kr = _hard_kernel.kernel_step(
            step,
            execute_fn=_exec_step,
            context=context_hash,
            intent=intent,
            bundle_id=bundle_id,
            run_id=run_id,
        )
        hard_rec = {
            "type": "HARD_KERNEL_STEP",
            "run_id": run_id,
            "iter": it,
            "phase": kr.phase,
            "approved": kr.approved,
            "success": kr.success,
            "error": kr.error,
            "reason": (
                kr.decision.reason
                if kr.decision else ""
            ),
            "risk": (
                kr.risk.to_dict()
                if kr.risk else None
            ),
            "simulation": (
                kr.simulation.to_dict()
                if kr.simulation else None
            ),
        }
        if step_num is not None:
            hard_rec["step_num"] = step_num
        ledger.append(hard_rec)

        if not kr.approved:
            reason = (
                kr.decision.reason
                if kr.decision
                else (kr.error or "kernel_reject")
            )
            return {
                "ok": False,
                "out": None,
                "reason": reason,
                "hard_kernel": True,
            }

        out = {
            "status": (
                kr.outcome.exit_code
                if kr.outcome else 1
            ),
            "payload": (
                kr.outcome.payload
                if kr.outcome else ""
            ),
            "logs": (
                kr.outcome.logs
                if kr.outcome else ""
            ),
            "seconds": (
                kr.outcome.duration_sec
                if kr.outcome else 0
            ),
        }

        if _memory:
            _memory.admit(MemoryEntry(
                content=(
                    f"action={step.get('type')}"
                    f" success={kr.success}"
                    f" risk={kr.risk.total_risk:.2f}"
                    if kr.risk else ""
                ),
                source="kernel",
                entry_type="action_outcome",
            ))

        return {
            "ok": True,
            "out": out,
            "reason": "",
            "hard_kernel": True,
        }

    return {
        "ok": False,
        "out": None,
        "reason": "hard kernel unavailable",
        "hard_kernel": False,
    }


def llm_chat(
    messages: list, run_id: str,
    call_index: int, repo_id: str,
    scenario: str,
    *,
    max_retries: int = 3,
):
    payload = {
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 1600,
        "run_id": run_id,
        "call_index": call_index,
        "repo_id": repo_id,
        "scenario": scenario,
    }
    last_exc: Exception = RuntimeError("no attempt")
    for attempt in range(max_retries):
        try:
            _METRICS["llm_calls"] += 1
            r = requests.post(
                f"{LLM_URL}/chat",
                json=payload,
                headers=auth_headers(),
                timeout=120,
            )
            if r.status_code == 429:
                # Rate-limited — back off
                _METRICS["llm_retries"] += 1
                wait = 2 ** attempt
                time.sleep(wait)
                continue
            if r.status_code != 200:
                raise HTTPException(
                    r.status_code, r.text,
                )
            return r.json()
        except requests.exceptions.Timeout:
            _METRICS["llm_retries"] += 1
            last_exc = requests.exceptions.Timeout(
                f"attempt {attempt + 1}",
            )
            time.sleep(2 ** attempt)
        except HTTPException:
            raise
        except Exception as exc:
            last_exc = exc
            _METRICS["llm_retries"] += 1
            time.sleep(2 ** attempt)
    raise HTTPException(
        502,
        f"LLM unreachable after {max_retries}"
        f" retries: {last_exc}",
    )


def failure_signature(text: str) -> str:
    """Deterministic signature for learner bucketing."""
    blob = (text or "").encode(
        "utf-8", errors="ignore",
    )[:20000]
    return hashlib.sha256(blob).hexdigest()[:16]


def learner_suggest(
    repo_id: str,
    task: str,
    last_fail: str,
    last_stage: str = "unknown",
) -> dict:
    repo_path = f"/data/repos/{repo_id}"
    ctx = build_context(repo_path, last_fail)
    # Include stage context so the learner can
    # use it for context_key partitioning.
    ctx["stage"] = last_stage

    # Parse failure for signature-aware routing.
    fail_sig = parse_failure_signature(last_fail)
    sig_hash = fail_sig.get("signature_hash", "")

    payload = {
        "repo_id": repo_id,
        "task": task,
        "meta": ctx,
        "failure_signature_hash": sig_hash,
    }
    try:
        r = requests.post(
            f"{LEARNER_URL}/suggest",
            json=payload,
            headers=auth_headers(),
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    # Learner is advisory; fail open.
    ck = (
        f"{ctx['lang']}|{ctx['tests']}"
        f"|{ctx['framework']}|{ctx['failure']}"
        f"|{last_stage}"
    )
    return {
        "context_key": ck,
        "strategy_id": "PB_generic_fix",
        "prompt_addendum": (
            "Strategy: search first,"
            " narrow reads,"
            " patch minimal. No refactor."
        ),
        "constraints": {
            "max_patch_files": 3,
            "max_patch_total_lines": 80,
            "max_added_lines": 40,
            "max_deleted_lines": 40,
            "forbid_test_edits": True,
        },
        "playbook_id": "PB_generic_fix",
        "playbook_guidance": None,
    }


def learner_ingest(
    run_id: str,
    strategy_id: str,
    success: bool,
    fail_sig: str,
    repo_id: str = "",
    last_fail: str = "",
    patch_hash: str = "",
    patch_files: str = "",
    patch_added: int = 0,
    patch_deleted: int = 0,
    test_exit_code: int = -1,
    tests_passed: int = 0,
    tests_failed: int = 0,
    tests_total: int = 0,
    dense_reward: float = 0.0,
    task: str = "",
    stage: str = "",
) -> None:
    repo_path = f"/data/repos/{repo_id}"
    ctx = build_context(
        repo_path, last_fail,
    )
    # Inject stage into meta so the learner
    # includes it in the context_key.
    if stage:
        ctx["stage"] = stage

    # Parse structured failure fields.
    parsed = parse_failure_signature(last_fail)

    payload = {
        "run_id": run_id,
        "strategy_id": strategy_id,
        "meta": ctx,
        "success": bool(success),
        "failure_signature": fail_sig or "",
        "stage": stage,
        # Outcome mapping
        "repo_id": repo_id,
        "task": task,
        "patch_hash": patch_hash,
        "patch_files": patch_files,
        "patch_added": patch_added,
        "patch_deleted": patch_deleted,
        "test_exit_code": test_exit_code,
        "tests_passed": tests_passed,
        "tests_failed": tests_failed,
        "tests_total": tests_total,
        "failure_class": parsed.get(
            "failure_class", "",
        ),
        "dense_reward": dense_reward,
        # Structured failure fields
        "failure_module": parsed.get(
            "failure_module", "",
        ),
        "failure_test": parsed.get(
            "failure_test", "",
        ),
        "failure_message": parsed.get(
            "failure_message", "",
        ),
        "failure_signature_hash": parsed.get(
            "signature_hash", "",
        ),
    }
    try:
        requests.post(
            f"{LEARNER_URL}/ingest",
            json=payload,
            headers=auth_headers(),
            timeout=10,
        )
    except Exception:
        return


@app.post("/run")
def run(req: RunReq):
    _METRICS["runs_total"] += 1
    scenario = req.scenario or "golden"
    run_id = stable_id(
        "run", SEED, req.repo_id, req.task,
        str(req.max_iters), scenario, n=10,
    )
    run_seed = int(
        hashlib.sha256(
            f"{_EPISODE_SEED}|{run_id}".encode(
                "utf-8",
            )
        ).hexdigest()[:8],
        16,
    )
    if not (_HAS_HARD_KERNEL and _hard_kernel):
        raise HTTPException(
            503,
            "hard kernel required but unavailable",
        )
    env_snapshot = (
        snapshot_environment(
            repo_path=f"/data/repos/{req.repo_id}",
            seed=run_seed,
        )
        if _HAS_HARD_KERNEL else {"env_hash": ""}
    )
    if _HAS_HARD_KERNEL and _hard_kernel:
        _hard_kernel.reset_for_run(
            run_id=run_id,
            rng_seed=run_seed,
            env_hash=env_snapshot.get(
                "env_hash", "",
            ),
            memory_version=(
                _memory.memory_version
                if _memory else "0"
            ),
            policy_hash=POLICY_HASH,
            reset_history=True,
        )
    if _HAS_HARD_KERNEL and _planner:
        _planner.reset()

    ledger.append({
        "type": "RUN_START",
        "run_id": run_id,
        "repo_id": req.repo_id,
        "task": req.task,
        "scenario": scenario,
        "policy_hash": POLICY_HASH,
        "seed": SEED,
        "episode_seed": run_seed,
        "env_hash": env_snapshot.get(
            "env_hash", "",
        ),
        "memory_version": (
            _hard_kernel.state.memory_version
            if _HAS_HARD_KERNEL and _hard_kernel
            else ""
        ),
    })

    # ── Warm sandbox lifecycle ─────────────────
    sb_info = _sandbox_create(run_id, req.repo_id)
    if sb_info:
        ledger.append({
            "type": "SANDBOX_CREATED",
            "run_id": run_id,
            "container_id": sb_info.get(
                "container_id",
            ),
            "image_hash": sb_info.get(
                "image_hash",
            ),
        })

    # tests-only fast path
    if is_tests_only_task(req.task):
        it = 1
        steps = []
        if DEPS_POLICY.get("enabled", True) and not venv_exists(req.repo_id):
            steps.append({
                "id": "auto-deps",
                "type": "ensure_deps",
                "manifest": DEPS_POLICY.get(
                    "manifest", "requirements.txt"
                ),
                "timeout_s": int(
                    DEPS_POLICY.get(
                        "max_install_seconds", 420
                    )
                ),
            })
        steps.append({
            "id": "t1",
            "type": "run_tests",
            "template_id": "pytest_targeted",
            "template_params": {"target": "tests"},
            "timeout_s": 240,
        })
        steps.append({
            "id": "t2",
            "type": "run_tests",
            "template_id": "pytest_suite",
            "template_params": {"target": ""},
            "timeout_s": 900,
        })
        bundle = {
            "intent": "tests-only fast path",
            "bundle_id": stable_id(
                "b", SEED, req.repo_id,
                req.task, "fast", scenario,
                n=8,
            ),
            "steps": steps,
            "acceptance": {
                "tests_green": True,
                "no_new_failures": True,
            },
        }
        ledger.append({
            "type": "BUNDLE_PROPOSED",
            "run_id": run_id,
            "bundle": bundle,
        })

        results = []
        for i_step, s in enumerate(
            steps, start=1,
        ):
            ex = execute_approved_step(
                req.repo_id,
                it,
                s,
                run_id,
                context_hash="tests_only",
                intent=bundle["intent"],
                bundle_id=bundle["bundle_id"],
                step_num=i_step,
            )
            if not ex["ok"]:
                _METRICS["gate_rejections"] += 1
                _sandbox_destroy(run_id, req.repo_id)
                ledger.append({
                    "type": "RUN_END",
                    "run_id": run_id,
                    "status": "rejected",
                    "reason": ex["reason"],
                })
                return {
                    "run_id": run_id,
                    "status": "rejected",
                    "errors": [{
                        "code": "HARD_KERNEL_REJECT",
                        "msg": ex["reason"],
                    }],
                    "results": results,
                }
            _METRICS["steps_executed"] += 1
            out = ex["out"]
            ledger.append({
                "type": "STEP_RESULT",
                "run_id": run_id,
                "iter": it,
                "step": s,
                "out": out,
            })
            results.append({"step": s, "out": out})
            if out.get("status", 0) != 0:
                _sandbox_destroy(run_id, req.repo_id)
                ledger.append({
                    "type": "RUN_END",
                    "run_id": run_id,
                    "status": "fail",
                })
                return {
                    "run_id": run_id,
                    "status": "fail",
                    "results": results,
                }
        _sandbox_destroy(run_id, req.repo_id)
        ledger.append({
            "type": "RUN_END",
            "run_id": run_id,
            "status": "ok",
        })
        _METRICS["runs_ok"] += 1
        return {
            "run_id": run_id,
            "status": "ok",
            "results": results,
        }

    # ── Interactive tool loop ──────────────────
    # Each iteration: ask LLM for ONE step, execute
    # it, append output to transcript, repeat.
    # This replaces the old "batch bundle" approach.
    call_index = 0
    last_fail = ""
    last_strategy = None
    last_stage = "unknown"  # stage tracking for learner

    # Per-iteration step budget (hard cap per iter).
    MAX_STEPS_PER_ITER = int(
        GATE_POLICY.get(
            "max_steps_per_bundle", 15,
        ),
    )
    # Total steps across all iterations.
    MAX_TOTAL_STEPS = MAX_STEPS_PER_ITER * req.max_iters

    total_steps_used = 0

    for it in range(1, req.max_iters + 1):
        fail_ctx = (
            "\n\nLast iteration failure:\n"
            + last_fail
            if last_fail else ""
        )

        sug = learner_suggest(
            req.repo_id, req.task, last_fail,
            last_stage,
        )
        last_strategy = sug.get("strategy_id")
        constraints = (
            sug.get("constraints") or {}
        )

        ledger.append({
            "type": "LEARNER_SUGGESTED",
            "run_id": run_id,
            "iter": it,
            "strategy_id": sug.get(
                "strategy_id",
            ),
            "playbook_id": sug.get(
                "playbook_id",
            ),
            "context_key": sug.get(
                "context_key",
            ),
            "constraints": constraints,
            "failure_hint": sug.get(
                "failure_hint",
            ),
        })

        # Build failure hint string for prompt.
        failure_hint_text = ""
        if sug.get("failure_hint"):
            failure_hint_text = (
                "\n\n[LEARNER HINT] "
                + sug["failure_hint"]
            )
        # Build past-outcomes context.
        past_text = ""
        if sug.get("past_outcomes"):
            past_items = sug["past_outcomes"][:3]
            past_lines = []
            for po in past_items:
                label = (
                    "PASS" if po.get("success")
                    else "FAIL"
                )
                past_lines.append(
                    f"  - [{label}]"
                    f" strategy={po.get('strategy_id', '?')}"
                    f" files={po.get('patch_files', '?')}"
                    f" reward={po.get('dense_reward', 0):.2f}"
                )
            past_text = (
                "\n\n[PAST ATTEMPTS]\n"
                + "\n".join(past_lines)
            )

        # Build playbook guidance text.
        pb_guidance = sug.get(
            "playbook_guidance", "",
        ) or ""
        if pb_guidance:
            pb_guidance = (
                "## Playbook (follow in order)\n"
                + pb_guidance
            )

        # ── Hierarchical planner guidance ─────
        strategic_guidance = ""
        if _HAS_HARD_KERNEL and _planner:
            if it == 1 and not _planner.state.goal:
                fail_cls = (
                    parse_failure_signature(
                        last_fail,
                    ).get("failure_class", "")
                )
                task_type = _planner.classify_task(
                    req.task, fail_cls,
                )
                _planner.set_goal(
                    req.task, task_type,
                )
            strategic_guidance = (
                _planner.get_planner_guidance()
            )

        prompt = USER_TEMPLATE.format(
            repo_id=req.repo_id,
            task=(
                req.task + fail_ctx
                + failure_hint_text + past_text
                + ("\n\n" + strategic_guidance
                   if strategic_guidance else "")
            ),
            learner_addendum=sug.get(
                "prompt_addendum", "",
            ),
            playbook_guidance=pb_guidance,
            max_patch_files=constraints.get(
                "max_patch_files", 3,
            ),
            max_patch_total_lines=constraints.get(
                "max_patch_total_lines", 80,
            ),
            max_added_lines=constraints.get(
                "max_added_lines", 40,
            ),
            max_deleted_lines=constraints.get(
                "max_deleted_lines", 40,
            ),
            forbid_test_edits=constraints.get(
                "forbid_test_edits", True,
            ),
            max_steps=MAX_STEPS_PER_ITER,
        )

        # Build message history: system + user
        # prompt + transcript of this iteration.
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ]

        # Auto-inject ensure_deps if needed.
        if (
            DEPS_POLICY.get("enabled", True)
            and not venv_exists(req.repo_id)
        ):
            dep_step = {
                "id": "auto-deps",
                "type": "ensure_deps",
                "manifest": DEPS_POLICY.get(
                    "manifest",
                    "requirements.txt",
                ),
                "timeout_s": int(
                    DEPS_POLICY.get(
                        "max_install_seconds",
                        420,
                    )
                ),
            }
            dep_bundle_id = stable_id(
                "dep", SEED, req.repo_id,
                str(it), scenario, n=8,
            )
            ex = execute_approved_step(
                req.repo_id, it, dep_step,
                run_id,
                context_hash=sug.get(
                    "context_key", "",
                ),
                intent="auto deps",
                bundle_id=dep_bundle_id,
                step_num=0,
            )
            if not ex["ok"]:
                _METRICS["gate_rejections"] += 1
                last_stage = "gate_reject"
                last_fail = (
                    "Auto-deps rejected by"
                    " hard kernel: "
                    + ex["reason"]
                )
                messages.append({
                    "role": "user",
                    "content": (
                        "HARD KERNEL REJECTED"
                        " auto-deps step:"
                        f" {ex['reason']}\n"
                        "Continue with another"
                        " approach."
                    ),
                })
            else:
                _METRICS["steps_executed"] += 1
                total_steps_used += 1
                dep_out = ex["out"]
                ledger.append({
                    "type": "STEP_RESULT",
                    "run_id": run_id,
                    "iter": it,
                    "step": dep_step,
                    "out": dep_out,
                })
                # Tell the LLM deps are installed.
                messages.append({
                    "role": "assistant",
                    "content": json.dumps({
                        "step": dep_step,
                        "done": False,
                        "intent": (
                            "auto-install deps"
                        ),
                    }),
                })
                dep_status = (
                    "ok"
                    if dep_out.get("status", 0) == 0
                    else "FAILED"
                )
                if dep_status != "ok":
                    last_stage = "deps"
                messages.append({
                    "role": "user",
                    "content": (
                        TRANSCRIPT_TEMPLATE.format(
                            step_num=0,
                            step_json=json.dumps(
                                dep_step,
                            ),
                            status=dep_status,
                            output=(
                                dep_out.get(
                                    "logs", "",
                                )[-2000:]
                            ),
                        )
                    ),
                })

        # ── Inner step loop for this iteration ──
        iter_steps_used = 0
        iter_results = []

        # Track test counts for dense reward.
        prev_test_counts: Optional[dict] = None
        curr_test_counts: Optional[dict] = None
        iter_dense_reward = 0.0
        # Track patch metadata.
        iter_patch_hash = ""
        iter_patch_files = ""
        iter_patch_added = 0
        iter_patch_deleted = 0
        iter_test_exit = -1

        # RFSN phase tracker for this iteration.
        phase = PhaseTracker()

        while iter_steps_used < MAX_STEPS_PER_ITER:
            if total_steps_used >= MAX_TOTAL_STEPS:
                last_fail = (
                    "Total step budget exhausted"
                )
                break

            # Ask LLM for next step.
            call_index += 1
            ledger.append({
                "type": "LLM_CALL",
                "run_id": run_id,
                "call_index": call_index,
                "iter": it,
            })
            try:
                llm = llm_chat(
                    messages, run_id,
                    call_index,
                    req.repo_id, scenario,
                )
            except Exception:
                last_fail = (
                    "LLM call failed"
                )
                break

            content = llm.get("content", "")

            # Parse the LLM response (robust).
            resp = _repair_json(content)
            if resp is None:
                # Append structured parse error
                # and let LLM try again (1 retry).
                messages.append({
                    "role": "assistant",
                    "content": content,
                })
                # Tell LLM exactly what's wrong.
                snippet = content[:200].replace(
                    "\n", " ",
                )
                messages.append({
                    "role": "user",
                    "content": (
                        "PARSE ERROR: your response"
                        " is not valid JSON.\n"
                        f"Received: {snippet!r}\n\n"
                        "Requirements:\n"
                        "1. Return ONLY a JSON"
                        " object — no markdown,"
                        " no commentary.\n"
                        '2. Required keys: "step"'
                        ' (dict|null), "done"'
                        ' (bool), "intent"'
                        " (string).\n"
                        "3. When done=true, step"
                        " must be null.\n"
                        "4. Example:\n"
                        '   {"step": {"id":"s1",'
                        ' "type":"repo_search",'
                        ' "pattern":"foo"},'
                        ' "done": false,'
                        ' "intent": "find foo"}\n'
                    ),
                })
                continue

            # Validate required keys.
            missing = _REQUIRED_KEYS - set(
                resp.keys(),
            )
            if missing:
                messages.append({
                    "role": "assistant",
                    "content": content,
                })
                messages.append({
                    "role": "user",
                    "content": (
                        "SCHEMA ERROR: missing"
                        " required keys:"
                        f" {sorted(missing)}.\n"
                        "Your JSON must have"
                        ' "step", "done",'
                        ' and "intent".'
                    ),
                })
                continue

            # Check if LLM says done.
            if resp.get("done", False):
                ledger.append({
                    "type": "LLM_DONE",
                    "run_id": run_id,
                    "iter": it,
                    "intent": resp.get(
                        "intent", "",
                    ),
                })
                break

            step = resp.get("step")
            if not step or not isinstance(
                step, dict,
            ):
                messages.append({
                    "role": "assistant",
                    "content": content,
                })
                messages.append({
                    "role": "user",
                    "content": (
                        "Invalid response: 'step'"
                        " must be a dict."
                        " Try again."
                    ),
                })
                continue

            # Normalize step.
            if not step.get("id"):
                step["id"] = (
                    f"s{iter_steps_used + 1}"
                )

            # ── RFSN phase transition check ──
            step_type = step.get("type", "")
            phase_ok, phase_err = (
                phase.check_transition(step_type)
            )
            if not phase_ok:
                messages.append({
                    "role": "assistant",
                    "content": content,
                })
                messages.append({
                    "role": "user",
                    "content": (
                        "PHASE VIOLATION: "
                        + phase_err
                        + "\nCurrent phase: "
                        + phase.phase
                        + ". Adjust your step"
                        " type and try again."
                    ),
                })
                ledger.append({
                    "type": "PHASE_VIOLATION",
                    "run_id": run_id,
                    "iter": it,
                    "current_phase": phase.phase,
                    "attempted_type": step_type,
                    "error": phase_err,
                })
                continue

            bundle_id = stable_id(
                "b", SEED, req.repo_id,
                req.task, str(it),
                str(iter_steps_used),
                scenario, n=8,
            )
            approved_step = step

            ex = execute_approved_step(
                req.repo_id,
                it,
                approved_step,
                run_id,
                context_hash=sug.get(
                    "context_key", "",
                ),
                intent=resp.get(
                    "intent", "",
                ),
                bundle_id=bundle_id,
                step_num=iter_steps_used + 1,
            )
            if not ex["ok"]:
                _METRICS["gate_rejections"] += 1
                last_stage = "gate_reject"
                messages.append({
                    "role": "assistant",
                    "content": content,
                })
                messages.append({
                    "role": "user",
                    "content": (
                        "HARD KERNEL REJECTED"
                        " (simulation/risk):"
                        f" {ex['reason']}\n"
                        "Try a different"
                        " approach."
                    ),
                })
                continue

            _METRICS["steps_executed"] += 1
            iter_steps_used += 1
            total_steps_used += 1
            out = ex["out"]

            ledger.append({
                "type": "STEP_RESULT",
                "run_id": run_id,
                "iter": it,
                "step": approved_step,
                "out": out,
            })
            iter_results.append({
                "step": approved_step,
                "out": out,
            })

            # Advance RFSN phase.
            phase.advance(
                approved_step.get("type", ""),
            )

            # Determine status.
            step_status = out.get("status", 0)
            status_label = (
                "ok" if step_status == 0
                else f"FAILED (exit {step_status})"
            )

            # Extract payload for feedback.
            payload = out.get("payload")
            logs = out.get("logs", "")

            # Build a compact output summary for
            # the transcript (capped at 3000 chars
            # to control token usage).
            if payload and isinstance(
                payload, str,
            ):
                output_text = payload[-3000:]
            else:
                output_text = logs[-3000:]

            # Append assistant response + tool
            # output to messages as transcript.
            messages.append({
                "role": "assistant",
                "content": content,
            })
            messages.append({
                "role": "user",
                "content": (
                    TRANSCRIPT_TEMPLATE.format(
                        step_num=iter_steps_used,
                        step_json=json.dumps(
                            approved_step,
                        ),
                        status=status_label,
                        output=output_text,
                    )
                ),
            })

            # If this was a failing test step,
            # record for learner but keep going
            # (LLM may want to retry/adapt).
            if step_status != 0:
                if approved_step.get(
                    "type",
                ) == "apply_patch":
                    last_stage = "apply_patch"
                    ledger.append({
                        "type": "PATCH_REJECTED",
                        "run_id": run_id,
                        "iter": it,
                        "status": step_status,
                    })

            # Track test counts for dense reward.
            if approved_step.get("type") == "run_tests":
                iter_test_exit = step_status
                log_text = out.get("logs", "")
                parsed_sig = parse_failure_signature(
                    log_text,
                )
                new_counts = parsed_sig.get(
                    "test_counts",
                )
                if new_counts:
                    prev_test_counts = curr_test_counts
                    curr_test_counts = new_counts
                    iter_dense_reward = (
                        compute_dense_reward(
                            prev_test_counts,
                            curr_test_counts,
                        )
                    )
                # ── Targeted test node injection ─
                # When tests fail, extract the
                # specific node IDs so LLM can
                # use pytest_targeted next time.
                if step_status != 0:
                    failed_nodes = extract_test_nodes(
                        log_text,
                    )
                    if failed_nodes:
                        node_list = " ".join(
                            failed_nodes[:5],
                        )
                        messages.append({
                            "role": "user",
                            "content": (
                                "HINT: Failing test"
                                " nodes extracted:\n"
                                f"  {node_list}\n"
                                "Use pytest_targeted"
                                " with one of these"
                                " as the target for"
                                " faster feedback."
                            ),
                        })
                    last_fail = log_text[-5000:]
                    last_stage = "tests"
                    # Planner: record stagnation.
                    if (
                        _HAS_HARD_KERNEL
                        and _planner
                    ):
                        stagnant = (
                            _planner
                            .record_no_progress()
                        )
                        if stagnant:
                            _planner.escalate()
                else:
                    last_stage = "success"
                    # Planner: advance subgoal.
                    if (
                        _HAS_HARD_KERNEL
                        and _planner
                    ):
                        _planner.advance_subgoal()
                        if _hard_kernel:
                            _hard_kernel\
                                .adaptive_relax()

            # Track patch metadata for outcome DB.
            if approved_step.get("type") == "apply_patch":
                patch_text = approved_step.get(
                    "patch", "",
                )
                iter_patch_hash = hashlib.sha256(
                    patch_text.encode("utf-8"),
                ).hexdigest()[:16]
                # Extract file list from diff.
                pfiles = []
                for pline in patch_text.splitlines():
                    if pline.startswith("+++ b/"):
                        pfiles.append(
                            pline[6:].strip(),
                        )
                iter_patch_files = ",".join(pfiles)
                # Count added/deleted lines.
                p_add = p_del = 0
                for pline in patch_text.splitlines():
                    if (
                        pline.startswith("+++")
                        or pline.startswith("---")
                    ):
                        continue
                    if pline.startswith("+"):
                        p_add += 1
                    elif pline.startswith("-"):
                        p_del += 1
                iter_patch_added = p_add
                iter_patch_deleted = p_del

            # If tests passed, hint the LLM to
            # either declare done or continue.
            if (
                approved_step.get("type")
                == "run_tests"
                and step_status == 0
            ):
                messages.append({
                    "role": "user",
                    "content": DONE_PROMPT,
                })

        # ── End of inner step loop ──
        # Check if this iteration succeeded.
        test_results = [
            r for r in iter_results
            if r["step"].get("type") == "run_tests"
        ]
        tests_passed = (
            test_results
            and all(
                r["out"].get("status", 1) == 0
                for r in test_results
            )
        )
        has_patch = any(
            r["step"].get("type") == "apply_patch"
            for r in iter_results
        )
        patch_applied = any(
            r["step"].get("type") == "apply_patch"
            and r["out"].get("status", 1) == 0
            for r in iter_results
        )

        if tests_passed and (
            not has_patch or patch_applied
        ):
            # Run static analysis if configured.
            if TEST_POLICY.get(
                "suite_on_success", False
            ):
                sa_steps = []
                for sa_tmpl in TEST_POLICY.get(
                    "static_templates", []
                ):
                    sa_steps.append({
                        "id": f"auto-{sa_tmpl}",
                        "type": "run_tests",
                        "template_id": sa_tmpl,
                        "template_params": {
                            "target": "",
                        },
                        "timeout_s": 300,
                    })
                if sa_steps:
                    sa_bundle_id = stable_id(
                        "sa", run_id,
                        str(it), n=8,
                    )
                    for i_sa, sa_s in enumerate(
                        sa_steps,
                        start=1,
                    ):
                        ex = execute_approved_step(
                            req.repo_id,
                            it,
                            sa_s,
                            run_id,
                            context_hash=sug.get(
                                "context_key", "",
                            ),
                            intent="static analysis",
                            bundle_id=sa_bundle_id,
                            step_num=1000 + i_sa,
                        )
                        if not ex["ok"]:
                            _METRICS[
                                "gate_rejections"
                            ] += 1
                            last_stage = "gate_reject"
                            last_fail = (
                                "Static analysis"
                                " rejected by"
                                " hard kernel: "
                                + ex["reason"]
                            )
                            _sandbox_destroy(
                                run_id, req.repo_id,
                            )
                            ledger.append({
                                "type": "RUN_END",
                                "run_id": run_id,
                                "status": "rejected",
                                "reason": ex["reason"],
                            })
                            return {
                                "run_id": run_id,
                                "status": "rejected",
                                "errors": [{
                                    "code": "HARD_KERNEL_REJECT",
                                    "msg": ex["reason"],
                                }],
                                "results": iter_results,
                            }
                        _METRICS["steps_executed"] += 1
                        sa_out = ex["out"]
                        ledger.append({
                            "type": (
                                "STEP_RESULT"
                            ),
                            "run_id": run_id,
                            "iter": it,
                            "step": sa_s,
                            "out": sa_out,
                        })
                        iter_results.append({
                            "step": sa_s,
                            "out": sa_out,
                        })

            _sandbox_destroy(run_id, req.repo_id)
            ledger.append({
                "type": "RUN_END",
                "run_id": run_id,
                "status": "ok",
            })
            learner_ingest(
                run_id,
                last_strategy or "unknown",
                True,
                "",
                repo_id=req.repo_id,
                task=req.task,
                patch_hash=iter_patch_hash,
                patch_files=iter_patch_files,
                patch_added=iter_patch_added,
                patch_deleted=iter_patch_deleted,
                test_exit_code=iter_test_exit,
                tests_passed=(
                    curr_test_counts.get("passed", 0)
                    if curr_test_counts else 0
                ),
                tests_failed=(
                    curr_test_counts.get("failed", 0)
                    if curr_test_counts else 0
                ),
                tests_total=(
                    curr_test_counts.get("total", 0)
                    if curr_test_counts else 0
                ),
                dense_reward=1.0,
                stage="success",
            )
            _METRICS["runs_ok"] += 1
            return {
                "run_id": run_id,
                "status": "ok",
                "results": iter_results,
            }

        # Iteration failed — carry failure context
        # forward to next iteration.
        if not last_fail:
            last_fail = (
                "Iteration ended without"
                " passing tests"
            )
        # Record for learner.
        learner_ingest(
            run_id,
            last_strategy or "unknown",
            False,
            failure_signature(last_fail),
            repo_id=req.repo_id,
            last_fail=last_fail,
            task=req.task,
            patch_hash=iter_patch_hash,
            patch_files=iter_patch_files,
            patch_added=iter_patch_added,
            patch_deleted=iter_patch_deleted,
            test_exit_code=iter_test_exit,
            tests_passed=(
                curr_test_counts.get("passed", 0)
                if curr_test_counts else 0
            ),
            tests_failed=(
                curr_test_counts.get("failed", 0)
                if curr_test_counts else 0
            ),
            tests_total=(
                curr_test_counts.get("total", 0)
                if curr_test_counts else 0
            ),
            dense_reward=iter_dense_reward,
            stage=last_stage,
        )

    _sandbox_destroy(run_id, req.repo_id)
    ledger.append({
        "type": "RUN_END",
        "run_id": run_id,
        "status": "fail",
    })
    learner_ingest(
        run_id,
        last_strategy or "unknown",
        False,
        failure_signature(last_fail),
        repo_id=req.repo_id,
        last_fail=last_fail,
        task=req.task,
        patch_hash=iter_patch_hash,
        patch_files=iter_patch_files,
        patch_added=iter_patch_added,
        patch_deleted=iter_patch_deleted,
        test_exit_code=iter_test_exit,
        tests_passed=(
            curr_test_counts.get("passed", 0)
            if curr_test_counts else 0
        ),
        tests_failed=(
            curr_test_counts.get("failed", 0)
            if curr_test_counts else 0
        ),
        tests_total=(
            curr_test_counts.get("total", 0)
            if curr_test_counts else 0
        ),
        dense_reward=iter_dense_reward,
        stage=last_stage,
    )
    _METRICS["runs_fail"] += 1
    return {
        "run_id": run_id,
        "status": "fail",
    }
