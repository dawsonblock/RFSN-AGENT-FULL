import os
import re
from typing import Optional

from fastapi import FastAPI, HTTPException  # type: ignore[import-not-found]
from pydantic import BaseModel  # type: ignore[import-not-found]
import yaml  # type: ignore[import-untyped]
import requests  # type: ignore[import-untyped]

from policy import validate_repo_path

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

EXECUTOR_URL = os.getenv("EXECUTOR_URL", "http://executor:8003")


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


ALLOW = _load_yaml("/policies/tool_allowlist.yaml")
DIFF_GUARD = _load_yaml("/policies/diff_guard.yaml")

ALLOWED_TYPES = set(ALLOW.get("allowed_step_types", []))
ALLOWED_PATHS = ALLOW.get("allowed_paths", ["repo/**"])
BLOCKED_GLOBS = ALLOW.get("blocked_globs", [])

MAX_PATCH_BYTES = int(ALLOW.get("max_patch_bytes", 200000))
MAX_READ_BYTES = int(ALLOW.get("max_read_bytes", 200000))

MAX_READ_STEPS = int(ALLOW.get("max_read_steps_per_iter", 6))
MAX_SEARCH_STEPS = int(ALLOW.get("max_search_steps_per_iter", 4))
MAX_BYTES_PER_ITER = int(ALLOW.get("max_bytes_returned_per_iter", 250000))

# (repo_id, iter) -> usage
ITER_USAGE: dict[tuple[str, int], dict] = {}


def usage_key(repo_id: str, it: int):
    return (repo_id, it)


def charge(
    repo_id: str, it: int,
    kind: str, bytes_out: int = 0,
):
    k = usage_key(repo_id, it)
    u = ITER_USAGE.setdefault(
        k, {"reads": 0, "searches": 0, "bytes": 0}
    )
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

    # prune old iters per repo (keep last 5)
    keys = [kk for kk in ITER_USAGE.keys() if kk[0] == repo_id]
    iters = sorted({kk[1] for kk in keys})
    if len(iters) > 5:
        drop = set(iters[:-5])
        for kk in list(ITER_USAGE.keys()):
            if kk[0] == repo_id and kk[1] in drop:
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


class RunStepReq(BaseModel):
    repo_id: str
    iter: int
    step: Step


@app.get("/health")
def health():
    return {"ok": True}


def _executor(step: dict, repo_id: str, it: int):
    r = requests.post(
        f"{EXECUTOR_URL}/run",
        json={"repo_id": repo_id, "iter": it, "step": step},
        headers=auth_headers(),
        timeout=600,
    )
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    return r.json()


@app.post("/run_step")
def run_step(req: RunStepReq):
    s = req.step.model_dump()

    if s["type"] not in ALLOWED_TYPES:
        raise HTTPException(403, f"step type blocked: {s['type']}")
    if s["type"] == "repo_search":
        charge(req.repo_id, req.iter, "search")

    if s["type"] == "repo_read_range":
        p = s.get("path") or ""
        if not validate_repo_path(p, ALLOWED_PATHS, BLOCKED_GLOBS):
            raise HTTPException(403, "path blocked")
        charge(req.repo_id, req.iter, "read")

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
        _SHELL_META = re.compile(r'[;&|`$\\(){}\[\]!<>]')
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
                        "patch contains shell"
                        f" metacharacters: {line[:80]}",
                    )

        # Block patches that try to modify sensitive files beyond dep manifests
        _SENSITIVE_PATTERNS = [
            r'\.env$', r'\.env\.', r'id_rsa', r'\.pem$', r'\.key$',
            r'\.p12$', r'\.pfx$', r'\.jks$', r'\.pypirc$', r'\.npmrc$',
            r'\.netrc$', r'Dockerfile', r'docker-compose',
            r'\.github/workflows/', r'\.circleci/', r'Jenkinsfile',
        ]

        # --- diff-guard: blocked dependency files ---
        blocked = set(DIFF_GUARD.get("blocked_dependency_files", []))
        header_paths = set(re.findall(
            r"^[+]{3} b/(.+)$|^--- a/(.+)$",
            patch_text, flags=re.MULTILINE,
        ))
        flat = set()
        for a, b in header_paths:
            if a:
                flat.add(a.strip())
            if b:
                flat.add(b.strip())
        for f in flat:
            base = os.path.basename(f)
            if base in blocked:
                raise HTTPException(
                    403,
                    f"dependency manifest edit blocked: {base}",
                )
            # Check sensitive file patterns
            for sp in _SENSITIVE_PATTERNS:
                if re.search(sp, f):
                    raise HTTPException(
                        403,
                        f"patch touches sensitive file: {f}",
                    )

        # --- diff-guard: max_changed_files ---
        files_touched = set()
        for line in patch_text.splitlines():
            if line.startswith("diff --git "):
                parts = line.split()
                if len(parts) >= 4:
                    files_touched.add(parts[2].replace("a/", "", 1))
        max_files = int(DIFF_GUARD.get("max_changed_files", 0))
        if max_files and len(files_touched) > max_files:
            raise HTTPException(
                403,
                "diff guard: too many files changed"
                f" ({len(files_touched)} > {max_files})",
            )

        # --- diff-guard: max_added_lines / max_deleted_lines ---
        added_lines = 0
        deleted_lines = 0
        for line in patch_text.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                added_lines += 1
            elif line.startswith("-") and not line.startswith("---"):
                deleted_lines += 1
        max_added = int(DIFF_GUARD.get("max_added_lines", 0))
        max_deleted = int(DIFF_GUARD.get("max_deleted_lines", 0))
        if max_added and added_lines > max_added:
            raise HTTPException(
                403,
                "diff guard: too many added lines"
                f" ({added_lines} > {max_added})",
            )
        if max_deleted and deleted_lines > max_deleted:
            raise HTTPException(
                403,
                "diff guard: too many deleted lines"
                f" ({deleted_lines} > {max_deleted})",
            )

    out = _executor(s, req.repo_id, req.iter)

    # charge output bytes (stable cap) for reads/searches
    payload = out.get("payload")
    if isinstance(payload, str):
        b = len(payload.encode("utf-8", errors="replace"))
    elif payload is None:
        b = 0
    else:
        b = len(str(payload).encode("utf-8", errors="replace"))
    if s["type"] in ("repo_search", "repo_read_range"):
        charge(req.repo_id, req.iter, "bytes", bytes_out=b)

    return out
