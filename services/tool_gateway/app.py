import os
import re
from typing import Optional, List
import json

from fastapi import FastAPI, HTTPException, Header  # type: ignore[import-not-found]
from pydantic import BaseModel  # type: ignore[import-not-found]
import yaml  # type: ignore[import-untyped]
import requests  # type: ignore[import-untyped]

from policy import (
    validate_repo_path,
    extract_patch_touched_paths,
)
from workdir_store import WorkdirStore

# Import the RFSN patch risk gate as final authority.
try:
    from rfsn_swebench.gate import patch_risk_gate as _patch_risk_gate

    _HAS_PATCH_GATE = True
except ImportError:
    _HAS_PATCH_GATE = False
    _patch_risk_gate = None  # type: ignore[assignment]
    print(
        "CRITICAL: rfsn_swebench.gate not available — "
        "patch_risk_gate enforcement DISABLED",
        flush=True,
    )

# ── Patch-gate-required guard ──────────────────────────
_PATCH_GATE_REQUIRED = os.getenv("RFSN_PATCH_GATE_REQUIRED", "1") == "1"
if not _HAS_PATCH_GATE and _PATCH_GATE_REQUIRED:
    if os.getenv("RFSN_DEV_MODE", "0") != "1":
        raise SystemExit(
            "FATAL: patch_risk_gate not available and "
            "RFSN_PATCH_GATE_REQUIRED=1. Set RFSN_DEV_MODE=1 to bypass."
        )

# ── Startup status logging ─────────────────────────────
print(
    f"patch_gate={'enabled' if _HAS_PATCH_GATE else 'disabled'} "
    f"patch_gate_required={'true' if _PATCH_GATE_REQUIRED else 'false'}",
    flush=True,
)

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

    def auth_headers():
        return {}


# ── Auth-required guard ────────────────────────────────
_AUTH_REQUIRED = os.getenv("RFSN_AUTH_REQUIRED", "1") == "1"
if not _HAS_AUTH and _AUTH_REQUIRED:
    if os.getenv("RFSN_DEV_MODE", "0") != "1":
        raise SystemExit(
            "FATAL: auth module not available and RFSN_AUTH_REQUIRED=1. "
            "Set RFSN_DEV_MODE=1 to bypass (dev only)."
        )

app = FastAPI()
if _HAS_AUTH:
    app.add_middleware(ServiceAuthMiddleware)  # type: ignore[possibly-unbound]

EXECUTOR_URL = os.getenv("EXECUTOR_URL", "http://executor:8003")


def _load_yaml(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (FileNotFoundError, PermissionError) as exc:
        print(
            f"FATAL: Cannot load policy file {path}:" f" {exc}",
            flush=True,
        )
        raise SystemExit(1) from exc


ALLOW = _load_yaml("/policies/tool_allowlist.yaml")
DIFF_GUARD = _load_yaml("/policies/diff_guard.yaml")

# Load kernel gate policy for unified budget
# enforcement (single source of truth).
GATE_POLICY = _load_yaml(
    "/policies/gate_policy.yaml",
)
RUN_TEST_TEMPLATES = (
    _load_yaml("/policies/command_templates.yaml").get("templates", {}) or {}
)

# ── Unified patch limits from gate_policy ──────────
# tool_gateway now enforces the SAME limits that the
# kernel gate and LLM prompt advertise.
_EFFECTIVE_MAX_FILES = int(
    GATE_POLICY.get("max_patch_files", 3),
)
_EFFECTIVE_MAX_ADDED = int(
    GATE_POLICY.get("max_added_lines", 40),
)
_EFFECTIVE_MAX_DELETED = int(
    GATE_POLICY.get("max_deleted_lines", 40),
)
_EFFECTIVE_MAX_TOTAL = int(
    GATE_POLICY.get(
        "max_patch_total_lines",
        _EFFECTIVE_MAX_ADDED + _EFFECTIVE_MAX_DELETED,
    ),
)
_BLOCKED_READ_PREFIXES = tuple(
    str(x)
    for x in (GATE_POLICY.get("blocked_read_prefixes", []) or [])
    if isinstance(x, str) and x.strip()
)
_BLOCKED_READ_SUFFIXES = tuple(
    str(x)
    for x in (GATE_POLICY.get("blocked_read_suffixes", []) or [])
    if isinstance(x, str) and x.strip()
)
NETWORK_MIN_TIER = int(
    GATE_POLICY.get("network_min_tier", 2),
)

ALLOWED_TYPES = set(ALLOW.get("allowed_step_types", []))
ALLOWED_PATHS = ALLOW.get("allowed_paths", ["**"])
BLOCKED_GLOBS = ALLOW.get("blocked_globs", [])
REPO_ROOT_REQUIRED = bool(
    ALLOW.get("repo_root_required", True),
)

MAX_PATCH_BYTES = int(ALLOW.get("max_patch_bytes", 200000))
MAX_READ_BYTES = int(ALLOW.get("max_read_bytes", 200000))
MAX_READ_FILE_BYTES = int(ALLOW.get("max_read_file_bytes", 65536))
MAX_WORKDIRS = int(ALLOW.get("max_workdirs", 24))
FORMAT_FIX_MIN_TIER = int(ALLOW.get("format_fix_min_tier", 2))
COMMAND_TEMPLATES = ALLOW.get("command_templates", {}) or {}
WORKDIR_RE = re.compile(r"^workdir_\d+$")
_WORKDIRS = WorkdirStore()

MAX_READ_STEPS = int(ALLOW.get("max_read_steps_per_iter", 6))
MAX_SEARCH_STEPS = int(ALLOW.get("max_search_steps_per_iter", 4))
MAX_BYTES_PER_ITER = int(ALLOW.get("max_bytes_returned_per_iter", 250000))

# (repo_id, run_scope, iter) -> usage
ITER_USAGE: dict[tuple[str, str, int], dict] = {}


def _norm_template_cmd(value) -> list[str] | None:
    if isinstance(value, list) and value:
        out = [str(x) for x in value if isinstance(x, str)]
        return out if out else None
    if isinstance(value, dict):
        cmd = value.get("cmd")
        if isinstance(cmd, list) and cmd:
            out = [str(x) for x in cmd if isinstance(x, str)]
            return out if out else None
    return None


def _check_template_registry_drift() -> None:
    overlap = set(COMMAND_TEMPLATES.keys()) & set(RUN_TEST_TEMPLATES.keys())
    for name in sorted(overlap):
        a = _norm_template_cmd(COMMAND_TEMPLATES.get(name))
        b = _norm_template_cmd(RUN_TEST_TEMPLATES.get(name))
        if a is None or b is None:
            continue
        if a != b:
            raise SystemExit(
                "Template registry drift detected for"
                f" '{name}': tool_allowlist.yaml and"
                " command_templates.yaml differ",
            )


_check_template_registry_drift()


def usage_key(
    repo_id: str,
    it: int,
    run_id: str | None = None,
):
    scope = run_id or f"repo:{repo_id}"
    return (repo_id, scope, it)


def charge(
    repo_id: str,
    it: int,
    kind: str,
    bytes_out: int = 0,
    run_id: str | None = None,
):
    k = usage_key(repo_id, it, run_id)
    u = ITER_USAGE.setdefault(k, {"reads": 0, "searches": 0, "bytes": 0})
    if kind == "read":
        u["reads"] += 1
        if u["reads"] > MAX_READ_STEPS:
            raise HTTPException(429, "read budget exceeded")
    if kind == "search":
        u["searches"] += 1
        if u["searches"] > MAX_SEARCH_STEPS:
            raise HTTPException(429, "search budget exceeded")
    if bytes_out:
        u["bytes"] += int(bytes_out)
        if u["bytes"] > MAX_BYTES_PER_ITER:
            raise HTTPException(429, "bytes budget exceeded")

    # prune old iters per run-scope (keep last 5)
    keys = [kk for kk in ITER_USAGE.keys() if kk[0] == repo_id and kk[1] == k[1]]
    iters = sorted({kk[2] for kk in keys})
    if len(iters) > 5:
        drop = set(iters[:-5])
        for kk in list(ITER_USAGE.keys()):
            if kk[0] == repo_id and kk[1] == k[1] and kk[2] in drop:
                del ITER_USAGE[kk]


class Step(BaseModel):
    id: str
    type: str
    pattern: Optional[str] = None
    path: Optional[str] = None
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    patch: Optional[str] = None
    template_id: Optional[str] = None
    template_params: Optional[dict] = None  # type: ignore[type-arg]
    manifest: Optional[str] = None
    mode: Optional[str] = None
    timeout_s: Optional[int] = None
    template: Optional[str] = None
    workdir_id: Optional[str] = None
    workdir: Optional[str] = None
    max_depth: Optional[int] = None
    # Phase 3 Fields
    focus: Optional[List[str]] = None  # for generate_repo_map
    target_file: Optional[str] = None  # for trace_execution
    lineno: Optional[int] = None  # for trace_execution
    variables: Optional[List[str]] = None  # for trace_execution
    triggering_test: Optional[str] = None  # for trace_execution


class RunStepReq(BaseModel):
    repo_id: str
    iter: int
    step: Step
    run_id: Optional[str] = None
    tier: Optional[int] = None
    warm_sandbox: Optional[bool] = None


class RunCleanupReq(BaseModel):
    run_id: str


def _effective_tier(
    body_tier: Optional[int],
    header_tier: Optional[str],
) -> int:
    if body_tier is not None:
        try:
            return max(0, int(body_tier))
        except (TypeError, ValueError):
            return 0
    if header_tier:
        try:
            return max(0, int(header_tier))
        except (TypeError, ValueError):
            return 0
    return 0


def _resolve_workdir(
    *,
    run_id: str | None,
    workdir_id: str,
) -> str:
    rel = _WORKDIRS.get_rel(run_id or "", workdir_id)
    if not rel:
        raise HTTPException(
            403,
            f"unknown workdir_id: {workdir_id}",
        )
    return rel


def _store_workdirs(
    run_id: str | None,
    out: dict,
) -> None:
    if not run_id:
        return

    payload = out.get("payload")
    parsed = None
    if isinstance(payload, str) and payload.strip():
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            parsed = None

    if not isinstance(parsed, dict):
        return
    workdirs = parsed.get("workdirs")
    if not isinstance(workdirs, list):
        return
    mapping = {}
    for item in workdirs[:MAX_WORKDIRS]:
        if not isinstance(item, dict):
            continue
        wid = item.get("id")
        rel = item.get("rel")
        if isinstance(wid, str) and isinstance(rel, str):
            mapping[wid] = rel
    if mapping:
        _WORKDIRS.set_run_workdirs(run_id, mapping)


@app.get("/health")
def health():
    return {"ok": True}


def _executor(
    step: dict,
    repo_id: str,
    it: int,
    run_id: str | None = None,
    run_warm: bool = False,
):
    step_type = str(step.get("type") or "")
    force_cold = step_type == "ensure_deps" and bool(step.get("_rfsn_allow_network"))
    if run_id and run_warm and not force_cold:
        # Route through warm sandbox.
        r = requests.post(
            f"{EXECUTOR_URL}/run_warm",
            json={
                "run_id": run_id,
                "repo_id": repo_id,
                "step": step,
            },
            headers=auth_headers(),
            timeout=600,
        )
    else:
        # Cold (ephemeral) execution.
        r = requests.post(
            f"{EXECUTOR_URL}/run",
            json={
                "repo_id": repo_id,
                "iter": it,
                "step": step,
            },
            headers=auth_headers(),
            timeout=600,
        )
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    return r.json()


@app.post("/run_cleanup")
def run_cleanup(req: RunCleanupReq):
    _WORKDIRS.clear(req.run_id)
    return {"ok": True}


@app.post("/run_step")
def run_step(
    req: RunStepReq,
    x_rfsn_tier: Optional[str] = Header(default=None),
):
    s = req.step.model_dump()
    repo_root = f"/data/repos/{req.repo_id}"
    tier = _effective_tier(req.tier, x_rfsn_tier)

    if s["type"] not in ALLOWED_TYPES:
        raise HTTPException(403, f"step type blocked: {s['type']}")
    if s["type"] in ("repo_search", "detect_project", "detect_workdirs"):
        charge(req.repo_id, req.iter, "search", run_id=req.run_id)

    if s["type"] == "repo_read_range":
        p = s.get("path") or ""
        if not validate_repo_path(
            p,
            ALLOWED_PATHS,
            BLOCKED_GLOBS,
            repo_root_required=REPO_ROOT_REQUIRED,
            repo_root=repo_root,
            blocked_prefixes=_BLOCKED_READ_PREFIXES,
            blocked_suffixes=_BLOCKED_READ_SUFFIXES,
        ):
            raise HTTPException(403, "path blocked")
        charge(req.repo_id, req.iter, "read", run_id=req.run_id)
    if s["type"] == "read_file":
        p = s.get("path") or ""
        if not validate_repo_path(
            p,
            ALLOWED_PATHS,
            BLOCKED_GLOBS,
            repo_root_required=REPO_ROOT_REQUIRED,
            repo_root=repo_root,
            blocked_prefixes=_BLOCKED_READ_PREFIXES,
            blocked_suffixes=_BLOCKED_READ_SUFFIXES,
        ):
            raise HTTPException(403, "path blocked")
        charge(req.repo_id, req.iter, "read", run_id=req.run_id)
    if s["type"] == "detect_workdirs":
        max_depth = int(s.get("max_depth") or 4)
        if max_depth < 1 or max_depth > 8:
            raise HTTPException(403, "invalid max_depth")
    if s["type"] == "run_tests":
        template_id = str(s.get("template_id") or "").strip()
        if not template_id:
            raise HTTPException(403, "run_tests template_id required")
        if template_id not in RUN_TEST_TEMPLATES:
            raise HTTPException(
                403,
                f"unknown run_tests template_id: {template_id}",
            )
        # Gateway-side target sanitizer (defense-in-depth).
        target = str(s.get("target") or "")
        if target:
            _GW_SHELL_REJECT = frozenset(" \t\n\r;$`|&(){}[]<>\\!")
            if any(c in _GW_SHELL_REJECT for c in target):
                raise HTTPException(
                    403,
                    f"target contains forbidden character: {target!r}",
                )
            # Strict pytest target pattern:
            # path/to/file.py  or  path/to/file.py::Class::test
            _PYTEST_TARGET_RE = re.compile(r"^[A-Za-z0-9_./:-]+(::[A-Za-z0-9_]+)*$")
            if template_id == "pytest_targeted":
                if not _PYTEST_TARGET_RE.fullmatch(target):
                    raise HTTPException(
                        403,
                        f"invalid pytest target pattern: {target!r}",
                    )
    if s["type"] in ("run_cmd_template", "format_fix"):
        template = str(s.get("template") or "").strip()
        if not template:
            raise HTTPException(403, "template required")
        if template not in COMMAND_TEMPLATES:
            raise HTTPException(403, f"unknown template: {template}")
        if "workdir" in s and s.get("workdir"):
            raise HTTPException(403, "raw workdir is not allowed")
        if "cwd" in s and s.get("cwd"):
            raise HTTPException(403, "raw cwd is not allowed")
        if s["type"] == "format_fix":
            if tier < FORMAT_FIX_MIN_TIER:
                raise HTTPException(
                    403,
                    f"format_fix requires tier >= {FORMAT_FIX_MIN_TIER}",
                )
            if not template.endswith("_fix"):
                raise HTTPException(
                    403,
                    "format_fix requires *_fix template",
                )
        else:
            if template.endswith("_fix"):
                raise HTTPException(
                    403,
                    "fix templates require format_fix step type",
                )
        workdir_id = str(s.get("workdir_id") or "").strip()
        if workdir_id:
            if not WORKDIR_RE.fullmatch(workdir_id):
                raise HTTPException(403, "invalid workdir_id")
            s["workdir"] = _resolve_workdir(
                run_id=req.run_id,
                workdir_id=workdir_id,
            )
    # Attach tier metadata for executor defense-in-depth.
    s["_rfsn_tier"] = int(tier)
    s["_rfsn_allow_network"] = False
    s["_rfsn_network_reason"] = ""
    if s["type"] == "ensure_deps":
        if tier < NETWORK_MIN_TIER:
            raise HTTPException(
                403,
                "ensure_deps requires network tier" f" >= {NETWORK_MIN_TIER}",
            )
        s["_rfsn_allow_network"] = True
        s["_rfsn_network_reason"] = "ensure_deps"

    if s["type"] == "apply_patch":
        patch_text = s.get("patch") or ""
        patch = patch_text.encode("utf-8", errors="replace")
        if len(patch) > MAX_PATCH_BYTES:
            raise HTTPException(413, "patch too large")

        # --- Deep patch content inspection (GATE IS FINAL AUTHORITY) ---

        # Block null bytes (binary injection)
        if "\x00" in patch_text:
            raise HTTPException(403, "patch contains null bytes")

        # Block shell metacharacters in patch file paths
        # (prevents injection if patch is ever mishandled downstream)
        _SHELL_META = re.compile(r"[;&|`$\\(){}\[\]!<>]")
        for line in patch_text.splitlines():
            if (
                line.startswith("diff --git ")
                or line.startswith("+++ ")
                or line.startswith("--- ")
            ):
                parts = line.split()
                last = parts[-1] if parts else ""
                if _SHELL_META.search(last):
                    raise HTTPException(
                        403,
                        "patch contains shell" f" metacharacters: {line[:80]}",
                    )

        # Block patches that try to modify sensitive files beyond dep manifests
        _SENSITIVE_PATTERNS = [
            r"\.env$",
            r"\.env\.",
            r"id_rsa",
            r"\.pem$",
            r"\.key$",
            r"\.p12$",
            r"\.pfx$",
            r"\.jks$",
            r"\.pypirc$",
            r"\.npmrc$",
            r"\.netrc$",
            r"Dockerfile",
            r"docker-compose",
        ]

        touched_paths = extract_patch_touched_paths(patch_text)
        for f in touched_paths:
            if not validate_repo_path(
                f,
                ALLOWED_PATHS,
                BLOCKED_GLOBS,
                repo_root_required=REPO_ROOT_REQUIRED,
                repo_root=repo_root,
                blocked_prefixes=_BLOCKED_READ_PREFIXES,
                blocked_suffixes=_BLOCKED_READ_SUFFIXES,
            ):
                raise HTTPException(
                    403,
                    f"patch path blocked: {f}",
                )
            # Check sensitive file patterns
            for sp in _SENSITIVE_PATTERNS:
                if re.search(sp, f):
                    raise HTTPException(
                        403,
                        f"patch touches sensitive file: {f}",
                    )

        # --- forbid flags (defense-in-depth mirror of kernel) ---
        _forbid_tests = bool(GATE_POLICY.get("forbid_test_edits", False))
        _forbid_ci = bool(GATE_POLICY.get("forbid_ci_edits", False))
        _forbid_deps = bool(GATE_POLICY.get("forbid_dep_manifest_edits", False))

        _DEP_MANIFEST_NAMES = {
            "pyproject.toml",
            "poetry.lock",
            "requirements.txt",
            "requirements-dev.txt",
            "requirements.in",
            "constraints.txt",
            "setup.py",
            "setup.cfg",
            "pipfile",
            "pipfile.lock",
            "package.json",
            "package-lock.json",
            "yarn.lock",
            "pnpm-lock.yaml",
            "go.mod",
            "go.sum",
            "cargo.toml",
            "cargo.lock",
        }

        for tp in touched_paths:
            pp = tp.replace("\\", "/").lstrip("/")
            if _forbid_tests and (
                pp.startswith("tests/")
                or "/tests/" in f"/{pp}"
                or pp.startswith("test/")
                or pp.endswith("_test.py")
                or pp.endswith("test.py")
            ):
                raise HTTPException(
                    403,
                    "forbid_test_edits: patch touches" f" test path: {tp}",
                )
            if _forbid_ci and (
                pp.startswith(".github/workflows/")
                or pp.startswith("ci/")
                or pp.startswith("scripts/")
            ):
                raise HTTPException(
                    403,
                    "forbid_ci_edits: patch touches" f" CI/scripts path: {tp}",
                )
            if _forbid_deps:
                base = pp.split("/")[-1].lower()
                if base in _DEP_MANIFEST_NAMES:
                    raise HTTPException(
                        403,
                        "forbid_dep_manifest_edits:"
                        f" patch touches dep manifest: {tp}",
                    )

        # --- diff-guard: max_changed_files ---
        if len(touched_paths) > _EFFECTIVE_MAX_FILES:
            raise HTTPException(
                403,
                "diff guard: too many files changed"
                f" ({len(touched_paths)} > {_EFFECTIVE_MAX_FILES})",
            )

        # --- diff-guard: max_added_lines / max_deleted_lines ---
        added_lines = 0
        deleted_lines = 0
        for line in patch_text.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                added_lines += 1
            elif line.startswith("-") and not line.startswith("---"):
                deleted_lines += 1
        if added_lines > _EFFECTIVE_MAX_ADDED:
            raise HTTPException(
                403,
                "diff guard: too many added lines"
                f" ({added_lines} > {_EFFECTIVE_MAX_ADDED})",
            )
        if deleted_lines > _EFFECTIVE_MAX_DELETED:
            raise HTTPException(
                403,
                "diff guard: too many deleted lines"
                f" ({deleted_lines} > {_EFFECTIVE_MAX_DELETED})",
            )
        total_changed = added_lines + deleted_lines
        if total_changed > _EFFECTIVE_MAX_TOTAL:
            raise HTTPException(
                403,
                "diff guard: too many total changed lines"
                f" ({total_changed} > {_EFFECTIVE_MAX_TOTAL})",
            )

        # ── RFSN Gate: FINAL AUTHORITY ──────────────────────
        # Call patch_risk_gate from gate.py for banned-pattern
        # enforcement (pytest.skip, xfail, test deletion heuristics,
        # CI/dep file blocking). This is THE central authority.
        if _HAS_PATCH_GATE and _patch_risk_gate is not None:
            gate_report = _patch_risk_gate(
                patch_text,
                MAX_PATCH_BYTES,
                _EFFECTIVE_MAX_FILES,
                5,  # max_new_files
            )
            s["_patch_gate_verdict"] = gate_report.decision.lower()
            s["_patch_gate_reason"] = (
                "; ".join(gate_report.reasons[:5]) if gate_report.reasons else ""
            )
            if gate_report.decision == "REJECT":
                raise HTTPException(
                    403,
                    "RFSN gate REJECT: " + "; ".join(gate_report.reasons[:5]),
                )
        else:
            s["_patch_gate_verdict"] = "na"
            s["_patch_gate_reason"] = "gate not available"

    # Enforce per-step budgets from kernel
    # gate policy (unified source of truth).
    budgets = GATE_POLICY.get(
        "step_budgets",
        {},
    )
    bt = budgets.get(s["type"])
    if bt and "timeout_s" in bt:
        max_t = int(bt["timeout_s"])
        cur = int(s.get("timeout_s") or max_t)
        s["timeout_s"] = min(cur, max_t)

    out = _executor(
        s,
        req.repo_id,
        req.iter,
        req.run_id,
        run_warm=bool(req.run_id and req.warm_sandbox),
    )

    # charge output bytes (stable cap) for reads/searches
    payload = out.get("payload")
    if isinstance(payload, str):
        b = len(payload.encode("utf-8", errors="replace"))
    elif payload is None:
        b = 0
    else:
        b = len(str(payload).encode("utf-8", errors="replace"))
    if s["type"] in (
        "repo_search",
        "repo_read_range",
        "read_file",
        "detect_project",
        "detect_workdirs",
    ):
        charge(
            req.repo_id,
            req.iter,
            "bytes",
            bytes_out=b,
            run_id=req.run_id,
        )

    if s["type"] == "detect_workdirs" and out.get("status", 1) == 0:
        _store_workdirs(req.run_id, out)

    return out
