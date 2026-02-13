import hashlib
import os
import random
import sys
from typing import Optional

from fastapi import FastAPI  # type: ignore[import-not-found]
from pydantic import BaseModel  # type: ignore[import-not-found]

from store_duckdb import DuckStore  # type: ignore[import-not-found]
from playbooks import (  # type: ignore[import-not-found]
    PLAYBOOKS,
    PLAYBOOK_IDS,
    PLAYBOOK_MAP,
    FAILURE_PLAYBOOK_PRIORS,
)

sys.path.insert(0, "/shared")
try:
    from auth import (  # type: ignore[import-not-found]
        ServiceAuthMiddleware,
    )

    _HAS_AUTH = True
except ImportError:
    _HAS_AUTH = False

app = FastAPI()
if _HAS_AUTH:
    app.add_middleware(ServiceAuthMiddleware)  # type: ignore[possibly-unbound]

LEARNER_DB = os.getenv(
    "LEARNER_DB",
    "/data/learner.duckdb",
)
store = DuckStore(LEARNER_DB)

# ── Strategy arms = Playbook IDs ──────────────
# The bandit arms are now concrete playbooks from
# the playbook catalog, not abstract strategy names.
STRATEGIES = PLAYBOOK_IDS

# Build addenda from playbooks.
_ADDENDA = {pb.playbook_id: pb.prompt_addendum for pb in PLAYBOOKS}

# ── Failure-class → playbook priors ───────────
# When the bandit has zero data for a context,
# use domain knowledge to pick the playbook
# designed for that failure class.
_FAILURE_STRATEGY_PRIORS: dict[str, str] = FAILURE_PLAYBOOK_PRIORS


def context_key(meta: dict) -> str:
    """Build context key for Thompson sampling.

    Includes failure_class and stage so the bandit
    can learn different strategies for different
    failure types at different pipeline stages.
    """
    lang = (meta.get("lang") or "py").strip().lower()
    tests = (meta.get("tests") or "pytest").strip().lower()
    fw = (meta.get("framework") or "unknown").strip().lower()
    fail = (meta.get("failure") or "none").strip().lower()
    stage = (meta.get("stage") or "unknown").strip().lower()
    repo = (meta.get("repo_id") or "unknown").strip().lower()
    # Use first 8 chars of repo_id to avoid extremely long keys
    repo_short = hashlib.sha256(repo.encode()).hexdigest()[:8]
    return f"{lang}|{tests}|{fw}|{fail}|{stage}|{repo_short}"


def _task_hash(repo_id: str, task: str) -> str:
    """Deterministic hash for task dedup."""
    blob = f"{repo_id}|{task}"
    return hashlib.sha256(
        blob.encode("utf-8"),
    ).hexdigest()[:16]


class SuggestReq(BaseModel):
    repo_id: str
    task: str
    meta: dict = {}
    failure_signature_hash: Optional[str] = None


class SuggestResp(BaseModel):
    context_key: str
    strategy_id: str
    prompt_addendum: str
    constraints: dict
    kernel_evidence: dict
    failure_hint: Optional[str] = None
    past_outcomes: Optional[list] = None
    playbook_id: Optional[str] = None
    playbook_guidance: Optional[str] = None


class IngestReq(BaseModel):
    run_id: str
    strategy_id: str
    meta: dict = {}
    success: bool
    failure_signature: str = ""
    # Extended fields for outcome mapping
    repo_id: str = ""
    task: str = ""
    patch_hash: str = ""
    patch_files: str = ""
    patch_added: int = 0
    patch_deleted: int = 0
    test_exit_code: int = -1
    tests_passed: int = 0
    tests_failed: int = 0
    tests_total: int = 0
    failure_class: str = ""
    dense_reward: float = 0.0
    stage: str = ""
    # Structured failure fields
    failure_module: str = ""
    failure_test: str = ""
    failure_message: str = ""
    failure_signature_hash: str = ""


_FORBIDDEN_PATCH_PATH_HINTS = (
    ".github/workflows/",
    "ci/",
    "scripts/",
    ".env",
    ".pem",
    "dockerfile",
    "docker-compose",
)


def _should_reject_ingest(req: IngestReq, task_hash: str) -> tuple[bool, str]:
    patch_files_low = (req.patch_files or "").lower()
    for marker in _FORBIDDEN_PATCH_PATH_HINTS:
        if marker in patch_files_low:
            return True, f"forbidden_patch_area:{marker}"
    if req.patch_hash:
        prev = store.latest_outcome(task_hash)
        if prev:
            if prev.get("patch_hash") == req.patch_hash and prev.get(
                "failure_signature", ""
            ) == (req.failure_signature or ""):
                return True, "duplicate_patch_fingerprint"
            if req.tests_total and prev.get("tests_total", 0):
                if int(req.tests_total) < int(prev.get("tests_total", 0)):
                    return True, "tests_reduced"
            if req.tests_failed and prev.get("tests_failed", 0):
                if int(req.tests_failed) > int(prev.get("tests_failed", 0)):
                    return True, "failures_increased"
    return False, ""


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/suggest", response_model=SuggestResp)
def suggest(req: SuggestReq):
    ck = context_key(req.meta)
    for sid in STRATEGIES:
        store.upsert_prior(sid, ck, a=1.0, b=1.0)

    post = store.get_posteriors(ck)

    # ── Failure-signature-aware routing ───────
    # If we've seen this failure before, bias
    # toward the strategy that worked last time.
    failure_hint: Optional[str] = None
    sig_boost_sid: Optional[str] = None
    known: Optional[dict] = None

    if req.failure_signature_hash:
        known = store.lookup_failure(
            req.failure_signature_hash,
        )
        if known and known.get("best_strategy_id"):
            sig_boost_sid = known["best_strategy_id"]
            wr = known.get(
                "best_strategy_win_rate",
                0,
            )
            failure_hint = (
                f"Known failure pattern"
                f" (seen {known['occurrence_count']}x)."
                f" Best strategy:"
                f" {sig_boost_sid}"
                f" (win rate: {wr:.0%})."
            )
            if known.get("failure_test"):
                failure_hint += f" Failing test:" f" {known['failure_test']}."

    # Thompson sampling over Beta posteriors,
    # with optional boost for known failures.
    best_sid = STRATEGIES[0]
    best_sample = -1.0
    for sid in STRATEGIES:
        a = float(
            post.get(sid, {}).get("alpha", 1.0),
        )
        b = float(
            post.get(sid, {}).get("beta", 1.0),
        )
        sample = random.betavariate(a, b)

        # Boost known-good strategy for this
        # failure signature.
        if sig_boost_sid and sid == sig_boost_sid:
            sample *= 1.5

        if sample > best_sample:
            best_sample = sample
            best_sid = sid

    # If we have zero data and know the failure
    # class, use the prior mapping.
    fc = (req.meta.get("failure") or "").strip()
    total_trials = sum(post.get(sid, {}).get("trials", 0) for sid in STRATEGIES)
    if total_trials == 0 and fc in _FAILURE_STRATEGY_PRIORS:
        best_sid = _FAILURE_STRATEGY_PRIORS[fc]

    addendum = _ADDENDA[best_sid]

    # ── Past outcome lookup ───────────────────
    past_outcomes: Optional[list] = None
    th = _task_hash(req.repo_id, req.task)
    history = store.lookup_patch_history(
        th,
        limit=5,
    )
    if history:
        past_outcomes = history

    # Identify known traps (actions that failed in this context)
    known_traps = []
    if history:
        # Simple heuristic: if we tried a patch and it failed with the SAME failure signature
        # as current (or just failed at all?), consider it a trap.
        # Ideally we'd map (strategy, action) -> outcome, but history is patch-based.
        # For now, let's just use the 'strategy_id' as a trap if it failed recently.
        for h in history:
            if not h["success"] and h["strategy_id"]:
                # This is loose, but meaningful: if strategy X failed here, warn about it
                known_traps.append(f"strategy:{h['strategy_id']}")
            # If we had finer grained action history here, we'd add it.

    # Learner recommends; Gate enforces final policy.
    # Constraints MUST match what gate_policy.yaml
    # and tool_gateway actually enforce.
    constraints = {
        "max_patch_files": 3,
        "max_patch_total_lines": 80,
        "max_added_lines": 40,
        "max_deleted_lines": 40,
        "forbid_test_edits": True,
        "enforce_tests": True,
    }

    # Look up the full playbook for guidance.
    pb = PLAYBOOK_MAP.get(best_sid)
    pb_guidance = pb.prompt_addendum if pb else None

    post_row = post.get(best_sid, {})
    alpha = float(post_row.get("alpha", 1.0))
    beta = float(post_row.get("beta", 1.0))
    trials = int(post_row.get("trials", 0))
    posterior_mean = alpha / max(alpha + beta, 1e-9)
    failure_occurrence = int(
        (known or {}).get("occurrence_count", 0),
    )
    failure_best_win_rate = float(
        (known or {}).get("best_strategy_win_rate", 0.0) or 0.0,
    )
    kernel_evidence = {
        "strategy_id": best_sid,
        "context_key": ck,
        "prior_success_prob": round(
            posterior_mean,
            4,
        ),
        "prior_trials": trials,
        "failure_occurrence": failure_occurrence,
        "failure_best_win_rate": round(
            failure_best_win_rate,
            4,
        ),
        "known_traps": list(set(known_traps)),
    }

    return SuggestResp(
        context_key=ck,
        strategy_id=best_sid,
        prompt_addendum=addendum,
        constraints=constraints,
        kernel_evidence=kernel_evidence,
        failure_hint=failure_hint,
        past_outcomes=past_outcomes,
        playbook_id=best_sid,
        playbook_guidance=pb_guidance,
    )


@app.post("/ingest")
def ingest(req: IngestReq):
    # Inject stage into meta for context_key.
    meta = dict(req.meta)
    if req.stage:
        meta["stage"] = req.stage
    ck = context_key(meta)

    th = _task_hash(req.repo_id, req.task)
    reject, reason = _should_reject_ingest(req, th)
    if reject:
        return {
            "ok": True,
            "ignored": True,
            "reason": reason,
            "patch_fingerprint": req.patch_hash or "",
        }

    # Record episode for Thompson sampling.
    store.record_episode(
        run_id=req.run_id,
        context_key=ck,
        strategy_id=req.strategy_id,
        success=bool(req.success),
        failure_signature=req.failure_signature or "",
    )

    # Record outcome mapping if we have patch data.
    if req.patch_hash:
        store.record_outcome(
            run_id=req.run_id,
            repo_id=req.repo_id,
            task_hash=th,
            patch_hash=req.patch_hash,
            patch_files=req.patch_files,
            patch_added=req.patch_added,
            patch_deleted=req.patch_deleted,
            test_exit_code=req.test_exit_code,
            tests_passed=req.tests_passed,
            tests_failed=req.tests_failed,
            tests_total=req.tests_total,
            failure_class=req.failure_class,
            failure_signature=(req.failure_signature or ""),
            strategy_id=req.strategy_id,
            success=bool(req.success),
            dense_reward=req.dense_reward,
        )

    # Index the failure signature if present.
    if req.failure_signature_hash and req.failure_class:
        best_sid = None
        best_wr = None
        if req.success:
            best_sid = req.strategy_id
            best_wr = 1.0
        store.upsert_failure(
            signature_hash=(req.failure_signature_hash),
            failure_class=req.failure_class,
            failure_module=req.failure_module or "",
            failure_test=req.failure_test or "",
            failure_message=(req.failure_message or ""),
            repo_id=req.repo_id,
            best_strategy_id=best_sid,
            best_win_rate=best_wr,
        )

    return {
        "ok": True,
        "ignored": False,
        "patch_fingerprint": req.patch_hash or "",
    }


# ── Query endpoints for outcome intelligence ──


@app.get("/outcomes/{repo_id}")
def get_outcomes(repo_id: str, limit: int = 20):
    """Get recent outcomes for a repo."""
    rows = store.con.execute(
        """
        SELECT run_id, task_hash, patch_hash,
               success, dense_reward,
               strategy_id, failure_class,
               tests_passed, tests_failed,
               ts
        FROM outcome_map
        WHERE repo_id = ?
        ORDER BY ts DESC
        LIMIT ?
        """,
        [repo_id, limit],
    ).fetchall()
    return [
        {
            "run_id": r[0],
            "task_hash": r[1],
            "patch_hash": r[2],
            "success": bool(r[3]),
            "dense_reward": float(r[4]),
            "strategy_id": r[5],
            "failure_class": r[6],
            "tests_passed": int(r[7]),
            "tests_failed": int(r[8]),
        }
        for r in rows
    ]


@app.get("/failures")
def get_failures(
    failure_class: Optional[str] = None,
    limit: int = 20,
):
    """Get indexed failure signatures."""
    if failure_class:
        return store.find_similar_failures(
            failure_class,
            limit=limit,
        )
    rows = store.con.execute(
        """
        SELECT signature_hash, failure_class,
               failure_module, failure_test,
               occurrence_count,
               best_strategy_id,
               best_strategy_win_rate
        FROM failure_index
        ORDER BY occurrence_count DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    return [
        {
            "signature_hash": r[0],
            "failure_class": r[1],
            "failure_module": r[2],
            "failure_test": r[3],
            "occurrence_count": int(r[4]),
            "best_strategy_id": r[5],
            "best_strategy_win_rate": (float(r[6]) if r[6] else None),
        }
        for r in rows
    ]


@app.get("/strategy_stats")
def get_strategy_stats():
    """Get all strategy stats across contexts."""
    rows = store.con.execute(
        """
        SELECT strategy_id, context_key,
               trials, wins, losses,
               alpha, beta
        FROM strategy_stats
        WHERE trials > 0
        ORDER BY trials DESC
        LIMIT 100
        """,
    ).fetchall()
    return [
        {
            "strategy_id": r[0],
            "context_key": r[1],
            "trials": int(r[2]),
            "wins": int(r[3]),
            "losses": int(r[4]),
            "alpha": float(r[5]),
            "beta": float(r[6]),
            "win_rate": (int(r[3]) / max(int(r[2]), 1)),
        }
        for r in rows
    ]
