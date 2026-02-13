import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from typing import Optional
from urllib.parse import urlparse

import yaml  # type: ignore[import-untyped]
from fastapi import FastAPI, HTTPException  # type: ignore[import-not-found]
from pydantic import BaseModel  # type: ignore[import-not-found]

# --- Auth middleware: only tool_gateway (with valid token) can reach us ---
import sys

sys.path.insert(0, "/shared")
try:
    from auth import ServiceAuthMiddleware  # type: ignore[import-not-found]

    _HAS_AUTH = True
except ImportError:
    _HAS_AUTH = False

# Defense-in-depth: import patch risk gate for apply_patch.
try:
    from rfsn_swebench.gate import patch_risk_gate as _patch_risk_gate

    _HAS_PATCH_GATE = True
except ImportError:
    _HAS_PATCH_GATE = False
    _patch_risk_gate = None  # type: ignore[assignment]

app = FastAPI()
if _HAS_AUTH:
    app.add_middleware(ServiceAuthMiddleware)  # type: ignore[possibly-unbound]

BLESSED_IMAGE = os.getenv(
    "BLESSED_IMAGE",
    "rfsn-blessed@sha256:208a2c2dac42ed9b3ca023b30cd815518070930274592844511aa34de21b6360",
)
HOST_DATA_DIR = os.getenv("HOST_DATA_DIR", "/data")
USE_DOCKER_SANDBOX = (
    os.getenv(
        "RFSN_EXEC_USE_DOCKER",
        "1",
    )
    == "1"
)
DEV_MODE = os.getenv("RFSN_DEV_MODE", "0") == "1"
STRICT_IMAGE_DIGEST = (
    os.getenv(
        "RFSN_STRICT_IMAGE_DIGEST",
        "1",
    )
    == "1"
)
ALLOW_LOCAL_EXEC = (
    os.getenv(
        "RFSN_ALLOW_LOCAL_EXEC",
        "0",
    )
    == "1"
)


def _local_exec_allowed() -> bool:
    return DEV_MODE and ALLOW_LOCAL_EXEC


def _is_digest_image_ref(ref: str) -> bool:
    return "@sha256:" in str(ref or "").strip()


def _docker_runtime_available() -> bool:
    """Best-effort runtime availability check for strict sandbox mode."""
    try:
        p = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
            check=False,
        )
    except Exception:
        return False
    return p.returncode == 0 and bool((p.stdout or "").strip())


DOCKER_RUNTIME_AVAILABLE = _docker_runtime_available() if USE_DOCKER_SANDBOX else False
if STRICT_IMAGE_DIGEST and not DEV_MODE and not _is_digest_image_ref(BLESSED_IMAGE):
    raise SystemExit(
        "BLESSED_IMAGE must be digest-pinned (@sha256:...)"
        " when RFSN_STRICT_IMAGE_DIGEST=1",
    )
if USE_DOCKER_SANDBOX and not DOCKER_RUNTIME_AVAILABLE:
    if _local_exec_allowed():
        print(
            "WARN: Docker runtime unavailable; "
            "falling back to dev-only local execution",
            flush=True,
        )
        USE_DOCKER_SANDBOX = False
    else:
        raise SystemExit(
            "RFSN_EXEC_USE_DOCKER=1 but docker runtime is unavailable. "
            "Use a sandbox runtime, or set RFSN_DEV_MODE=1 and "
            "RFSN_ALLOW_LOCAL_EXEC=1 for dev-only local mode.",
        )

if not USE_DOCKER_SANDBOX and not _local_exec_allowed():
    raise SystemExit(
        "RFSN_EXEC_USE_DOCKER=0 is disabled unless "
        "RFSN_DEV_MODE=1 and RFSN_ALLOW_LOCAL_EXEC=1",
    )

# ── Warm sandbox pool ─────────────────────────
try:
    from sandbox_pool import SandboxPool  # type: ignore[import-not-found]

    _sandbox_pool: SandboxPool | None = SandboxPool()
except Exception as _pool_err:
    print(
        f"WARN: sandbox pool disabled: {_pool_err}",
        flush=True,
    )
    _sandbox_pool = None  # type: ignore[assignment]


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


DEPS = _load_yaml("/policies/deps_policy.yaml")
GATE_POLICY = _load_yaml("/policies/gate_policy.yaml")
CMD_TEMPLATES = _load_yaml("/policies/command_templates.yaml").get("templates", {})
TOOL_ALLOWLIST = _load_yaml("/policies/tool_allowlist.yaml")
SAFE_CMD_TEMPLATES = TOOL_ALLOWLIST.get("command_templates", {}) or {}
MAX_READ_FILE_BYTES = int(
    TOOL_ALLOWLIST.get("max_read_file_bytes", 65536),
)
MAX_WORKDIRS = int(
    TOOL_ALLOWLIST.get("max_workdirs", 24),
)
NETWORK_MIN_TIER = int(
    os.getenv(
        "RFSN_NETWORK_MIN_TIER",
        str(GATE_POLICY.get("network_min_tier", 2)),
    )
)
MAX_STEP_LOG_BYTES = int(
    os.getenv(
        "RFSN_MAX_STEP_LOG_BYTES",
        str(GATE_POLICY.get("max_step_log_bytes", 200000)),
    )
)
MAX_ARTIFACT_DIR_BYTES = int(
    os.getenv(
        "RFSN_MAX_ARTIFACT_DIR_BYTES",
        str(GATE_POLICY.get("max_artifact_dir_bytes", 1000000000)),
    )
)
MAX_ARTIFACT_DELTA_BYTES = int(
    os.getenv(
        "RFSN_MAX_ARTIFACT_DELTA_BYTES",
        str(GATE_POLICY.get("max_artifact_delta_bytes", 200000000)),
    )
)

_SAFE_REPO_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_SAFE_REF = re.compile(r"^[A-Za-z0-9._/-]{1,128}$")


class ExecReq(BaseModel):
    repo_id: str
    iter: int
    step: dict


class RepoImportReq(BaseModel):
    repo_url: str
    repo_id: Optional[str] = None
    ref: Optional[str] = None
    depth: int = 1
    force: bool = False


def _classify_failure(logs: str, status: int) -> str | None:
    low = (logs or "").lower()
    if int(status) == 0:
        return None
    if "failed" in low and ("pytest" in low or "unittest" in low or "tests" in low):
        return "tests_failed"
    if "modulenotfounderror" in low or "no module named" in low:
        return "import_error_missing_module"
    if "missing venv; ensure_deps first" in low:
        return "deps_install_failed"
    if "resolutionimpossible" in low or "could not find a version" in low:
        return "deps_install_failed"
    if ".github/workflows" in low or ("workflow" in low and "yaml" in low):
        return "ci_config_invalid"
    if "ci" in low and ("failed" in low or "error" in low):
        return "ci_failed"
    return None


def _normalize_workdir(rel: str) -> str:
    workdir = (rel or ".").strip()
    if not workdir:
        return "."
    if workdir.startswith("/") or workdir.startswith("~"):
        raise HTTPException(403, "invalid workdir")
    if ".." in workdir.split("/"):
        raise HTTPException(403, "invalid workdir")
    return workdir


def _coerce_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _truncate_text_bytes(text: str, max_bytes: int) -> tuple[str, bool]:
    raw = (text or "").encode("utf-8", errors="replace")
    if max_bytes <= 0 or len(raw) <= max_bytes:
        return text or "", False
    truncated = raw[:max_bytes].decode("utf-8", errors="ignore")
    return truncated, True


def _dir_size_bytes(path: str, max_files: int = 200000) -> int:
    total = 0
    seen = 0
    if not os.path.isdir(path):
        return 0
    for root, _, files in os.walk(path):
        for name in files:
            seen += 1
            if seen > max_files:
                return total
            fp = os.path.join(root, name)
            try:
                total += os.path.getsize(fp)
            except OSError:
                continue
    return total


def _apply_artifact_quota(
    out: dict,
    *,
    before_size: int,
    after_size: int,
) -> None:
    delta = max(0, after_size - before_size)
    out["artifact_bytes_before"] = int(before_size)
    out["artifact_bytes_after"] = int(after_size)
    out["artifact_bytes_delta"] = int(delta)
    if after_size > MAX_ARTIFACT_DIR_BYTES:
        out["status"] = 1
        out["failure_kind"] = "artifact_quota_exceeded"
        out["logs"] = str(out.get("logs") or "") + (
            "\n[ARTIFACT_QUOTA] artifact dir exceeds max"
            f" ({after_size} > {MAX_ARTIFACT_DIR_BYTES})"
        )
        return
    if delta > MAX_ARTIFACT_DELTA_BYTES:
        out["status"] = 1
        out["failure_kind"] = "artifact_quota_exceeded"
        out["logs"] = str(out.get("logs") or "") + (
            "\n[ARTIFACT_QUOTA] step artifact growth exceeds max"
            f" ({delta} > {MAX_ARTIFACT_DELTA_BYTES})"
        )


def _require_network_tier(step: dict) -> None:
    tier = _coerce_int(step.get("_rfsn_tier"), 0)
    allow_network = bool(step.get("_rfsn_allow_network"))
    if not allow_network or tier < NETWORK_MIN_TIER:
        raise HTTPException(
            403,
            "networked ensure_deps requires tier" f" >= {NETWORK_MIN_TIER}",
        )


def _resolve_safe_template(template: str) -> list[str]:
    argv = SAFE_CMD_TEMPLATES.get(template)
    if not isinstance(argv, list) or not argv:
        raise HTTPException(
            403,
            f"unknown safe command template: {template}",
        )
    out: list[str] = []
    for item in argv:
        if not isinstance(item, str) or not item.strip():
            raise HTTPException(
                403,
                f"invalid template element in {template}",
            )
        out.append(item)
    return out


def _result(
    *,
    out: dict,
    payload=None,
    command: list[str] | None = None,
    workdir: str | None = None,
    network_mode: str = "none",
    allow_network: bool = False,
    tier: int = 0,
    network_reason: str = "",
):
    status_raw = out.get("status", 1)
    if status_raw is None:
        status = 1
    else:
        status = int(status_raw)
    logs, trunc = _truncate_text_bytes(
        str(out.get("logs", "") or ""),
        MAX_STEP_LOG_BYTES,
    )
    explicit_failure_kind = out.get("failure_kind")
    if isinstance(explicit_failure_kind, str) and explicit_failure_kind:
        failure_kind = explicit_failure_kind
    else:
        failure_kind = _classify_failure(logs, status)
    return {
        "status": status,
        "seconds": float(out.get("seconds", 0.0) or 0.0),
        "logs": logs,
        "logs_truncated": bool(out.get("logs_truncated", False) or trunc),
        "payload": payload,
        "failure_kind": failure_kind,
        "command": command,
        "workdir": workdir or ".",
        "network_mode": network_mode,
        "allow_network": bool(allow_network),
        "tier": int(tier),
        "network_reason": network_reason or "",
        "artifact_bytes_before": int(out.get("artifact_bytes_before", 0) or 0),
        "artifact_bytes_after": int(out.get("artifact_bytes_after", 0) or 0),
        "artifact_bytes_delta": int(out.get("artifact_bytes_delta", 0) or 0),
    }


@app.get("/health")
def health():
    pool_stats = _sandbox_pool.stats() if _sandbox_pool else {"active": 0}
    return {
        "ok": True,
        "image": BLESSED_IMAGE,
        "strict_image_digest": STRICT_IMAGE_DIGEST,
        "mode": "docker" if USE_DOCKER_SANDBOX else "local",
        "docker_runtime_available": DOCKER_RUNTIME_AVAILABLE,
        "local_exec_allowed": _local_exec_allowed(),
        "sandbox_pool": pool_stats,
    }


def _safe_cmd_output(cmd: list[str], timeout: int = 5) -> str:
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
        return (p.stdout or "").strip()
    except Exception:
        return ""


def _os_release() -> dict:
    out: dict[str, str] = {}
    path = "/etc/os-release"
    if not os.path.exists(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k] = v.strip().strip('"')
    except Exception:
        return {}
    return out


@app.get("/env_manifest")
def env_manifest(run_id: str, repo_id: str):
    _validate_repo_id(repo_id)
    safe_run = re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)[:128]
    replay_dir = os.path.abspath(
        f"/data/artifacts/{repo_id}/replay/{safe_run}",
    )
    os.makedirs(replay_dir, exist_ok=True)
    out_path = os.path.join(replay_dir, "env.json")

    manifest = {
        "run_id": run_id,
        "repo_id": repo_id,
        "blessed_image": BLESSED_IMAGE,
        "strict_image_digest": bool(STRICT_IMAGE_DIGEST),
        "mode": "docker" if USE_DOCKER_SANDBOX else "local",
        "docker_runtime_available": bool(DOCKER_RUNTIME_AVAILABLE),
        "python_version": _safe_cmd_output(["python", "--version"]),
        "uname": _safe_cmd_output(["uname", "-a"]),
        "os_release": _os_release(),
        "lang": os.getenv("LANG", ""),
        "lc_all": os.getenv("LC_ALL", ""),
        "tz": os.getenv("TZ", ""),
        "captured_at": time.time(),
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    return {"ok": True, "path": out_path, "manifest": manifest}


def _normalize_repo_url(repo_url: str) -> str:
    raw = (repo_url or "").strip()
    if not raw:
        raise HTTPException(400, "repo_url is required")

    # Convenience shorthand: owner/repo -> GitHub HTTPS.
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", raw):
        raw = f"https://github.com/{raw}.git"

    u = urlparse(raw)
    if u.scheme != "https":
        raise HTTPException(400, "repo_url must use https://")
    if not u.netloc:
        raise HTTPException(400, "repo_url missing host")
    if u.username or u.password:
        raise HTTPException(
            400,
            "repo_url must not include credentials",
        )
    if not u.path or u.path in ("/", ""):
        raise HTTPException(400, "repo_url missing repository path")
    if u.query or u.fragment:
        raise HTTPException(
            400,
            "repo_url must not include query or fragment",
        )
    return raw


def _derive_repo_id(repo_url: str) -> str:
    u = urlparse(repo_url)
    parts = [p for p in u.path.split("/") if p]
    if not parts:
        raise HTTPException(400, "unable to derive repo_id")

    leaf = parts[-1]
    if leaf.endswith(".git"):
        leaf = leaf[:-4]
    if len(parts) >= 2:
        owner = parts[-2]
        raw = f"{owner}-{leaf}"
    else:
        raw = leaf

    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "-", raw).strip("-")
    if not cleaned:
        raise HTTPException(400, "unable to derive safe repo_id")
    if len(cleaned) > 128:
        cleaned = cleaned[:128]
    return cleaned


def _repo_root() -> str:
    root = os.path.abspath("/data/repos")
    os.makedirs(root, exist_ok=True)
    return root


def _git_output(repo_path: str, args: list[str]) -> str:
    try:
        p = subprocess.run(
            ["git", *args],
            cwd=repo_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        )
    except Exception:
        return ""
    if p.returncode != 0:
        return ""
    return (p.stdout or "").strip()


@app.get("/repos")
def list_repos():
    root = _repo_root()
    repos = []
    for repo_id in sorted(os.listdir(root)):
        path = os.path.join(root, repo_id)
        if not os.path.isdir(path):
            continue
        if not _SAFE_REPO_ID.match(repo_id):
            continue

        has_git = os.path.isdir(os.path.join(path, ".git")) or os.path.isfile(
            os.path.join(path, ".git")
        )
        repos.append(
            {
                "repo_id": repo_id,
                "path": path,
                "has_git": has_git,
                "updated_at": int(os.path.getmtime(path)),
                "origin": (
                    _git_output(path, ["config", "--get", "remote.origin.url"])
                    if has_git
                    else ""
                ),
                "head": (
                    _git_output(path, ["rev-parse", "--short", "HEAD"])
                    if has_git
                    else ""
                ),
                "branch": (
                    _git_output(path, ["rev-parse", "--abbrev-ref", "HEAD"])
                    if has_git
                    else ""
                ),
            }
        )
    return {"count": len(repos), "repos": repos}


@app.post("/repo/import")
def repo_import(req: RepoImportReq):
    repo_url = _normalize_repo_url(req.repo_url)
    repo_id = (req.repo_id or _derive_repo_id(repo_url)).strip()
    _validate_repo_id(repo_id)

    root = _repo_root()
    repo_local = os.path.abspath(os.path.join(root, repo_id))
    if not repo_local.startswith(root + os.sep):
        raise HTTPException(400, "repo path traversal detected")

    if os.path.exists(repo_local):
        if not req.force:
            raise HTTPException(
                409,
                f"repo already exists: {repo_id}",
            )
        shutil.rmtree(repo_local, ignore_errors=True)

    ref = (req.ref or "").strip()
    if ref and not _SAFE_REF.fullmatch(ref):
        raise HTTPException(400, "invalid ref format")

    depth = int(req.depth or 1)
    depth = min(max(depth, 1), 200)

    args = [
        "git",
        "clone",
        "--filter=blob:none",
        "--depth",
        str(depth),
    ]
    if ref:
        args.extend(
            [
                "--branch",
                ref,
                "--single-branch",
            ]
        )
    args.extend([repo_url, repo_local])

    start = time.time()
    try:
        p = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=600,
        )
    except FileNotFoundError as exc:
        raise HTTPException(500, "git is not available") from exc
    except subprocess.TimeoutExpired as exc:
        raw = exc.stdout or ""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        shutil.rmtree(repo_local, ignore_errors=True)
        raise HTTPException(
            504,
            f"git clone timeout for {repo_url}: {raw[-4000:]}",
        ) from exc

    logs = (p.stdout or "").replace("\r\n", "\n")[-20000:]
    if p.returncode != 0:
        shutil.rmtree(repo_local, ignore_errors=True)
        raise HTTPException(
            400,
            f"git clone failed: {logs[-4000:]}",
        )

    origin = _git_output(
        repo_local,
        ["config", "--get", "remote.origin.url"],
    )
    branch = _git_output(
        repo_local,
        ["rev-parse", "--abbrev-ref", "HEAD"],
    )
    head = _git_output(
        repo_local,
        ["rev-parse", "--short", "HEAD"],
    )

    return {
        "ok": True,
        "repo_id": repo_id,
        "repo_url": origin or repo_url,
        "branch": branch,
        "head": head,
        "path": repo_local,
        "seconds": round(time.time() - start, 3),
        "logs": logs[-4000:],
    }


# ── Warm sandbox lifecycle endpoints ─────────


class SandboxReq(BaseModel):
    run_id: str
    repo_id: str
    network: str = "none"


@app.post("/sandbox/create")
def sandbox_create(req: SandboxReq):
    """Create or reuse a warm sandbox for a run."""
    if not _sandbox_pool:
        raise HTTPException(
            501,
            "sandbox pool not available",
        )
    _validate_repo_id(req.repo_id)
    repo_host, art_host, venv_host, wheels_host = _paths(req.repo_id)
    sb = _sandbox_pool.get_or_create(
        req.run_id,
        repo_host,
        art_host,
        venv_host,
        wheels_host,
        req.network,
    )
    return {
        "ok": True,
        "container_id": sb.container_id[:12],
        "image_hash": sb.image_hash,
    }


@app.post("/sandbox/destroy")
def sandbox_destroy(req: SandboxReq):
    """Destroy a warm sandbox after a run ends."""
    if not _sandbox_pool:
        return {"ok": True, "image_hash": None}
    img_hash = _sandbox_pool.destroy_run(
        req.run_id,
    )
    return {
        "ok": True,
        "image_hash": img_hash,
    }


class WarmExecReq(BaseModel):
    run_id: str
    repo_id: str
    step: dict


@app.post("/run_warm")
def run_warm(req: WarmExecReq):
    """Execute a step in a warm sandbox.

    Falls back to cold (ephemeral) execution if
    no sandbox exists for this run_id.
    """
    if not _sandbox_pool:
        # Fall back to cold execution.
        return run(
            ExecReq(
                repo_id=req.repo_id,
                iter=0,
                step=req.step,
            )
        )

    _validate_repo_id(req.repo_id)
    repo_host, art_host, venv_host, wheels_host = _paths(req.repo_id)

    step = req.step
    t: str = step.get("type") or ""
    timeout_s = int(step.get("timeout_s") or 300)
    if t == "ensure_deps":
        # Network-enabled dependency install is
        # explicitly tier-gated and always routed to
        # cold execution so networking policy is
        # applied in one place.
        _require_network_tier(step)
        return run(
            ExecReq(
                repo_id=req.repo_id,
                iter=0,
                step=req.step,
            )
        )

    sb = _sandbox_pool.get_or_create(
        req.run_id,
        repo_host,
        art_host,
        venv_host,
        wheels_host,
    )

    # Build script + data files for this step
    # (reuse the same logic as cold path).
    script, data_files = _build_step_script(
        t,
        step,
        req.repo_id,
    )
    artifact_before = _dir_size_bytes(art_host)

    out = _sandbox_pool.exec_in(
        sb,
        script,
        data_files,
        timeout_s,
    )
    artifact_after = _dir_size_bytes(art_host)
    _apply_artifact_quota(
        out,
        before_size=artifact_before,
        after_size=artifact_after,
    )

    # For apply_patch, add verification.
    if t == "apply_patch":
        out = _verify_patch_result(
            out,
            step,
            sb,
        )

    payload = None
    if t in (
        "repo_search",
        "repo_read_range",
        "read_file",
        "detect_project",
        "detect_workdirs",
    ):
        payload = out.get("logs", "").strip()
    command = None
    workdir = str(req.step.get("workdir") or ".")
    allow_network = bool(req.step.get("_rfsn_allow_network"))
    tier = _coerce_int(req.step.get("_rfsn_tier"), 0)
    network_reason = str(req.step.get("_rfsn_network_reason") or "")
    network_mode = str(out.get("network_mode") or "none")
    if t in ("run_cmd_template", "format_fix"):
        tmpl = str(req.step.get("template") or "")
        try:
            command = _resolve_safe_template(tmpl)
        except HTTPException:
            command = None
    return _result(
        out=out,
        payload=payload,
        command=command,
        workdir=workdir,
        network_mode=network_mode,
        allow_network=allow_network,
        tier=tier,
        network_reason=network_reason,
    )


def _validate_repo_id(repo_id: str) -> None:
    if not _SAFE_REPO_ID.match(repo_id):
        raise HTTPException(
            400,
            "invalid repo_id: must match" f" {_SAFE_REPO_ID.pattern}",
        )
    if ".." in repo_id:
        raise HTTPException(400, "repo_id must not contain '..'")


def _paths(repo_id: str):
    _validate_repo_id(repo_id)
    repo_local = os.path.abspath(f"/data/repos/{repo_id}")
    art_local = os.path.abspath(f"/data/artifacts/{repo_id}")
    venv_local = os.path.abspath(f"/data/venv/{repo_id}")
    wheels_local = os.path.abspath(f"/data/wheels/{repo_id}")
    for p in (repo_local, art_local, venv_local, wheels_local):
        if not p.startswith("/data/"):
            raise HTTPException(400, "path traversal detected")
    os.makedirs(art_local, exist_ok=True)
    os.makedirs(venv_local, exist_ok=True)
    os.makedirs(wheels_local, exist_ok=True)
    if not os.path.isdir(repo_local):
        raise HTTPException(404, f"repo not found at /data/repos/{repo_id}")
    if USE_DOCKER_SANDBOX:
        # Nested docker mode mounts host paths into blessed containers.
        repo_exec = os.path.join(HOST_DATA_DIR, "repos", repo_id)
        art_exec = os.path.join(HOST_DATA_DIR, "artifacts", repo_id)
        venv_exec = os.path.join(HOST_DATA_DIR, "venv", repo_id)
        wheels_exec = os.path.join(HOST_DATA_DIR, "wheels", repo_id)
    else:
        # Local mode runs inside this container and must use container paths.
        repo_exec = repo_local
        art_exec = art_local
        venv_exec = venv_local
        wheels_exec = wheels_local
    return repo_exec, art_exec, venv_exec, wheels_exec


def _write_data_file(data: str, suffix: str = ".txt") -> str:
    """Write data to a temp file for mounting into container (no heredoc)."""
    fd = tempfile.NamedTemporaryFile(
        delete=False,
        suffix=suffix,
        mode="w",
        encoding="utf-8",
        dir="/tmp",
    )
    fd.write(data)
    fd.close()
    return fd.name


def _run_docker_with_data(
    script: str,
    data_files: dict,
    repo_host,
    art_host,
    venv_host,
    wheels_host,
    timeout_s: int,
    network_disabled: bool,
):
    """Run script in blessed container.

    Data passed via mounted files — NEVER heredocs.

    Security features:
    - --user 1000:1000 (non-root)
    - --security-opt no-new-privileges:true
    - --memory 2g / --cpus 2 / --pids-limit 256
    - --cap-drop ALL
    """
    if not USE_DOCKER_SANDBOX:
        if _local_exec_allowed():
            return _run_local_with_data(
                script,
                data_files,
                repo_host,
                art_host,
                venv_host,
                wheels_host,
                timeout_s,
            )
        raise HTTPException(
            503,
            "docker sandbox is required"
            " (set RFSN_DEV_MODE=1 and"
            " RFSN_ALLOW_LOCAL_EXEC=1 for dev-only local mode)",
        )

    script_path = _write_data_file(script, suffix=".sh")
    try:
        net = "none" if network_disabled else "bridge"
        extra_mounts = ["-v", f"{script_path}:/tmp/rfsn_script.sh:ro"]
        for cpath, hpath in data_files.items():
            extra_mounts.extend(["-v", f"{hpath}:{cpath}:ro"])

        args = (
            [
                "docker",
                "run",
                "--rm",
                "--network",
                net,
                "--user",
                "1000:1000",
                "--security-opt",
                "no-new-privileges:true",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=256m",
                "--memory",
                "2g",
                "--cpus",
                "2",
                "--pids-limit",
                "256",
                "--cap-drop",
                "ALL",
                "-e",
                "HOME=/tmp",
            ]
            + extra_mounts
            + [
                "-v",
                f"{repo_host}:/work/repo:rw",
                "-v",
                f"{art_host}:/work/artifacts:rw",
                "-v",
                f"{venv_host}:/work/venv:rw",
                "-v",
                f"{wheels_host}:/work/wheels:rw",
                "-w",
                "/work",
                BLESSED_IMAGE,
                "bash",
                "/tmp/rfsn_script.sh",
            ]
        )

        start = time.time()
        try:
            p = subprocess.run(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout_s,
                text=True,
            )
            raw_logs = (p.stdout or "").replace("\r\n", "\n")
            out, truncated = _truncate_text_bytes(
                raw_logs,
                MAX_STEP_LOG_BYTES,
            )
            return {
                "status": p.returncode,
                "seconds": time.time() - start,
                "logs": out,
                "logs_truncated": truncated,
                "network_mode": net,
            }
        except subprocess.TimeoutExpired as e:
            raw = e.stdout or ""
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            out, truncated = _truncate_text_bytes(
                str(raw) + "\n[TIMEOUT]\n",
                MAX_STEP_LOG_BYTES,
            )
            return {
                "status": 124,
                "seconds": time.time() - start,
                "logs": out.replace("\r\n", "\n"),
                "logs_truncated": truncated,
                "network_mode": net,
            }
        except FileNotFoundError:
            if _local_exec_allowed():
                return _run_local_with_data(
                    script,
                    data_files,
                    repo_host,
                    art_host,
                    venv_host,
                    wheels_host,
                    timeout_s,
                )
            raise HTTPException(
                503,
                "docker binary unavailable and local fallback disabled",
            )
        except Exception as e:
            raise HTTPException(500, f"executor error: {type(e).__name__}")
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass
        for hpath in data_files.values():
            try:
                os.unlink(hpath)
            except OSError:
                pass


def _run_local_with_data(
    script: str,
    data_files: dict,
    repo_host: str,
    art_host: str,
    venv_host: str,
    wheels_host: str,
    timeout_s: int,
):
    """Execute script directly in executor container.

    Used when docker socket is intentionally unavailable.
    """
    start = time.time()
    translated = script
    replacements = {
        "/work/repo": repo_host,
        "/work/artifacts": art_host,
        "/work/venv": venv_host,
        "/work/wheels": wheels_host,
    }
    for src, dst in replacements.items():
        translated = translated.replace(
            src,
            shlex.quote(dst),
        )
    translated = translated.replace(
        "cd repo",
        f"cd {shlex.quote(repo_host)}",
    )

    setup_lines: list[str] = []
    for cpath, hpath in data_files.items():
        cdir = os.path.dirname(cpath) or "/tmp"
        setup_lines.append(f"mkdir -p {shlex.quote(cdir)}")
        setup_lines.append("cp " f"{shlex.quote(hpath)} " f"{shlex.quote(cpath)}")
    if setup_lines:
        translated = (
            "# local-mode data mounts\n" + "\n".join(setup_lines) + "\n" + translated
        )

    try:
        p = subprocess.run(
            ["bash", "-lc", translated],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_s,
            text=True,
        )
        raw_logs = (p.stdout or "").replace("\r\n", "\n")
        out, truncated = _truncate_text_bytes(
            raw_logs,
            MAX_STEP_LOG_BYTES,
        )
        return {
            "status": p.returncode,
            "seconds": time.time() - start,
            "logs": out,
            "logs_truncated": truncated,
            "network_mode": "local",
        }
    except subprocess.TimeoutExpired as e:
        raw = e.stdout or ""
        if isinstance(raw, bytes):
            raw = raw.decode(
                "utf-8",
                errors="replace",
            )
        out, truncated = _truncate_text_bytes(
            str(raw) + "\n[TIMEOUT]\n",
            MAX_STEP_LOG_BYTES,
        )
        return {
            "status": 124,
            "seconds": time.time() - start,
            "logs": out.replace("\r\n", "\n"),
            "logs_truncated": truncated,
            "network_mode": "local",
        }


def _ensure_deps(
    repo_id,
    repo_host,
    art_host,
    venv_host,
    wheels_host,
    timeout_s,
):
    manifest = DEPS.get("manifest", "requirements.txt")
    require_hashes = bool(DEPS.get("require_hashes", True))
    only_binary = bool(DEPS.get("only_binary", True))
    cache_dir = DEPS.get("pip_cache_dir", "/work/wheels")
    if "/" in manifest or "\\" in manifest or ".." in manifest:
        raise HTTPException(400, f"invalid manifest name: {manifest}")
    rh = 1 if require_hashes else 0
    ob = 1 if only_binary else 0
    script = f"""#!/bin/bash
set -euo pipefail
cd /work/repo
MFST={shlex.quote(manifest)}
if [ ! -f $MFST ]; then
  echo "Missing manifest: {manifest}"; exit 31
fi
if [ {rh} -eq 1 ]; then
  if ! grep -q -- "--hash=" {shlex.quote(manifest)}; then
    echo "Policy: requirements.txt must include --hash entries"; exit 33
  fi
fi
python -m venv /work/venv
. /work/venv/bin/activate
python -m pip install --upgrade pip
BIN_FLAG=""
if [ {ob} -eq 1 ]; then BIN_FLAG="--only-binary=:all:"; fi
python -m pip install --require-hashes $BIN_FLAG \\
  --cache-dir {shlex.quote(cache_dir)} \\
  -r {shlex.quote(manifest)}
python -c "import sys; print('deps_ok', sys.version)"
"""
    return _run_docker_with_data(
        script,
        {},
        repo_host,
        art_host,
        venv_host,
        wheels_host,
        timeout_s,
        network_disabled=False,
    )


def _repo_search(
    pattern,
    repo_host,
    art_host,
    venv_host,
    wheels_host,
    timeout_s,
):
    """SAFE: pattern written to mounted file, never in bash."""
    if len(pattern) > 500:
        raise HTTPException(400, "search pattern too long (max 500 chars)")
    pattern_file = _write_data_file(pattern, suffix=".pattern")
    search_py = (
        "import os, re, pathlib, json, sys\n"
        "try:\n"
        "    pat = open('/tmp/rfsn_data/pattern.txt', 'r').read().strip()\n"
        "except Exception:\n"
        "    print('[]'); sys.exit(0)\n"
        "try:\n"
        "    rx = re.compile(pat, re.MULTILINE)\n"
        "except re.error as e:\n"
        '    print(json.dumps({"error":'
        ' f"invalid regex: {e}"}))'
        "; sys.exit(1)\n"
        "root = pathlib.Path('.')\n"
        "EXTS = {'.py', '.js', '.ts', '.jsx', '.tsx',\n"
        "        '.java', '.rs', '.go', '.rb', '.c',\n"
        "        '.cpp', '.h', '.hpp', '.cs', '.swift',\n"
        "        '.kt', '.scala', '.toml', '.yaml',\n"
        "        '.yml', '.json', '.cfg', '.ini',\n"
        "        '.md', '.rst', '.txt', '.sh'}\n"
        "SKIP = {'.git', '__pycache__', 'node_modules',\n"
        "        '.tox', '.mypy_cache', '.eggs',\n"
        "        'dist', 'build', '.venv', 'venv'}\n"
        "# Config pre-index: always scan root configs\n"
        "CONFIG_NAMES = {'setup.py', 'setup.cfg',\n"
        "    'pyproject.toml', 'Makefile', 'Dockerfile',\n"
        "    'tox.ini', 'pytest.ini', '.flake8',\n"
        "    'Cargo.toml', 'go.mod', 'package.json',\n"
        "    'tsconfig.json', 'Gemfile', 'pom.xml',\n"
        "    'CMakeLists.txt', 'Pipfile',\n"
        "    'requirements.txt', 'requirements.in',\n"
        "    'MANIFEST.in', 'conftest.py'}\n"
        "out = []\n"
        "# Pre-index: check root configs first\n"
        "for cn in sorted(CONFIG_NAMES):\n"
        "    cp = root / cn\n"
        "    if not cp.is_file():\n"
        "        continue\n"
        "    try:\n"
        "        txt = cp.read_text(encoding='utf-8', errors='replace')\n"
        "    except Exception:\n"
        "        continue\n"
        "    if rx.search(txt):\n"
        "        out.append(str(cp))\n"
        "for p in root.rglob('*'):\n"
        "    if any(s in p.parts for s in SKIP):\n"
        "        continue\n"
        "    if not p.is_file():\n"
        "        continue\n"
        "    if p.suffix not in EXTS:\n"
        "        continue\n"
        "    if str(p) in out:\n"
        "        continue\n"
        "    try:\n"
        "        txt = p.read_text(encoding='utf-8', errors='replace')\n"
        "    except Exception:\n"
        "        continue\n"
        "    if rx.search(txt):\n"
        "        out.append(str(p))\n"
        "    if len(out) >= 200:\n"
        "        break\n"
        "print(json.dumps(out[:200]))\n"
    )
    search_py_file = _write_data_file(search_py, suffix=".py")
    script = (
        "#!/bin/bash\nset -euo pipefail\n"
        "cd /work/repo\n"
        "python3 /tmp/rfsn_data/search.py\n"
    )
    return _run_docker_with_data(
        script,
        {
            "/tmp/rfsn_data/pattern.txt": pattern_file,
            "/tmp/rfsn_data/search.py": search_py_file,
        },
        repo_host,
        art_host,
        venv_host,
        wheels_host,
        timeout_s,
        network_disabled=True,
    )


def _repo_read_range(
    path,
    line_start,
    line_end,
    repo_host,
    art_host,
    venv_host,
    wheels_host,
    timeout_s,
):
    """SAFE: path & lines passed via mounted JSON, never in bash."""
    if path.startswith("/") or path.startswith("~") or ".." in path.split("/"):
        raise HTTPException(403, f"path rejected: {path}")
    config = json.dumps(
        {
            "path": path,
            "start": line_start,
            "end": line_end,
        }
    )
    config_file = _write_data_file(config, suffix=".json")
    read_py = (
        "import pathlib, sys, json\n"
        "cfg = json.loads(open('/tmp/rfsn_data/config.json').read())\n"
        "p = pathlib.Path(cfg['path'])\n"
        "if not p.exists() or not p.is_file():\n"
        "    print('', end=''); sys.exit(0)\n"
        "rp = p.resolve()\n"
        "if not str(rp).startswith(str(pathlib.Path('.').resolve())):\n"
        "    print('path traversal blocked', file=sys.stderr); sys.exit(1)\n"
        "lines = p.read_text("
        "encoding='utf-8', errors='replace'"
        ").splitlines()\n"
        "s = cfg['start'] - 1\n"
        "e = cfg['end']\n"
        "chunk = lines[s:e]\n"
        "for i, ln in enumerate(chunk, start=cfg['start']):\n"
        "    print(f'[L{i}] {ln}')\n"
    )
    read_py_file = _write_data_file(read_py, suffix=".py")
    script = (
        "#!/bin/bash\nset -euo pipefail\n"
        "cd /work/repo\n"
        "python3 /tmp/rfsn_data/reader.py\n"
    )
    return _run_docker_with_data(
        script,
        {
            "/tmp/rfsn_data/config.json": config_file,
            "/tmp/rfsn_data/reader.py": read_py_file,
        },
        repo_host,
        art_host,
        venv_host,
        wheels_host,
        timeout_s,
        network_disabled=True,
    )


def _apply_patch(
    patch,
    repo_host,
    art_host,
    venv_host,
    wheels_host,
    timeout_s,
):
    """SAFE: patch written to file, not heredoc."""
    if not patch.strip():
        return {
            "status": 1,
            "seconds": 0.0,
            "logs": "REJECTED: empty patch has no effect",
        }

    # ── Defense-in-depth: gate check ──────────────────
    # Redundant safety net — upstream (tool_gateway) should
    # have already gated. This blocks direct-call bypasses.
    if _HAS_PATCH_GATE and _patch_risk_gate is not None:
        gate_report = _patch_risk_gate(patch)
        if gate_report.decision == "REJECT":
            return {
                "status": 1,
                "seconds": 0.0,
                "logs": (
                    "DEFENSE_IN_DEPTH_REJECT: " + "; ".join(gate_report.reasons[:5])
                ),
            }
    patch_file = _write_data_file(patch, suffix=".patch")
    # Apply patch and then run git diff --stat to
    # verify it actually changed something.
    script = (
        "#!/bin/bash\nset -euo pipefail\ncd /work/repo\n"
        "git init --quiet 2>/dev/null || true\n"
        "git add -A 2>/dev/null || true\n"
        "git apply --whitespace=nowarn /tmp/rfsn_data/patch.diff\n"
        'echo "---PATCH_STAT_START---"\n'
        "git diff --stat 2>/dev/null || true\n"
        "git diff --numstat 2>/dev/null || true\n"
        'echo "---PATCH_STAT_END---"\n'
        'echo "patch applied successfully"\n'
    )
    out = _run_docker_with_data(
        script,
        {"/tmp/rfsn_data/patch.diff": patch_file},
        repo_host,
        art_host,
        venv_host,
        wheels_host,
        timeout_s,
        network_disabled=True,
    )

    # Parse patch stats from output.
    out["patch_meta"] = _parse_patch_stat(
        out.get("logs", ""),
    )

    # If patch applied but diff is empty, it had
    # no effect — hard fail.
    if (
        out["status"] == 0
        and out["patch_meta"].get(
            "files_touched",
            0,
        )
        == 0
    ):
        out["status"] = 1
        out["logs"] += "\nREJECTED: patch had no effect" " (diff is empty after apply)"

    return out


def _run_tests(
    template_id,
    target,
    repo_host,
    art_host,
    venv_host,
    wheels_host,
    timeout_s,
):
    if template_id not in CMD_TEMPLATES:
        raise HTTPException(
            403,
            f"unknown template_id: {template_id}",
        )
    tmpl = CMD_TEMPLATES[template_id]
    cmd = tmpl["cmd"]
    allowed_re = tmpl.get("allowed_target_regex", "")
    if allowed_re:
        if not re.fullmatch(allowed_re, target):
            raise HTTPException(
                403,
                "target rejected by regex:" f" {target!r} !~ {allowed_re}",
            )
    elif target:
        raise HTTPException(
            403,
            f"target not allowed for template" f" {template_id}",
        )
    safe_target = shlex.quote(target) if target else ""
    cmd_str = " ".join([shlex.quote(x) for x in cmd])
    cmd_str = cmd_str.replace("{target}", safe_target)
    # Run tests from a scratch copy so tests cannot
    # mutate canonical /work/repo state.
    cmd_str = cmd_str.replace("cd repo", "cd scratch_repo")
    script = (
        "#!/bin/bash\nset -euo pipefail\n"
        "cd /work\n"
        "rm -rf /work/scratch_repo\n"
        "mkdir -p /work/scratch_repo\n"
        "cp -a /work/repo/. /work/scratch_repo/\n"
        "# git-init-once: ensure mutation detection works even pre-patch\n"
        "if [ ! -d /work/repo/.git ]; then\n"
        '  (cd /work/repo && git init -q && git add -A && git commit -qm "baseline" --allow-empty) || true\n'
        "fi\n"
        'BEFORE_TRACKED="$(cd /work/repo && git status --porcelain 2>/dev/null || true)"\n'
        "if [ ! -f /work/venv/bin/activate ]; then\n"
        '  echo "Missing venv; ensure_deps first"\n'
        "  exit 39\n"
        "fi\n"
        f"{cmd_str}\n"
        'AFTER_TRACKED="$(cd /work/repo && git status --porcelain 2>/dev/null || true)"\n'
        'if [ "$AFTER_TRACKED" != "$BEFORE_TRACKED" ]; then\n'
        '  echo "tracked files mutated during tests"\n'
        "  exit 42\n"
        "fi\n"
    )
    return _run_docker_with_data(
        script,
        {},
        repo_host,
        art_host,
        venv_host,
        wheels_host,
        timeout_s,
        network_disabled=True,
    )


# ── Helpers for warm sandbox + patch verify ───


def _parse_patch_stat(logs: str) -> dict:
    """Parse git diff --stat/--numstat from logs."""
    meta: dict = {
        "files_touched": 0,
        "lines_added": 0,
        "lines_deleted": 0,
        "changed_files": [],
    }
    in_stat = False
    for line in logs.splitlines():
        if "---PATCH_STAT_START---" in line:
            in_stat = True
            continue
        if "---PATCH_STAT_END---" in line:
            in_stat = False
            continue
        if not in_stat:
            continue
        # numstat format: added\tdeleted\tfile
        parts = line.split("\t")
        if len(parts) == 3:
            try:
                a = int(parts[0])
                d = int(parts[1])
                f = parts[2].strip()
                meta["lines_added"] += a
                meta["lines_deleted"] += d
                meta["changed_files"].append(f)
            except ValueError:
                pass
    meta["files_touched"] = len(
        meta["changed_files"],
    )
    return meta


def _verify_patch_result(
    out: dict,
    step: dict,
    sandbox,
) -> dict:
    """Verify a patch had real effect in a warm
    sandbox by checking git diff --stat.
    """
    if out["status"] != 0:
        return out
    if not _sandbox_pool:
        return out

    # Run a quick stat check.
    stat_script = (
        "#!/bin/bash\ncd /work/repo\n" "git diff --numstat 2>/dev/null || true\n"
    )
    stat_out = _sandbox_pool.exec_in(
        sandbox,
        stat_script,
        {},
        10,
    )
    meta = _parse_patch_stat(stat_out.get("logs", ""))
    out["patch_meta"] = meta

    if meta["files_touched"] == 0:
        out["status"] = 1
        out["logs"] += "\nREJECTED: patch had no effect" " (diff is empty after apply)"
    return out


def _build_step_script(
    step_type: str,
    step: dict,
    repo_id: str,
) -> tuple:
    """Build script + data_files for a step type.

    Returns (script_str, data_files_dict).
    Used by both cold and warm execution paths.
    """
    data_files: dict = {}

    if step_type == "ensure_deps":
        manifest = DEPS.get(
            "manifest",
            "requirements.txt",
        )
        require_hashes = bool(
            DEPS.get("require_hashes", True),
        )
        only_binary = bool(
            DEPS.get("only_binary", True),
        )
        cache_dir = DEPS.get(
            "pip_cache_dir",
            "/work/wheels",
        )
        rh = 1 if require_hashes else 0
        ob = 1 if only_binary else 0
        script = (
            "#!/bin/bash\nset -euo pipefail\n"
            "cd /work/repo\n"
            f"MFST={shlex.quote(manifest)}\n"
            "if [ ! -f $MFST ]; then\n"
            f'  echo "Missing manifest: {manifest}"\n'
            "  exit 31\nfi\n"
            f"if [ {rh} -eq 1 ]; then\n"
            f'  if ! grep -q -- "--hash="'
            f" {shlex.quote(manifest)}; then\n"
            '    echo "Policy: requirements.txt'
            ' must include --hash entries"\n'
            "    exit 33\n  fi\nfi\n"
            "python -m venv /work/venv\n"
            ". /work/venv/bin/activate\n"
            "python -m pip install --upgrade pip\n"
            'BIN_FLAG=""\n'
            f"if [ {ob} -eq 1 ]; then"
            ' BIN_FLAG="--only-binary=:all:";'
            " fi\n"
            "python -m pip install"
            " --require-hashes $BIN_FLAG"
            f" --cache-dir {shlex.quote(cache_dir)}"
            f" -r {shlex.quote(manifest)}\n"
            'python -c "import sys;'
            " print('deps_ok', sys.version)\"\n"
        )
        return script, data_files

    if step_type == "repo_search":
        pattern = step.get("pattern") or ""
        pat_file = _write_data_file(
            pattern,
            suffix=".pattern",
        )
        search_py = (
            "import os, re, pathlib, json, sys\n"
            "try:\n"
            "    pat = open("
            "'/tmp/rfsn_data/pattern.txt'"
            ", 'r').read().strip()\n"
            "except Exception:\n"
            "    print('[]'); sys.exit(0)\n"
            "try:\n"
            "    rx = re.compile("
            "pat, re.MULTILINE)\n"
            "except re.error as e:\n"
            '    print(json.dumps({"error":'
            ' f"invalid regex: {e}"}))'
            "; sys.exit(1)\n"
            "root = pathlib.Path('.')\n"
            "EXTS = {'.py', '.js', '.ts',"
            " '.jsx', '.tsx',\n"
            "        '.java', '.rs', '.go',"
            " '.rb', '.c',\n"
            "        '.cpp', '.h', '.hpp',"
            " '.cs', '.swift',\n"
            "        '.kt', '.scala', '.toml',"
            " '.yaml',\n"
            "        '.yml', '.json', '.cfg',"
            " '.ini',\n"
            "        '.md', '.rst', '.txt',"
            " '.sh'}\n"
            "SKIP = {'.git', '__pycache__',"
            " 'node_modules',\n"
            "        '.tox', '.mypy_cache',"
            " '.eggs',\n"
            "        'dist', 'build',"
            " '.venv', 'venv'}\n"
            "CONFIG_NAMES = {'setup.py',"
            " 'setup.cfg',\n"
            "    'pyproject.toml', 'Makefile',"
            " 'Dockerfile',\n"
            "    'tox.ini', 'pytest.ini',"
            " '.flake8',\n"
            "    'Cargo.toml', 'go.mod',"
            " 'package.json',\n"
            "    'tsconfig.json', 'Gemfile',"
            " 'pom.xml',\n"
            "    'CMakeLists.txt', 'Pipfile',\n"
            "    'requirements.txt',"
            " 'requirements.in',\n"
            "    'MANIFEST.in', 'conftest.py'}\n"
            "out = []\n"
            "for cn in sorted(CONFIG_NAMES):\n"
            "    cp = root / cn\n"
            "    if not cp.is_file():\n"
            "        continue\n"
            "    try:\n"
            "        txt = cp.read_text("
            "encoding='utf-8',"
            " errors='replace')\n"
            "    except Exception:\n"
            "        continue\n"
            "    if rx.search(txt):\n"
            "        out.append(str(cp))\n"
            "for p in root.rglob('*'):\n"
            "    if any(s in p.parts"
            " for s in SKIP):\n"
            "        continue\n"
            "    if not p.is_file():\n"
            "        continue\n"
            "    if p.suffix not in EXTS:\n"
            "        continue\n"
            "    if str(p) in out:\n"
            "        continue\n"
            "    try:\n"
            "        txt = p.read_text("
            "encoding='utf-8',"
            " errors='replace')\n"
            "    except Exception:\n"
            "        continue\n"
            "    if rx.search(txt):\n"
            "        out.append(str(p))\n"
            "    if len(out) >= 200:\n"
            "        break\n"
            "print(json.dumps(out[:200]))\n"
        )
        spy_file = _write_data_file(
            search_py,
            suffix=".py",
        )
        data_files = {
            "/tmp/rfsn_data/pattern.txt": pat_file,
            "/tmp/rfsn_data/search.py": spy_file,
        }
        script = (
            "#!/bin/bash\nset -euo pipefail\n"
            "cd /work/repo\n"
            "python3 /tmp/rfsn_data/search.py\n"
        )
        return script, data_files

    if step_type == "repo_read_range":
        path = step.get("path") or ""
        ls = int(step.get("line_start") or 1)
        le = int(step.get("line_end") or ls)
        config = json.dumps(
            {
                "path": path,
                "start": ls,
                "end": le,
            }
        )
        cfg_file = _write_data_file(
            config,
            suffix=".json",
        )
        read_py = (
            "import pathlib, sys, json\n"
            "cfg = json.loads(open("
            "'/tmp/rfsn_data/config.json'"
            ").read())\n"
            "p = pathlib.Path(cfg['path'])\n"
            "if not p.exists()"
            " or not p.is_file():\n"
            "    print('', end='');"
            " sys.exit(0)\n"
            "rp = p.resolve()\n"
            "if not str(rp).startswith("
            "str(pathlib.Path('.').resolve())):\n"
            "    print('path traversal blocked',"
            " file=sys.stderr);"
            " sys.exit(1)\n"
            "lines = p.read_text("
            "encoding='utf-8',"
            " errors='replace').splitlines()\n"
            "s = cfg['start'] - 1\n"
            "e = cfg['end']\n"
            "chunk = lines[s:e]\n"
            "for i, ln in enumerate("
            "chunk, start=cfg['start']):\n"
            "    print(f'[L{i}] {ln}')\n"
        )
        rpy_file = _write_data_file(
            read_py,
            suffix=".py",
        )
        data_files = {
            "/tmp/rfsn_data/config.json": cfg_file,
            "/tmp/rfsn_data/reader.py": rpy_file,
        }
        script = (
            "#!/bin/bash\nset -euo pipefail\n"
            "cd /work/repo\n"
            "python3 /tmp/rfsn_data/reader.py\n"
        )
        return script, data_files

    if step_type == "read_file":
        path = step.get("path") or ""
        config = json.dumps(
            {
                "path": path,
                "max_bytes": MAX_READ_FILE_BYTES,
            }
        )
        cfg_file = _write_data_file(
            config,
            suffix=".json",
        )
        read_py = (
            "import hashlib, json, pathlib, sys\n"
            "cfg = json.loads(open('/tmp/rfsn_data/config.json').read())\n"
            "p = pathlib.Path(cfg['path'])\n"
            "if not p.exists() or not p.is_file():\n"
            "  print(json.dumps({'error': 'file not found'})); sys.exit(1)\n"
            "rp = p.resolve()\n"
            "root = pathlib.Path('.').resolve()\n"
            "if not str(rp).startswith(str(root)):\n"
            "  print(json.dumps({'error': 'path traversal blocked'})); sys.exit(1)\n"
            "data = p.read_bytes()\n"
            "if len(data) > int(cfg.get('max_bytes', 65536)):\n"
            "  print(json.dumps({'error': 'file too large'})); sys.exit(1)\n"
            "sha = hashlib.sha256(data).hexdigest()\n"
            "txt = data.decode('utf-8', errors='replace')\n"
            "print(json.dumps({'path': cfg['path'], 'sha256': sha, 'content': txt}))\n"
        )
        rpy_file = _write_data_file(read_py, suffix=".py")
        data_files = {
            "/tmp/rfsn_data/config.json": cfg_file,
            "/tmp/rfsn_data/read_file.py": rpy_file,
        }
        script = (
            "#!/bin/bash\nset -euo pipefail\n"
            "cd /work/repo\n"
            "python3 /tmp/rfsn_data/read_file.py\n"
        )
        return script, data_files

    if step_type == "detect_project":
        detect_py = (
            "import json, os\n"
            "found = {}\n"
            "cand = [\n"
            " 'pyproject.toml','requirements.txt','setup.cfg','setup.py',\n"
            " 'package.json','pnpm-lock.yaml','yarn.lock','package-lock.json',\n"
            " 'go.mod','Cargo.toml','Makefile','.github/workflows'\n"
            "]\n"
            "for rel in cand:\n"
            "  if os.path.exists(rel):\n"
            "    found[rel] = True\n"
            "profile = {\n"
            " 'has_python': any(k in found for k in ['pyproject.toml','requirements.txt','setup.py','setup.cfg']),\n"
            " 'has_node': any(k in found for k in ['package.json','pnpm-lock.yaml','yarn.lock','package-lock.json']),\n"
            " 'has_go': 'go.mod' in found,\n"
            " 'has_rust': 'Cargo.toml' in found,\n"
            " 'has_make': 'Makefile' in found,\n"
            " 'has_github_actions': '.github/workflows' in found,\n"
            " 'found': sorted(list(found.keys())),\n"
            "}\n"
            "print(json.dumps({'profile': profile}))\n"
        )
        dpy_file = _write_data_file(
            detect_py,
            suffix=".py",
        )
        data_files = {
            "/tmp/rfsn_data/detect_project.py": dpy_file,
        }
        script = (
            "#!/bin/bash\nset -euo pipefail\n"
            "cd /work/repo\n"
            "python3 /tmp/rfsn_data/detect_project.py\n"
        )
        return script, data_files

    if step_type == "detect_workdirs":
        max_depth = int(step.get("max_depth") or 4)
        max_depth = min(max(max_depth, 1), 8)
        cfg_file = _write_data_file(
            json.dumps(
                {
                    "max_depth": max_depth,
                    "max_workdirs": MAX_WORKDIRS,
                }
            ),
            suffix=".json",
        )
        detect_py = (
            "import json, os\n"
            "from collections import deque\n"
            "cfg = json.loads(open('/tmp/rfsn_data/config.json').read())\n"
            "max_depth = int(cfg.get('max_depth', 4))\n"
            "max_workdirs = int(cfg.get('max_workdirs', 24))\n"
            "markers = ['pyproject.toml','requirements.txt','setup.py','setup.cfg','package.json','go.mod','Cargo.toml','Makefile']\n"
            "skip = {'.git','.venv','venv','__pycache__','node_modules','dist','build','.ruff_cache','.pytest_cache'}\n"
            "q = deque([('.', 0)])\n"
            "out = []\n"
            "while q and len(out) < max_workdirs:\n"
            "  rel, depth = q.popleft()\n"
            "  abs_dir = os.path.abspath(rel)\n"
            "  try:\n"
            "    entries = list(os.scandir(abs_dir))\n"
            "  except Exception:\n"
            "    continue\n"
            "  found = []\n"
            "  for m in markers:\n"
            "    if os.path.exists(os.path.join(abs_dir, m)):\n"
            "      found.append(m)\n"
            "  if found:\n"
            "    out.append({'rel': '.' if rel in ('', '.') else rel, 'markers': sorted(found)})\n"
            "  if depth >= max_depth:\n"
            "    continue\n"
            "  for e in entries:\n"
            "    if not e.is_dir(follow_symlinks=False):\n"
            "      continue\n"
            "    if e.name in skip:\n"
            "      continue\n"
            "    child = e.name if rel in ('', '.') else f'{rel}/{e.name}'\n"
            "    q.append((child, depth + 1))\n"
            "workdirs = []\n"
            "for i, item in enumerate(out):\n"
            "  workdirs.append({'id': f'workdir_{i}', 'rel': item['rel'], 'markers': item['markers']})\n"
            "print(json.dumps({'workdirs': workdirs}))\n"
        )
        dpy_file = _write_data_file(
            detect_py,
            suffix=".py",
        )
        data_files = {
            "/tmp/rfsn_data/config.json": cfg_file,
            "/tmp/rfsn_data/detect_workdirs.py": dpy_file,
        }
        script = (
            "#!/bin/bash\nset -euo pipefail\n"
            "cd /work/repo\n"
            "python3 /tmp/rfsn_data/detect_workdirs.py\n"
        )
        return script, data_files

    if step_type in ("run_cmd_template", "format_fix"):
        template = str(step.get("template") or "")
        try:
            argv = _resolve_safe_template(template)
        except HTTPException:
            script = "#!/bin/bash\n" "echo 'unknown template'\n" "exit 1\n"
            return script, data_files
        workdir = _normalize_workdir(
            str(step.get("workdir") or "."),
        )
        cmd = " ".join(shlex.quote(x) for x in argv)
        script = (
            "#!/bin/bash\nset -euo pipefail\n"
            "cd /work/repo\n"
            "if [ -f /work/venv/bin/activate ]; then\n"
            "  . /work/venv/bin/activate\n"
            "fi\n"
            f"cd {shlex.quote(workdir)}\n"
            f"{cmd}\n"
        )
        return script, data_files

    if step_type == "apply_patch":
        patch = step.get("patch") or ""
        p_file = _write_data_file(
            patch,
            suffix=".patch",
        )
        data_files = {
            "/tmp/rfsn_data/patch.diff": p_file,
        }
        script = (
            "#!/bin/bash\nset -euo pipefail\n"
            "cd /work/repo\n"
            "git init --quiet 2>/dev/null"
            " || true\n"
            "git add -A 2>/dev/null || true\n"
            "git apply --whitespace=nowarn"
            " /tmp/rfsn_data/patch.diff\n"
            'echo "patch applied successfully"\n'
        )
        return script, data_files

    if step_type == "run_tests":
        template_id = step.get(
            "template_id",
            "",
        )
        params = (
            step.get(
                "template_params",
                {},
            )
            or {}
        )
        target = params.get("target", "")
        if template_id not in CMD_TEMPLATES:
            # Return a failing script.
            script = (
                "#!/bin/bash\n"
                "echo 'unknown template_id:"
                f" {template_id}'\n"
                "exit 1\n"
            )
            return script, data_files
        tmpl = CMD_TEMPLATES[template_id]
        cmd = tmpl["cmd"]
        safe_target = shlex.quote(target) if target else ""
        cmd_str = " ".join(
            [shlex.quote(x) for x in cmd],
        )
        cmd_str = cmd_str.replace(
            "{target}",
            safe_target,
        )
        cmd_str = cmd_str.replace("cd repo", "cd scratch_repo")
        script = (
            "#!/bin/bash\nset -euo pipefail\n"
            "cd /work\n"
            "rm -rf /work/scratch_repo\n"
            "mkdir -p /work/scratch_repo\n"
            "cp -a /work/repo/. /work/scratch_repo/\n"
            "# git-init-once: ensure mutation detection works even pre-patch\n"
            "if [ ! -d /work/repo/.git ]; then\n"
            '  (cd /work/repo && git init -q && git add -A && git commit -qm "baseline" --allow-empty) || true\n'
            "fi\n"
            'BEFORE_TRACKED="$(cd /work/repo && git status --porcelain 2>/dev/null || true)"\n'
            "if [ ! -f"
            " /work/venv/bin/activate ]; then\n"
            '  echo "Missing venv;'
            ' ensure_deps first"\n'
            "  exit 39\nfi\n"
            f"{cmd_str}\n"
            'AFTER_TRACKED="$(cd /work/repo && git status --porcelain 2>/dev/null || true)"\n'
            'if [ "$AFTER_TRACKED" != "$BEFORE_TRACKED" ]; then\n'
            '  echo "tracked files mutated during tests"\n'
            "  exit 42\n"
            "fi\n"
        )
        return script, data_files

    # Unknown step type.
    script = "#!/bin/bash\n" f"echo 'unknown step type: {step_type}'\n" "exit 1\n"
    return script, data_files


@app.post("/run")
def run(req: ExecReq):
    repo_host, art_host, venv_host, wheels_host = _paths(req.repo_id)
    step = req.step
    t = step.get("type")
    allow_network = bool(step.get("_rfsn_allow_network"))
    tier = _coerce_int(step.get("_rfsn_tier"), 0)
    network_reason = str(step.get("_rfsn_network_reason") or "")

    if t == "ensure_deps":
        _require_network_tier(step)
        allow_network = True
        timeout_s = int(step.get("timeout_s") or DEPS.get("max_install_seconds", 420))
        artifact_before = _dir_size_bytes(art_host)
        out = _ensure_deps(
            req.repo_id,
            repo_host,
            art_host,
            venv_host,
            wheels_host,
            timeout_s,
        )
        artifact_after = _dir_size_bytes(art_host)
        _apply_artifact_quota(
            out,
            before_size=artifact_before,
            after_size=artifact_after,
        )
        return _result(
            out=out,
            payload=None,
            network_mode=str(out.get("network_mode") or "bridge"),
            allow_network=allow_network,
            tier=tier,
            network_reason=network_reason,
        )

    if t == "repo_search":
        pattern = step.get("pattern") or ""
        timeout_s = int(step.get("timeout_s") or 30)
        artifact_before = _dir_size_bytes(art_host)
        out = _repo_search(
            pattern,
            repo_host,
            art_host,
            venv_host,
            wheels_host,
            timeout_s,
        )
        artifact_after = _dir_size_bytes(art_host)
        _apply_artifact_quota(
            out,
            before_size=artifact_before,
            after_size=artifact_after,
        )
        payload = out["logs"].strip()
        return _result(
            out=out,
            payload=payload,
            network_mode=str(out.get("network_mode") or "none"),
            allow_network=allow_network,
            tier=tier,
            network_reason=network_reason,
        )

    if t == "repo_read_range":
        path = step.get("path") or ""
        ls = int(step.get("line_start") or 1)
        le = int(step.get("line_end") or ls)
        timeout_s = int(step.get("timeout_s") or 30)
        artifact_before = _dir_size_bytes(art_host)
        out = _repo_read_range(
            path,
            ls,
            le,
            repo_host,
            art_host,
            venv_host,
            wheels_host,
            timeout_s,
        )
        artifact_after = _dir_size_bytes(art_host)
        _apply_artifact_quota(
            out,
            before_size=artifact_before,
            after_size=artifact_after,
        )
        payload = out["logs"]
        return _result(
            out=out,
            payload=payload,
            network_mode=str(out.get("network_mode") or "none"),
            allow_network=allow_network,
            tier=tier,
            network_reason=network_reason,
        )

    if t in (
        "read_file",
        "detect_project",
        "detect_workdirs",
    ):
        timeout_s = int(step.get("timeout_s") or 60)
        script, data_files = _build_step_script(
            t,
            step,
            req.repo_id,
        )
        artifact_before = _dir_size_bytes(art_host)
        out = _run_docker_with_data(
            script,
            data_files,
            repo_host,
            art_host,
            venv_host,
            wheels_host,
            timeout_s,
            network_disabled=True,
        )
        artifact_after = _dir_size_bytes(art_host)
        _apply_artifact_quota(
            out,
            before_size=artifact_before,
            after_size=artifact_after,
        )
        payload = out.get("logs", "").strip()
        return _result(
            out=out,
            payload=payload,
            network_mode=str(out.get("network_mode") or "none"),
            allow_network=allow_network,
            tier=tier,
            network_reason=network_reason,
        )

    if t == "apply_patch":
        patch = step.get("patch") or ""
        timeout_s = int(step.get("timeout_s") or 60)
        artifact_before = _dir_size_bytes(art_host)
        out = _apply_patch(
            patch,
            repo_host,
            art_host,
            venv_host,
            wheels_host,
            timeout_s,
        )
        artifact_after = _dir_size_bytes(art_host)
        _apply_artifact_quota(
            out,
            before_size=artifact_before,
            after_size=artifact_after,
        )
        return _result(
            out=out,
            payload=None,
            network_mode=str(out.get("network_mode") or "none"),
            allow_network=allow_network,
            tier=tier,
            network_reason=network_reason,
        )

    if t == "run_tests":
        template_id = step.get("template_id") or ""
        params = step.get("template_params") or {}
        target = params.get("target") or ""
        timeout_s = int(
            step.get("timeout_s")
            or CMD_TEMPLATES.get(template_id, {}).get("max_seconds", 240)
        )
        artifact_before = _dir_size_bytes(art_host)
        out = _run_tests(
            template_id,
            target,
            repo_host,
            art_host,
            venv_host,
            wheels_host,
            timeout_s,
        )
        artifact_after = _dir_size_bytes(art_host)
        _apply_artifact_quota(
            out,
            before_size=artifact_before,
            after_size=artifact_after,
        )
        tmpl_cmd = CMD_TEMPLATES.get(template_id, {}).get("cmd", None)
        command = [str(x) for x in tmpl_cmd] if isinstance(tmpl_cmd, list) else None
        return _result(
            out=out,
            payload=None,
            command=command,
            network_mode=str(out.get("network_mode") or "none"),
            allow_network=allow_network,
            tier=tier,
            network_reason=network_reason,
        )

    if t in ("run_cmd_template", "format_fix"):
        timeout_s = int(step.get("timeout_s") or 240)
        script, data_files = _build_step_script(
            t,
            step,
            req.repo_id,
        )
        artifact_before = _dir_size_bytes(art_host)
        out = _run_docker_with_data(
            script,
            data_files,
            repo_host,
            art_host,
            venv_host,
            wheels_host,
            timeout_s,
            network_disabled=True,
        )
        artifact_after = _dir_size_bytes(art_host)
        _apply_artifact_quota(
            out,
            before_size=artifact_before,
            after_size=artifact_after,
        )
        template = str(step.get("template") or "")
        command = None
        try:
            command = _resolve_safe_template(template)
        except HTTPException:
            command = None
        return _result(
            out=out,
            payload=None,
            command=command,
            workdir=str(step.get("workdir") or "."),
            network_mode=str(out.get("network_mode") or "none"),
            allow_network=allow_network,
            tier=tier,
            network_reason=network_reason,
        )

    raise HTTPException(400, f"unknown step type: {t}")
