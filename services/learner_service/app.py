import os
import random
import sys

from fastapi import FastAPI  # type: ignore[import-not-found]
from pydantic import BaseModel  # type: ignore[import-not-found]

from store_duckdb import DuckStore  # type: ignore[import-not-found]

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
    app.add_middleware(
        ServiceAuthMiddleware  # type: ignore[possibly-unbound]
    )

LEARNER_DB = os.getenv(
    "LEARNER_DB", "/data/learner.duckdb",
)
store = DuckStore(LEARNER_DB)

STRATEGIES = [
    "S1_search_then_patch_small",
    "S2_patch_small_targeted_first",
    "S3_error_signature_driven",
    "S4_dependency_first",
    "S5_refactor_blocker",
]

_ADDENDA = {
    "S1_search_then_patch_small": (
        "Strategy: search first, narrow reads,"
        " patch minimal. No refactor."
        " Keep diff small. Run pytest_targeted"
        " first; suite only after green."
    ),
    "S2_patch_small_targeted_first": (
        "Strategy: patch minimal immediately."
        " No refactor. Run pytest_targeted"
        " first; suite only after green."
    ),
    "S3_error_signature_driven": (
        "Strategy: use failing stacktrace/error"
        " signature to locate exact code."
        " Patch minimal. Run pytest_targeted"
        " first; suite only after green."
    ),
    "S4_dependency_first": (
        "Strategy: if import/build errors, fix"
        " deps/install first; then patch minimal."
        " Run pytest_targeted first; suite only"
        " after green."
    ),
    "S5_refactor_blocker": (
        "Strategy: forbid refactor. Only surgical"
        " fix. Diff must be tiny. Run"
        " pytest_targeted first; suite only"
        " after green."
    ),
}


def context_key(meta: dict) -> str:
    lang = (meta.get("lang") or "py").strip().lower()
    tests = (
        (meta.get("tests") or "pytest").strip().lower()
    )
    fw = (
        (meta.get("framework") or "unknown")
        .strip()
        .lower()
    )
    return f"{lang}|{tests}|{fw}"


class SuggestReq(BaseModel):
    repo_id: str
    task: str
    meta: dict = {}


class SuggestResp(BaseModel):
    context_key: str
    strategy_id: str
    prompt_addendum: str
    constraints: dict


class IngestReq(BaseModel):
    run_id: str
    strategy_id: str
    meta: dict = {}
    success: bool
    failure_signature: str = ""


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/suggest", response_model=SuggestResp)
def suggest(req: SuggestReq):
    ck = context_key(req.meta)
    for sid in STRATEGIES:
        store.upsert_prior(sid, ck, a=1.0, b=1.0)

    post = store.get_posteriors(ck)

    # Thompson sampling over Beta posteriors.
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
        if sample > best_sample:
            best_sample = sample
            best_sid = sid

    addendum = _ADDENDA[best_sid]

    # Learner recommends; Gate enforces final policy.
    constraints = {
        "max_patch_files": 6,
        "max_patch_total_lines": 300,
        "forbid_test_edits": True,
        "enforce_tests": True,
    }

    return SuggestResp(
        context_key=ck,
        strategy_id=best_sid,
        prompt_addendum=addendum,
        constraints=constraints,
    )


@app.post("/ingest")
def ingest(req: IngestReq):
    ck = context_key(req.meta)
    store.record_episode(
        run_id=req.run_id,
        context_key=ck,
        strategy_id=req.strategy_id,
        success=bool(req.success),
        failure_signature=req.failure_signature or "",
    )
    return {"ok": True}
