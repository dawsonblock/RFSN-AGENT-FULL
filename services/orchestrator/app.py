import hashlib
import json
import os
from typing import Optional

from fastapi import FastAPI, HTTPException  # type: ignore[import-not-found]
from pydantic import BaseModel  # type: ignore[import-not-found]
import requests  # type: ignore[import-untyped]
import yaml  # type: ignore[import-untyped]

from context_fingerprint import build_context  # type: ignore[import-not-found]
from kernel import Kernel  # type: ignore[import-not-found]
from ledger import Ledger  # type: ignore[import-not-found]
from prompts import SYSTEM, USER_TEMPLATE  # type: ignore[import-not-found]

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
LEARNER_URL = os.getenv("LEARNER_URL", "http://learner_service:8004")
SEED = os.getenv("RFSN_SEED", "1")


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

kernel = Kernel(
    "/shared/bundle_schema.json",
    "/policies/tool_allowlist.yaml",
    "/policies/gate_policy.yaml",
)
ledger = Ledger("/data/ledger.jsonl")


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
    return {"ok": True}


def run_step(repo_id: str, it: int, step: dict):
    r = requests.post(
        f"{TOOL_GATEWAY_URL}/run_step",
        json={"repo_id": repo_id, "iter": it, "step": step},
        headers=auth_headers(),
        timeout=300,
    )
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    return r.json()


def llm_chat(
    messages: list, run_id: str,
    call_index: int, repo_id: str,
    scenario: str,
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
    r = requests.post(
        f"{LLM_URL}/chat",
        json=payload,
        headers=auth_headers(),
        timeout=120,
    )
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    return r.json()


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
) -> dict:
    repo_path = f"/data/repos/{repo_id}"
    ctx = build_context(repo_path, last_fail)

    payload = {
        "repo_id": repo_id,
        "task": task,
        "meta": ctx,
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
        f"|{ctx['framework']}"
    )
    return {
        "context_key": ck,
        "strategy_id": (
            "S1_search_then_patch_small"
        ),
        "prompt_addendum": (
            "Strategy: search first,"
            " narrow reads,"
            " patch minimal. No refactor."
        ),
        "constraints": {
            "max_patch_files": 6,
            "max_patch_total_lines": 300,
            "forbid_test_edits": True,
        },
    }


def learner_ingest(
    run_id: str,
    strategy_id: str,
    success: bool,
    fail_sig: str,
    repo_id: str = "",
    last_fail: str = "",
) -> None:
    repo_path = f"/data/repos/{repo_id}"
    ctx = build_context(
        repo_path, last_fail,
    )
    payload = {
        "run_id": run_id,
        "strategy_id": strategy_id,
        "meta": ctx,
        "success": bool(success),
        "failure_signature": fail_sig or "",
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
    scenario = req.scenario or "golden"
    run_id = stable_id(
        "run", SEED, req.repo_id, req.task,
        str(req.max_iters), scenario, n=10,
    )

    ledger.append({
        "type": "RUN_START",
        "run_id": run_id,
        "repo_id": req.repo_id,
        "task": req.task,
        "scenario": scenario,
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

        decision = kernel.validate_and_plan(bundle)
        ledger.append({
            "type": "KERNEL_DECISION",
            "run_id": run_id,
            "decision": decision,
        })
        if not decision["ok"]:
            return {
                "run_id": run_id,
                "status": "rejected",
                "errors": decision["errors"],
            }

        results = []
        for s in decision["approved_steps"]:
            out = run_step(req.repo_id, it, s)
            ledger.append({
                "type": "STEP_RESULT",
                "run_id": run_id,
                "iter": it,
                "step": s,
                "out": out,
            })
            results.append({"step": s, "out": out})
            if out.get("status", 0) != 0:
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
        ledger.append({
            "type": "RUN_END",
            "run_id": run_id,
            "status": "ok",
        })
        return {
            "run_id": run_id,
            "status": "ok",
            "results": results,
        }

    # LLM loop
    call_index = 0
    last_fail = ""
    last_strategy = None
    for it in range(1, req.max_iters + 1):
        fail_ctx = (
            "\n\nLast failure:\n" + last_fail
            if last_fail else ""
        )

        sug = learner_suggest(
            req.repo_id, req.task, last_fail,
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
            "context_key": sug.get(
                "context_key",
            ),
            "constraints": constraints,
        })

        prompt = USER_TEMPLATE.format(
            repo_id=req.repo_id,
            task=req.task + fail_ctx,
            learner_addendum=sug.get(
                "prompt_addendum", "",
            ),
            max_patch_files=constraints.get(
                "max_patch_files", 6,
            ),
            max_patch_total_lines=constraints.get(
                "max_patch_total_lines", 300,
            ),
            forbid_test_edits=constraints.get(
                "forbid_test_edits", True,
            ),
        )
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ]

        # Multi-proposal: request N candidates,
        # gate-validate all, pick lowest risk.
        n_candidates = 3
        candidates: list = []
        for ci in range(n_candidates):
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
                continue
            content = llm.get("content", "")
            try:
                cand = json.loads(content)
                if (
                    isinstance(cand, dict)
                    and "steps" in cand
                ):
                    candidates.append(cand)
            except Exception:
                continue

        if not candidates:
            last_fail = (
                "All LLM proposals"
                " failed to parse"
            )
            ledger.append({
                "type": "BUNDLE_PARSE_FAIL",
                "run_id": run_id,
                "iter": it,
            })
            continue

        # Pick lowest-risk valid candidate.
        bundle = candidates[0]
        best_risk = 10**9
        best_dec = None
        for cand in candidates:
            # Normalize before gate check
            if "bundle_id" not in cand:
                cand["bundle_id"] = stable_id(
                    "b", SEED, req.repo_id,
                    req.task, str(it),
                    scenario, n=8,
                )
            for idx, st in enumerate(
                cand.get("steps", []),
            ):
                if not st.get("id"):
                    st["id"] = f"s{idx+1}"

            # Inject auto-deps per candidate
            cand_steps = cand.get("steps", [])
            c_needs_tests = any(
                s.get("type") == "run_tests"
                for s in cand_steps
            )
            c_needs_deps = (
                c_needs_tests
                and DEPS_POLICY.get(
                    "enabled", True,
                )
                and not venv_exists(
                    req.repo_id,
                )
            )
            if c_needs_deps and not any(
                s.get("type") == "ensure_deps"
                for s in cand_steps
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
                cand["steps"] = (
                    [dep_step] + cand_steps
                )

            d = kernel.validate_and_plan(cand)
            if (
                d["ok"]
                and d.get(
                    "risk_score", 0,
                ) < best_risk
            ):
                best_risk = d.get(
                    "risk_score", 0,
                )
                best_dec = d
                bundle = cand

        ledger.append({
            "type": "BUNDLE_PROPOSED",
            "run_id": run_id,
            "iter": it,
            "bundle": bundle,
            "candidates_count": len(
                candidates,
            ),
        })

        if best_dec is not None:
            decision = best_dec
        else:
            # All rejected; re-validate first
            # candidate to get error details.
            decision = (
                kernel.validate_and_plan(
                    bundle,
                )
            )
        ledger.append({
            "type": "KERNEL_DECISION",
            "run_id": run_id,
            "iter": it,
            "decision": decision,
        })
        if not decision["ok"]:
            last_fail = json.dumps(
                decision["errors"],
            )
            learner_ingest(
                run_id,
                last_strategy or "unknown",
                False,
                failure_signature(last_fail),
                repo_id=req.repo_id,
                last_fail=last_fail,
            )
            continue

        approved = list(
            decision["approved_steps"],
        )

        results = []
        ok = True
        for s in approved:
            out = run_step(req.repo_id, it, s)
            ledger.append({
                "type": "STEP_RESULT",
                "run_id": run_id,
                "iter": it,
                "step": s,
                "out": out,
            })
            results.append(
                {"step": s, "out": out}
            )

            if (
                s.get("type") == "apply_patch"
                and out.get("status", 0) != 0
            ):
                ledger.append({
                    "type": "PATCH_REJECTED",
                    "run_id": run_id,
                    "iter": it,
                    "status": out.get(
                        "status", 0
                    ),
                })

            if out.get("status", 0) != 0:
                ok = False
                last_fail = (
                    out.get("logs", "")[:5000]
                )
                learner_ingest(
                    run_id,
                    last_strategy or "unknown",
                    False,
                    failure_signature(last_fail),
                    repo_id=req.repo_id,
                    last_fail=last_fail,
                )
                break

        if ok:
            # Enforce acceptance criteria from the bundle
            acceptance: dict = bundle.get(  # type: ignore[assignment]
                "acceptance", {},
            )
            if acceptance.get("tests_green", False):
                test_results = [
                    r for r in results
                    if r["step"].get("type")
                    == "run_tests"
                ]
                tests_passed = all(
                    r["out"].get("status", 1) == 0
                    for r in test_results
                )
                if test_results and not tests_passed:
                    last_fail = (
                        "acceptance.tests_green"
                        " not met: some tests"
                        " returned non-zero"
                    )
                    ok = False
            # Run static analysis if test_policy
            # says so — gate-validated first.
            if ok and TEST_POLICY.get(
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
                    sa_bundle = {
                        "intent": "static analysis",
                        "bundle_id": stable_id(
                            "sa", run_id,
                            str(it), n=8,
                        ),
                        "steps": sa_steps,
                        "acceptance": {},
                    }
                    sa_dec = (
                        kernel.validate_and_plan(
                            sa_bundle,
                        )
                    )
                    ledger.append({
                        "type": "KERNEL_DECISION",
                        "run_id": run_id,
                        "iter": it,
                        "decision": sa_dec,
                    })
                    if sa_dec["ok"]:
                        for sa_s in sa_dec[
                            "approved_steps"
                        ]:
                            sa_out = run_step(
                                req.repo_id,
                                it,
                                sa_s,
                            )
                            ledger.append({
                                "type": (
                                    "STEP_RESULT"
                                ),
                                "run_id": run_id,
                                "iter": it,
                                "step": sa_s,
                                "out": sa_out,
                            })
                            results.append({
                                "step": sa_s,
                                "out": sa_out,
                            })
                            # Static: non-blocking

        if ok:
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
            )
            return {
                "run_id": run_id,
                "status": "ok",
                "results": results,
            }

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
    )
    return {
        "run_id": run_id,
        "status": "fail",
    }
