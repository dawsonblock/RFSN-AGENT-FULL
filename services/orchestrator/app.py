import hashlib
import json
import os
from typing import Optional

from fastapi import FastAPI, HTTPException  # type: ignore[import-not-found]
from pydantic import BaseModel  # type: ignore[import-not-found]
import requests  # type: ignore[import-untyped]
import yaml  # type: ignore[import-untyped]

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

kernel = Kernel("/shared/bundle_schema.json", "/policies/tool_allowlist.yaml")
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
    for it in range(1, req.max_iters + 1):
        fail_ctx = (
            "\n\nLast failure:\n" + last_fail
            if last_fail else ""
        )
        prompt = USER_TEMPLATE.format(
            repo_id=req.repo_id,
            task=req.task + fail_ctx,
        )
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ]

        call_index += 1
        ledger.append({
            "type": "LLM_CALL",
            "run_id": run_id,
            "call_index": call_index,
            "iter": it,
        })
        llm = llm_chat(
            messages, run_id, call_index,
            req.repo_id, scenario,
        )
        content = llm.get("content", "")
        try:
            bundle = json.loads(content)
        except Exception as e:
            last_fail = (
                f"LLM returned non-JSON: {e}"
            )
            ledger.append({
                "type": "BUNDLE_PARSE_FAIL",
                "run_id": run_id,
                "iter": it,
            })
            continue

        if "bundle_id" not in bundle:
            bundle["bundle_id"] = stable_id(
                "b", SEED, req.repo_id,
                req.task, str(it), scenario,
                n=8,
            )
        # normalize missing step ids
        for i, s in enumerate(bundle.get("steps", [])):
            if not s.get("id"):
                s["id"] = f"s{i+1}"

        ledger.append({
            "type": "BUNDLE_PROPOSED",
            "run_id": run_id,
            "iter": it,
            "bundle": bundle,
        })

        decision = kernel.validate_and_plan(bundle)
        ledger.append({
            "type": "KERNEL_DECISION",
            "run_id": run_id,
            "iter": it,
            "decision": decision,
        })
        if not decision["ok"]:
            last_fail = json.dumps(decision["errors"])
            continue

        # Optionally insert deps if tests exist and venv missing
        approved = list(decision["approved_steps"])
        needs_tests = any(
            s.get("type") == "run_tests"
            for s in approved
        )
        needs_deps = (
            needs_tests
            and DEPS_POLICY.get("enabled", True)
            and not venv_exists(req.repo_id)
        )
        if needs_deps:
            dep_step = {
                "id": "auto-deps",
                "type": "ensure_deps",
                "manifest": DEPS_POLICY.get(
                    "manifest",
                    "requirements.txt",
                ),
                "timeout_s": int(
                    DEPS_POLICY.get(
                        "max_install_seconds", 420
                    )
                ),
            }
            approved = [dep_step] + approved

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
                last_fail = out.get("logs", "")[:5000]
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
            # Run static analysis if test_policy says so
            if ok and TEST_POLICY.get(
                "suite_on_success", False
            ):
                for sa_tmpl in TEST_POLICY.get(
                    "static_templates", []
                ):
                    sa_step = {
                        "id": f"auto-{sa_tmpl}",
                        "type": "run_tests",
                        "template_id": sa_tmpl,
                        "template_params": {
                            "target": "",
                        },
                        "timeout_s": 300,
                    }
                    sa_out = run_step(
                        req.repo_id, it, sa_step,
                    )
                    ledger.append({
                        "type": "STEP_RESULT",
                        "run_id": run_id,
                        "iter": it,
                        "step": sa_step,
                        "out": sa_out,
                    })
                    results.append({
                        "step": sa_step,
                        "out": sa_out,
                    })
                    # Static analysis: non-blocking

        if ok:
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

    ledger.append({
        "type": "RUN_END",
        "run_id": run_id,
        "status": "fail",
    })
    return {
        "run_id": run_id,
        "status": "fail",
    }
