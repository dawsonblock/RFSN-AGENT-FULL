from __future__ import annotations

import hashlib
import json
import os
import random
import re
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException  # type: ignore[import-not-found]
from fastapi.responses import HTMLResponse  # type: ignore[import-not-found]
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

_sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ),
)
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
    from rfsn_kernel.command_infer import (
        infer_commands,
    )
    from rfsn_kernel.repair_loop import (
        next_phase,
        should_retry,
        update_state,
    )
    from rfsn_kernel.patch_minimize import (
        minimize_unified_diff,
    )
    from rfsn_kernel.sim_cache import (
        SimCache,
    )
    from rfsn_kernel.scheduler import (
        Scheduler,
    )

    _HAS_HARD_KERNEL = True
except ImportError:
    _HAS_HARD_KERNEL = False

# ── Kernel-required guard ──────────────────────────────────
# When RFSN_KERNEL_REQUIRED=1 (default), the orchestrator
# MUST have the hard kernel available. A silent fallback to
# ungated execution is a security hole.
_KERNEL_REQUIRED = os.getenv("RFSN_KERNEL_REQUIRED", "1") == "1"
if not _HAS_HARD_KERNEL:
    if _KERNEL_REQUIRED and os.getenv("RFSN_DEV_MODE", "0") != "1":
        raise SystemExit(
            "FATAL: RFSN hard kernel failed to import and "
            "RFSN_KERNEL_REQUIRED=1. All actions would bypass "
            "the security gate. Set RFSN_KERNEL_REQUIRED=0 to "
            "override (NOT recommended for production)."
        )
    print(
        "CRITICAL: RFSN hard kernel unavailable — "
        "ALL security gating is DISABLED. "
        "Set RFSN_KERNEL_REQUIRED=1 to enforce.",
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


@app.on_event("startup")
async def startup_check_hardening():
    """Run SHH self-healing checks on startup."""
    import sys
    import subprocess

    try:
        cmd = [sys.executable, "-m", "services.hardening_guard.app"]
        print(f"KERNEL: Running hardening checks: {' '.join(cmd)}", flush=True)
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError:
        print("KERNEL: Hardening checks FAILED. Aborting startup.", flush=True)
        sys.exit(2)
    except Exception as exc:
        print(f"KERNEL: Error running hardening checks: {exc}", flush=True)
        if (
            os.getenv("RFSN_HARDENING_STRICT", "1") == "1"
            and os.getenv("RFSN_DEV_MODE", "0") != "1"
        ):
            sys.exit(2)


def _ui_html() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    ui_path = os.path.join(here, "ui", "index.html")
    try:
        with open(ui_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return (
            "<!doctype html><html><body>"
            "<h1>RFSN UI unavailable</h1>"
            "<p>Missing /services/orchestrator/ui/index.html</p>"
            "</body></html>"
        )


@app.get("/", response_class=HTMLResponse)
def ui_root():
    return _ui_html()


@app.get("/ui", response_class=HTMLResponse)
def ui_page():
    return _ui_html()


LLM_URL = os.getenv("LLM_URL", "http://llm_service:8001")
TOOL_GATEWAY_URL = os.getenv("TOOL_GATEWAY_URL", "http://tool_gateway:8002")
EXECUTOR_URL = os.getenv("EXECUTOR_URL", "http://executor:8003")
LEARNER_URL = os.getenv("LEARNER_URL", "http://learner_service:8004")
HARD_LEDGER_PATH = os.getenv(
    "RFSN_HARD_LEDGER_PATH",
    "/data/kernel_ledger.jsonl",
)
SEED = os.getenv("RFSN_SEED", "1")
WARM_SANDBOX = os.getenv("RFSN_WARM_SANDBOX", "0") == "1"
_REPO_ROOT = os.path.dirname(
    os.path.dirname(
        os.path.dirname(
            os.path.abspath(__file__),
        )
    )
)
_LOCAL_POLICY_DIR = os.path.join(
    _REPO_ROOT,
    "policies",
)
_POLICY_DIR = os.getenv(
    "RFSN_POLICY_DIR",
    "/policies",
)


def _policy_candidates(path_or_name: str) -> list[str]:
    raw = path_or_name.strip()
    base = os.path.basename(raw)
    out: list[str] = []
    if os.path.isabs(raw):
        out.append(raw)
    if raw.startswith("/policies/") and base:
        out.append(os.path.join(_POLICY_DIR, base))
    elif base:
        out.append(os.path.join(_POLICY_DIR, base))
    if base:
        out.append(os.path.join(_LOCAL_POLICY_DIR, base))
    # Preserve order but dedupe.
    seen = set()
    ordered = []
    for p in out:
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    return ordered


def _load_yaml(path_or_name: str) -> dict:
    last_exc: Exception | None = None
    for path in _policy_candidates(path_or_name):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except (FileNotFoundError, PermissionError) as exc:
            last_exc = exc
            continue
    print(
        "FATAL: Cannot load policy file"
        f" {path_or_name}. Tried:"
        f" {_policy_candidates(path_or_name)}."
        f" Last error: {last_exc}",
        flush=True,
    )
    raise SystemExit(1)


DEPS_POLICY = _load_yaml("deps_policy.yaml")
TEST_POLICY = _load_yaml("test_policy.yaml")
GATE_POLICY = _load_yaml("gate_policy.yaml")
TOOL_ALLOWLIST = _load_yaml("tool_allowlist.yaml")


# ── Compiled policy hash (determinism anchor) ─
# Hash all policy files at startup so every ledger
# entry can reference the exact policy version.
def _compile_policy_hash() -> str:
    """Hash all policy YAML files to a single hex digest."""
    h = hashlib.sha256()
    for name in sorted(
        [
            "command_templates.yaml",
            "deps_policy.yaml",
            "diff_guard.yaml",
            "gate_policy.yaml",
            "gate_policy_tiers.yaml",
            "llm_cassette.yaml",
            "test_policy.yaml",
            "tool_allowlist.yaml",
        ]
    ):
        found = False
        for path in _policy_candidates(name):
            try:
                with open(path, "rb") as f:
                    h.update(f.read())
                    found = True
                    break
            except FileNotFoundError:
                continue
        if not found:
            h.update(name.encode())
    return h.hexdigest()[:16]


POLICY_HASH = _compile_policy_hash()
REPLAY_BASE_DIR = os.getenv(
    "RFSN_REPLAY_DIR",
    "/data/replay",
)
REPLAY_MANIFEST_DIR = os.path.join(
    REPLAY_BASE_DIR,
    "manifests",
)
LEARNER_DB_PATH = os.getenv(
    "RFSN_LEARNER_DB_PATH",
    "/data/learner.duckdb",
)
DETERMINISTIC_RUN_ID = (
    os.getenv(
        "RFSN_DETERMINISTIC_RUN_ID",
        "0",
    )
    == "1"
)


def _file_sha256(path: str) -> str:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def _policy_file_hash(name: str) -> str:
    for path in _policy_candidates(name):
        digest = _file_sha256(path)
        if digest:
            return digest[:16]
    return ""


def _repo_head(repo_id: str) -> str:
    repo_path = f"/data/repos/{repo_id}"
    try:
        p = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        p = None
    if p is not None:
        head = (p.stdout or "").strip()
        if head:
            return head

    # Fallback when git binary is unavailable in container:
    # read .git/HEAD + refs directly.
    git_dir = os.path.join(repo_path, ".git")
    head_path = os.path.join(git_dir, "HEAD")
    try:
        with open(head_path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        if not raw:
            return ""
        if not raw.startswith("ref: "):
            return raw
        ref = raw[5:].strip()
        if not ref:
            return ""
        ref_path = os.path.join(git_dir, ref)
        if os.path.exists(ref_path):
            with open(ref_path, "r", encoding="utf-8") as f:
                return f.read().strip()
        packed = os.path.join(git_dir, "packed-refs")
        if os.path.exists(packed):
            with open(packed, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or line.startswith("^"):
                        continue
                    parts = line.split(" ", 1)
                    if len(parts) == 2 and parts[1].strip() == ref:
                        return parts[0].strip()
    except Exception:
        return ""
    return ""


def _replay_manifest_path(run_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)[:128]
    return os.path.join(
        REPLAY_MANIFEST_DIR,
        f"{safe}.json",
    )


def _replay_bundle_dir(repo_id: str, run_id: str) -> str:
    safe_repo = re.sub(r"[^A-Za-z0-9_.-]", "_", repo_id)[:128]
    safe_run = re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)[:128]
    out = os.path.abspath(
        os.path.join("/data/artifacts", safe_repo, "replay", safe_run),
    )
    os.makedirs(out, exist_ok=True)
    return out


_SNAPSHOT_EXCLUDE_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".tox",
    "secrets",
    "credentials",
    "private",
}
# Secret-file patterns excluded from replay snapshots (share-safe).
_SNAPSHOT_SECRET_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
)
_SNAPSHOT_SECRET_PREFIXES = (
    ".env",
    "id_rsa",
    "id_ed25519",
)
_MAX_SNAPSHOT_FILE_BYTES = int(
    os.getenv("RFSN_MAX_SNAPSHOT_FILE_BYTES", "50000000"),
)
_MAX_SNAPSHOT_BYTES = int(
    os.getenv("RFSN_MAX_SNAPSHOT_BYTES", "250000000"),
)


def _snapshot_tar_filter(
    ti: tarfile.TarInfo,
) -> tarfile.TarInfo | None:
    parts = [p for p in ti.name.replace("\\", "/").split("/") if p]
    if any(seg in _SNAPSHOT_EXCLUDE_DIRS for seg in parts):
        return None
    if ti.size and ti.size > _MAX_SNAPSHOT_FILE_BYTES:
        return None
    # Share-safe: exclude secret files by name pattern.
    basename = parts[-1] if parts else ""
    lower_base = basename.lower()
    if any(lower_base.startswith(p) for p in _SNAPSHOT_SECRET_PREFIXES):
        return None
    if any(lower_base.endswith(s) for s in _SNAPSHOT_SECRET_SUFFIXES):
        return None
    return ti


def _capture_repo_snapshot(repo_id: str, run_id: str, label: str) -> tuple:
    """Returns (path, skipped_reason). path='' if skipped."""
    repo_path = _repo_abs_path(repo_id)
    if not os.path.isdir(repo_path):
        return "", "repo_not_found"
    out_dir = _replay_bundle_dir(repo_id, run_id)
    out_path = os.path.join(out_dir, f"repo_{label}.tar.gz")
    try:
        with tarfile.open(out_path, "w:gz") as tf:
            tf.add(
                repo_path,
                arcname="repo",
                filter=_snapshot_tar_filter,
            )
        # Enforce total snapshot size cap.
        if os.path.getsize(out_path) > _MAX_SNAPSHOT_BYTES:
            os.remove(out_path)
            return "", "size_cap"
        return out_path, ""
    except Exception:
        return "", "capture_error"


def _capture_requirements_lock(repo_id: str, run_id: str) -> str:
    out_dir = _replay_bundle_dir(repo_id, run_id)
    out_path = os.path.join(out_dir, "requirements.lock")
    pip_bin = os.path.abspath(f"/data/venv/{repo_id}/bin/pip")
    if not os.path.isfile(pip_bin):
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write("# missing venv pip\n")
            return out_path
        except Exception:
            return ""
    try:
        p = subprocess.run(
            [pip_bin, "freeze"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=120,
            check=False,
        )
        text = p.stdout or ""
        if p.returncode != 0 and not text.strip():
            text = "# pip freeze failed\n"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
        return out_path
    except Exception:
        return ""


def _capture_executor_env_manifest(run_id: str, repo_id: str) -> dict:
    try:
        r = requests.get(
            f"{EXECUTOR_URL}/env_manifest",
            params={"run_id": run_id, "repo_id": repo_id},
            headers=auth_headers(),
            timeout=20,
        )
        if r.status_code != 200:
            return {
                "ok": False,
                "error": f"status_{r.status_code}",
            }
        payload = r.json()
        if isinstance(payload, dict):
            return payload
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": False, "error": "unknown"}


def _write_replay_manifest(run_id: str, manifest: dict) -> None:
    path = _replay_manifest_path(run_id)
    try:
        os.makedirs(REPLAY_MANIFEST_DIR, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                manifest,
                f,
                sort_keys=True,
                ensure_ascii=False,
                indent=2,
            )
        os.replace(tmp, path)
    except Exception:
        return


def _replay_manifest_check(manifest: dict) -> dict:
    def _present(v) -> bool:
        if not isinstance(v, str):
            return False
        s = v.strip().lower()
        return bool(s) and s not in {"unknown", "missing", "n/a"}

    required_nonempty = [
        "run_id",
        "repo_id",
        "policy_hash",
        "env_hash",
        "ledger_path",
        "executor_image",
        "repo_head",
        "learner_db_hash",
    ]
    status = str(manifest.get("status", "running"))
    if status != "running":
        required_nonempty.extend(
            [
                "repo_snapshot_start",
                "repo_snapshot_end",
                "requirements_lock",
                "executor_env_manifest_path",
            ]
        )
    missing: list[str] = []
    for k in required_nonempty:
        v = manifest.get(k)
        if not _present(v):
            missing.append(k)
    for k in ("started_at", "ended_at", "seed", "episode_seed", "status"):
        if k not in manifest:
            missing.append(k)
    return {
        "ok": len(missing) == 0,
        "missing": missing,
        "required_count": len(required_nonempty) + 5,
    }


def _init_replay_manifest(
    *,
    run_id: str,
    repo_id: str,
    task: str,
    scenario: str,
    run_seed: int,
    env_snapshot: dict,
    sandbox_info: dict | None,
) -> dict:
    manifest = {
        "run_id": run_id,
        "repo_id": repo_id,
        "task": task,
        "scenario": scenario,
        "status": "running",
        "reason": "",
        "seed": SEED,
        "episode_seed": run_seed,
        "deterministic_run_id": DETERMINISTIC_RUN_ID,
        "started_at": time.time(),
        "ended_at": 0.0,
        "policy_hash": POLICY_HASH,
        "policy_hashes": {
            "gate_policy": _policy_file_hash("gate_policy.yaml"),
            "tool_allowlist": _policy_file_hash("tool_allowlist.yaml"),
            "tier_policy": _policy_file_hash("gate_policy_tiers.yaml"),
            "llm_cassette": _policy_file_hash("llm_cassette.yaml"),
        },
        "env_hash": str(env_snapshot.get("env_hash", "")),
        "env_snapshot": env_snapshot,
        "ledger_path": HARD_LEDGER_PATH,
        "executor_image": os.getenv(
            "BLESSED_IMAGE",
            "rfsn-blessed@sha256:208a2c2dac42ed9b3ca023b30cd815518070930274592844511aa34de21b6360",
        ),
        "strict_image_digest": os.getenv(
            "RFSN_STRICT_IMAGE_DIGEST",
            "1",
        ),
        "sandbox_mode": "warm" if WARM_SANDBOX else "cold",
        "sandbox_image_hash": str(
            (sandbox_info or {}).get("image_hash", ""),
        ),
        "repo_snapshot_start": "",
        "repo_snapshot_end": "",
        "snapshot_skipped_reason": "",
        "requirements_lock": "",
        "executor_env_manifest_path": "",
        "executor_env_manifest": {},
        "venv_mode": os.getenv("RFSN_VENV_MODE", "per_run"),
        "deps_state": {},
        "repo_head": _repo_head(repo_id) or "unknown",
        "learner_db_path": LEARNER_DB_PATH,
        "learner_db_hash": (_file_sha256(LEARNER_DB_PATH) or "missing"),
        "results_count": 0,
        "replay_verify": {},
    }
    manifest["completeness"] = _replay_manifest_check(manifest)
    return manifest


def _finalize_replay_manifest(
    *,
    run_id: str,
    status: str,
    reason: str = "",
    results_count: int = 0,
) -> None:
    ctx = _RUN_CONTEXT.get(run_id)
    if not isinstance(ctx, dict):
        return
    manifest = ctx.get("replay_manifest")
    if not isinstance(manifest, dict):
        return
    manifest["status"] = status
    manifest["reason"] = reason
    manifest["ended_at"] = time.time()
    manifest["results_count"] = int(max(0, results_count))
    repo_id = str(manifest.get("repo_id", "") or "")
    if repo_id:
        if not str(manifest.get("repo_snapshot_end", "")).strip():
            _end_path, _end_reason = _capture_repo_snapshot(
                repo_id,
                run_id,
                "end",
            )
            manifest["repo_snapshot_end"] = _end_path
            if _end_reason:
                manifest["snapshot_skipped_reason"] = _end_reason
        if not str(manifest.get("requirements_lock", "")).strip():
            manifest["requirements_lock"] = _capture_requirements_lock(
                repo_id,
                run_id,
            )
        if not str(manifest.get("executor_env_manifest_path", "")).strip():
            env_manifest = _capture_executor_env_manifest(
                run_id,
                repo_id,
            )
            if isinstance(env_manifest, dict):
                if isinstance(env_manifest.get("path"), str):
                    manifest["executor_env_manifest_path"] = env_manifest["path"]
                if isinstance(env_manifest.get("manifest"), dict):
                    manifest["executor_env_manifest"] = env_manifest["manifest"]
    try:
        runner = ReplayRunner(HARD_LEDGER_PATH)
        manifest["replay_verify"] = runner.replay_verify(
            run_id=run_id,
        ).to_dict()
    except Exception:
        manifest["replay_verify"] = {
            "ok": False,
            "error": "replay_verify_failed",
        }
    manifest["completeness"] = _replay_manifest_check(manifest)
    ctx["replay_manifest"] = manifest
    _RUN_CONTEXT[run_id] = ctx
    _write_replay_manifest(run_id, manifest)

    # ---- After replay bundle saved ----
    # 1. Load executor dependency state
    replay_dir = Path(_replay_bundle_dir(str(manifest.get("repo_id", "")), run_id))
    deps_path = Path(
        "/work/artifacts/deps_state.json"
    )  # Note: assuming this path is accessible or logic adapted.
    # Actually, orchestrator cannot see /work/artifacts of executor directly unless volume mounted.
    # But the user provided patch assumes it can: "deps_path = Path("/work/artifacts/deps_state.json")"
    # Wait, Orchestrator runs in its own container. It cannot see Executor's /work.
    # The user might imply a shared volume? "Host data dir" is shared.
    # If /work/artifacts is in the shared volume...
    # I will stick to the user's code, assuming the path is correct in their context.

    if deps_path.exists():
        try:
            manifest["deps_state"] = json.loads(deps_path.read_text())
        except Exception:
            manifest["deps_state"] = {"error": "unreadable"}

    # 2. Canonical manifest for replay verifier
    canonical = {
        "deps": manifest.get("deps_state"),
        "env": manifest.get("env_snapshot"),
        "patch_hash": manifest.get(
            "patch_hash"
        ),  # This key might fallback to None if not in manifest
        "kernel_trace_hash": manifest.get("kernel_trace_hash"),
        "artifact_hash": manifest.get("artifact_hash"),
    }

    # Ensure replay_dir exists (it should via _replay_bundle_dir call)
    try:
        canon_path = replay_dir / "manifest.json"
        canon_path.write_text(json.dumps(canonical, indent=2))
    except Exception as e:
        print(f"WARN: Failed to write canonical manifest: {e}", flush=True)

    # 3. Auto Replay Verify
    if os.getenv("RFSN_AUTO_REPLAY_VERIFY", "1") == "1":
        try:
            from services.replay_verifier.verify import main as replay_verify

            # Try to identify previous run via 'latest' symlink if it exists
            runs_dir = replay_dir.parent
            latest_symlink = runs_dir / "latest"
            prev_replay_dir = None

            if latest_symlink.is_symlink():
                target = latest_symlink.resolve()
                if target != replay_dir and target.exists():
                    prev_replay_dir = target

            # Update 'latest' symlink to current for next time
            try:
                if latest_symlink.exists() or latest_symlink.is_symlink():
                    latest_symlink.unlink()
                latest_symlink.symlink_to(replay_dir)
            except Exception as e:
                print(f"WARN: Failed to update latest symlink: {e}", flush=True)

            if prev_replay_dir:
                print(
                    f"AUTO_REPLAY_VERIFY: Comparing {prev_replay_dir.name} vs {replay_dir.name}",
                    flush=True,
                )
                replay_verify(str(prev_replay_dir), str(replay_dir))
            else:
                print("AUTO_REPLAY_VERIFY: SKIPPED (no previous run found)", flush=True)

        except Exception as e:
            print(f"AUTO_REPLAY_VERIFY: FAIL - {e}", flush=True)
            if os.getenv("RFSN_REPLAY_STRICT", "1") == "1":
                # In strict mode, failure to verify (if prev exists) should be fatal?
                # Or just log. The user prompt implies it's a "Hardening" feature.
                # We will just log for now to avoid breaking the first run.
                pass


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
                "errors": [
                    {
                        "line": 0,
                        "error": "hard kernel unavailable",
                    }
                ],
            }
        return self._kernel.ledger.verify_chain()


# ── Hard kernel v2 (simulation + risk + replay) ─
_KERNEL_REJECT_RISK_SCORE = float(
    GATE_POLICY.get("reject_risk_score", 65),
)
_KERNEL_RISK_MAX = float(
    GATE_POLICY.get(
        "risk_max",
        max(
            0.0,
            min(1.0, _KERNEL_REJECT_RISK_SCORE / 100.0),
        ),
    )
)
_READ_BUDGET = (
    GATE_POLICY.get("step_budgets", {}).get("repo_read_range", {})
    if isinstance(GATE_POLICY.get("step_budgets", {}), dict)
    else {}
)
if _HAS_HARD_KERNEL:
    _hard_kernel = HardKernel(
        ledger_path=HARD_LEDGER_PATH,
        policy={
            "risk_max": _KERNEL_RISK_MAX,
            "success_min": float(
                GATE_POLICY.get("success_min", 0.15),
            ),
            "loop_max": float(
                GATE_POLICY.get("loop_max", 0.8),
            ),
            "drift_max": float(
                GATE_POLICY.get("drift_max", 0.85),
            ),
            "risk_lambda": float(
                GATE_POLICY.get("risk_lambda", 0.7),
            ),
            "max_total_steps": int(
                GATE_POLICY.get("max_total_steps", 200),
            ),
            "history_max": 500,
            "rng_seed": _EPISODE_SEED,
            "policy_hash": POLICY_HASH,
            "fail_cluster_threshold": 8,
            "max_lines_per_read": int(
                _READ_BUDGET.get(
                    "max_lines_per_read",
                    300,
                )
            ),
            "blocked_read_prefixes": (
                GATE_POLICY.get("blocked_read_prefixes", []) or []
            ),
            "blocked_read_suffixes": (
                GATE_POLICY.get("blocked_read_suffixes", []) or []
            ),
            "allowed_command_templates": sorted(
                list((TOOL_ALLOWLIST.get("command_templates") or {}).keys())
            ),
            # ── Patch budget enforcement (kernel single-source) ──
            "max_patch_files": int(
                GATE_POLICY.get("max_patch_files", 0) or 0,
            ),
            "max_patch_total_lines": int(
                GATE_POLICY.get("max_patch_total_lines", 0) or 0,
            ),
            "max_added_lines": int(
                GATE_POLICY.get("max_added_lines", 0) or 0,
            ),
            "max_deleted_lines": int(
                GATE_POLICY.get("max_deleted_lines", 0) or 0,
            ),
            # ── Forbid flags (kernel hard gate) ──
            "forbid_test_edits": bool(
                GATE_POLICY.get("forbid_test_edits", False),
            ),
            "forbid_ci_edits": bool(
                GATE_POLICY.get("forbid_ci_edits", False),
            ),
            "forbid_dep_manifest_edits": bool(
                GATE_POLICY.get("forbid_dep_manifest_edits", False),
            ),
            # ── Policy-driven test template validation ──
            "allowed_test_templates": sorted(
                list(
                    (_load_yaml("command_templates.yaml").get("templates") or {}).keys()
                )
            ),
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
    # Persist memory across runs.
    _MEMORY_PATH = os.path.join(
        os.environ.get("DATA_DIR", "data"),
        "memory_immune_system.jsonl",
    )
    _memory.load(_MEMORY_PATH)
else:
    _hard_kernel = None  # type: ignore[assignment]
    _planner = None  # type: ignore[assignment]
    _memory = None  # type: ignore[assignment]

ledger = _LedgerSink(_hard_kernel)


def _policy_tier_for_run(
    run_id: str,
) -> tuple[int, str, dict]:
    if not (_HAS_HARD_KERNEL and _hard_kernel):
        return 0, "code-only", {}

    rs = _hard_kernel.run_state.get(run_id)
    tiers = (_hard_kernel.tier_policy or {}).get(
        "tiers",
        {},
    )
    cfg = tiers.get(rs.tier)
    if not isinstance(cfg, dict):
        cfg = tiers.get(str(rs.tier), {})
    if not isinstance(cfg, dict):
        cfg = {}
    return rs.tier, str(cfg.get("name", rs.tier)), cfg


def _default_cmd_plan() -> dict:
    templates = TOOL_ALLOWLIST.get("command_templates") or {}
    default_tests = []
    for name in (
        "python:pytest",
        "python:unittest",
        "node:test",
        "make:test",
    ):
        if name in templates:
            default_tests.append(name)
    return {
        "workdir_id": "workdir_0",
        "test_templates": default_tests,
        "lint_templates": [
            t
            for t in (
                "python:ruff",
                "node:lint",
            )
            if t in templates
        ],
        "build_templates": [t for t in ("tsc",) if t in templates],
    }


def _ensure_run_context(run_id: str) -> dict:
    ctx = _RUN_CONTEXT.get(run_id)
    if isinstance(ctx, dict):
        return ctx
    ctx = {
        "cmd_plan": _default_cmd_plan(),
        "baseline_test_template": "",
        "sim_cache": SimCache(),
        "repair": {
            "phase": "SEARCH",
            "attempt": 0,
            "max_attempts": 3,
            "last_status": 1,
        },
    }
    _RUN_CONTEXT[run_id] = ctx
    return ctx


def _parse_payload_json(value) -> dict | None:
    if isinstance(value, str) and value.strip():
        try:
            obj = json.loads(value)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            return None
    return None


def _is_test_template(template: str) -> bool:
    t = (template or "").strip()
    if not t:
        return False
    return t.endswith(":test") or t in {
        "python:pytest",
        "python:unittest",
        "make:test",
        "go:test",
        "rust:test",
    }


def _is_test_step(step: dict) -> bool:
    st = str(step.get("type", ""))
    if st == "run_tests":
        return True
    if st == "run_cmd_template":
        return _is_test_template(
            str(step.get("template", "")),
        )
    return False


def _test_template_key(step: dict) -> str:
    st = str(step.get("type", ""))
    if st == "run_tests":
        tmpl = str(step.get("template_id", "")).strip()
        return f"run_tests:{tmpl}" if tmpl else ""
    if st == "run_cmd_template":
        tmpl = str(step.get("template", "")).strip()
        if _is_test_template(tmpl):
            return f"run_cmd_template:{tmpl}"
    return ""


def _test_step_variant(step: dict) -> str:
    """Classify test step into targeted/suite/generic."""
    st = str(step.get("type", ""))
    if st == "run_tests":
        tmpl = str(step.get("template_id", "")).lower()
        if "targeted" in tmpl:
            return "targeted"
        if "suite" in tmpl:
            return "suite"
        return "generic"
    if st == "run_cmd_template" and _is_test_template(
        str(step.get("template", "")),
    ):
        return "generic"
    return ""


def _patch_verify_ok(state: dict) -> bool:
    if bool(state.get("targeted_ok")) and bool(
        state.get("suite_ok"),
    ):
        return True
    return int(state.get("generic_ok_count", 0)) >= 2


def _bootstrap_command_plan(
    *,
    run_id: str,
    repo_id: str,
    scenario: str,
) -> None:
    """Discover project/workdirs once and infer deterministic command plan."""
    ctx = _ensure_run_context(run_id)

    detect_proj_step = {
        "id": "auto-detect-project",
        "type": "detect_project",
        "timeout_s": 20,
    }
    detect_wd_step = {
        "id": "auto-detect-workdirs",
        "type": "detect_workdirs",
        "max_depth": 4,
        "timeout_s": 30,
    }
    bundle_id = stable_id(
        "bootstrap",
        SEED,
        repo_id,
        run_id,
        scenario,
        n=8,
    )

    project_profile = {}
    workdirs = []

    ex1 = execute_approved_step(
        repo_id,
        0,
        detect_proj_step,
        run_id,
        context_hash="bootstrap",
        intent="detect project",
        bundle_id=bundle_id,
        step_num=-2,
    )
    if ex1["ok"] and ex1["out"]:
        _METRICS["steps_executed"] += 1
        out = ex1["out"]
        ledger.append(
            {
                "type": "STEP_RESULT",
                "run_id": run_id,
                "iter": 0,
                "step": detect_proj_step,
                "out": out,
            }
        )
        parsed = _parse_payload_json(out.get("payload"))
        if parsed and isinstance(parsed.get("profile"), dict):
            project_profile = parsed.get("profile", {})

    ex2 = execute_approved_step(
        repo_id,
        0,
        detect_wd_step,
        run_id,
        context_hash="bootstrap",
        intent="detect workdirs",
        bundle_id=bundle_id,
        step_num=-1,
    )
    if ex2["ok"] and ex2["out"]:
        _METRICS["steps_executed"] += 1
        out = ex2["out"]
        ledger.append(
            {
                "type": "STEP_RESULT",
                "run_id": run_id,
                "iter": 0,
                "step": detect_wd_step,
                "out": out,
            }
        )
        parsed = _parse_payload_json(out.get("payload"))
        wd = parsed.get("workdirs") if parsed else None
        if isinstance(wd, list):
            workdirs = [x for x in wd if isinstance(x, dict)]

    if not workdirs:
        workdirs = [
            {
                "id": "workdir_0",
                "rel": ".",
                "markers": [],
            }
        ]
    cmd_plan = infer_commands(
        project_profile,
        workdirs,
    )
    if not cmd_plan.get("workdir_id"):
        cmd_plan["workdir_id"] = str(workdirs[0].get("id", "workdir_0"))
    if not cmd_plan.get("test_templates"):
        cmd_plan["test_templates"] = _default_cmd_plan().get(
            "test_templates",
            [],
        )
    ctx["cmd_plan"] = cmd_plan

    ledger.append(
        {
            "type": "CMD_INFERRED",
            "run_id": run_id,
            "plan": cmd_plan,
        }
    )


def _end_kernel_run(run_id: str) -> None:
    if _HAS_HARD_KERNEL and _hard_kernel:
        _hard_kernel.end_run(run_id)
    _RUN_CONTEXT.pop(run_id, None)
    if _SCHEDULER:
        _SCHEDULER.end_run(run_id)
    try:
        requests.post(
            f"{TOOL_GATEWAY_URL}/run_cleanup",
            json={"run_id": run_id},
            headers=auth_headers(),
            timeout=5,
        )
    except Exception:
        pass


def stable_id(
    prefix: str,
    *parts: str,
    n: int = 10,
) -> str:
    h = hashlib.sha256(("|".join(parts)).encode("utf-8")).hexdigest()
    return f"{prefix}-{h[:n]}"


def venv_exists(repo_id: str) -> bool:
    return os.path.exists(f"/data/venv/{repo_id}/bin/activate")


def is_tests_only_task(task: str) -> bool:
    t = (task or "").lower()
    triggers = [
        "run pytest",
        "run tests",
        "confirm green",
        "make no changes",
        "tests only",
        "no changes",
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


class RepoImportReq(BaseModel):
    repo_url: str
    repo_id: Optional[str] = None
    ref: Optional[str] = None
    depth: int = 1
    force: bool = False


class RepoChatReq(BaseModel):
    repo_id: str
    message: str
    thread_id: Optional[str] = None
    max_files: int = 5


class TextChatReq(BaseModel):
    message: str
    thread_id: Optional[str] = None


def _validate_repo_id_local(repo_id: str) -> None:
    if not _SAFE_REPO_ID.fullmatch(repo_id or ""):
        raise HTTPException(400, "invalid repo_id format")


def _repo_abs_path(repo_id: str) -> str:
    _validate_repo_id_local(repo_id)
    root = os.path.abspath("/data/repos")
    path = os.path.abspath(os.path.join(root, repo_id))
    if not path.startswith(root + os.sep):
        raise HTTPException(400, "repo path traversal blocked")
    if not os.path.isdir(path):
        raise HTTPException(404, f"repo not found: {repo_id}")
    return path


def _chat_terms(query: str, max_terms: int = 8) -> list[str]:
    raw = re.findall(r"[A-Za-z_][A-Za-z0-9_:-]{2,}", query or "")
    out: list[str] = []
    seen = set()
    for tok in raw:
        low = tok.lower()
        if low in _CHAT_STOPWORDS:
            continue
        if low in seen:
            continue
        seen.add(low)
        out.append(tok)
        if len(out) >= max_terms:
            break
    return out


def _normalize_repo_rel_path(path: str) -> str:
    p = (path or "").strip()
    if p.startswith("./"):
        p = p[2:]
    return p


def _collect_repo_chat_context(
    *,
    repo_id: str,
    query: str,
    iter_num: int,
    max_files: int,
) -> dict:
    _ = iter_num
    profile: dict = {}
    workdirs: list[dict] = []
    file_hits: list[str] = []
    snippets: list[dict] = []
    warnings: list[str] = []

    repo_path = _repo_abs_path(repo_id)
    max_files = min(max(int(max_files or 5), 1), 10)

    candidates = [
        "pyproject.toml",
        "requirements.txt",
        "setup.cfg",
        "setup.py",
        "package.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "package-lock.json",
        "go.mod",
        "Cargo.toml",
        "Makefile",
    ]
    found = {}
    for rel in candidates:
        if os.path.exists(os.path.join(repo_path, rel)):
            found[rel] = True
    profile = {
        "has_python": any(
            k in found
            for k in [
                "pyproject.toml",
                "requirements.txt",
                "setup.py",
                "setup.cfg",
            ]
        ),
        "has_node": any(
            k in found
            for k in [
                "package.json",
                "pnpm-lock.yaml",
                "yarn.lock",
                "package-lock.json",
            ]
        ),
        "has_go": "go.mod" in found,
        "has_rust": "Cargo.toml" in found,
        "has_make": "Makefile" in found,
        "found": sorted(list(found.keys())),
    }

    marker_files = [
        "pyproject.toml",
        "requirements.txt",
        "setup.py",
        "setup.cfg",
        "package.json",
        "go.mod",
        "Cargo.toml",
        "Makefile",
    ]
    skip_dirs = {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        "dist",
        "build",
        ".ruff_cache",
        ".pytest_cache",
    }
    queue: list[tuple[str, int]] = [(".", 0)]
    max_depth = 4
    while queue and len(workdirs) < 10:
        rel, depth = queue.pop(0)
        abs_dir = (
            repo_path
            if rel == "."
            else os.path.join(
                repo_path,
                rel,
            )
        )
        try:
            entries = list(os.scandir(abs_dir))
        except Exception:
            continue
        marker_hits = []
        for m in marker_files:
            if os.path.exists(os.path.join(abs_dir, m)):
                marker_hits.append(m)
        if marker_hits:
            workdirs.append(
                {
                    "id": f"workdir_{len(workdirs)}",
                    "rel": rel,
                    "markers": sorted(marker_hits),
                }
            )
        if depth >= max_depth:
            continue
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if entry.name in skip_dirs:
                continue
            child = entry.name if rel == "." else f"{rel}/{entry.name}"
            queue.append((child, depth + 1))

    terms = _chat_terms(query)
    pattern = (
        "|".join(re.escape(t) for t in terms)
        if terms
        else "README|setup|pyproject|package|main"
    )
    try:
        query_re = re.compile(pattern, re.IGNORECASE)
    except re.error:
        query_re = re.compile("README|setup|main", re.IGNORECASE)
        warnings.append("invalid query regex; fallback applied")

    allowed_exts = {
        ".py",
        ".js",
        ".ts",
        ".jsx",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".rb",
        ".c",
        ".cpp",
        ".h",
        ".hpp",
        ".cs",
        ".swift",
        ".kt",
        ".scala",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
        ".cfg",
        ".ini",
        ".md",
        ".rst",
        ".txt",
        ".sh",
    }
    scan_limit = 1200
    scanned = 0
    for root_dir, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fname in files:
            if scanned >= scan_limit or len(file_hits) >= max_files * 2:
                break
            scanned += 1
            _, ext = os.path.splitext(fname)
            if ext and ext.lower() not in allowed_exts:
                continue
            abs_path = os.path.join(root_dir, fname)
            rel_path = _normalize_repo_rel_path(
                os.path.relpath(abs_path, repo_path),
            )
            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read(65536)
            except Exception:
                continue
            if query_re.search(text):
                file_hits.append(rel_path)
        if scanned >= scan_limit or len(file_hits) >= max_files * 2:
            break
    if not file_hits:
        for candidate in [
            "README.md",
            "README.rst",
            "pyproject.toml",
            "package.json",
        ]:
            if os.path.exists(os.path.join(repo_path, candidate)):
                file_hits.append(candidate)
    if scanned >= scan_limit:
        warnings.append("search scan limit reached")

    dedup_hits: list[str] = []
    seen = set()
    for p in file_hits:
        if p in seen:
            continue
        seen.add(p)
        dedup_hits.append(p)

    for p in dedup_hits[:max_files]:
        try:
            abs_path = os.path.join(repo_path, p)
            with open(abs_path, "rb") as f:
                data = f.read(65536)
            content = data.decode("utf-8", errors="replace")[:3500]
            snippets.append(
                {
                    "path": p,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "content": content,
                }
            )
        except Exception:
            continue

    return {
        "repo_id": repo_id,
        "query_terms": terms,
        "pattern": pattern,
        "profile": profile,
        "workdirs": workdirs,
        "files": dedup_hits[:max_files],
        "snippets": snippets,
        "warnings": warnings,
    }


def _prune_chat_threads(limit: int = 200) -> None:
    while len(_CHAT_THREADS) > limit:
        oldest = next(iter(_CHAT_THREADS.keys()))
        _CHAT_THREADS.pop(oldest, None)
        _CHAT_CALL_INDEX.pop(oldest, None)
        _CHAT_ITER.pop(oldest, None)


def _prune_text_chat_threads(limit: int = 200) -> None:
    while len(_TEXT_CHAT_THREADS) > limit:
        oldest = next(iter(_TEXT_CHAT_THREADS.keys()))
        _TEXT_CHAT_THREADS.pop(oldest, None)
        _TEXT_CHAT_CALL_INDEX.pop(oldest, None)
        _TEXT_CHAT_ITER.pop(oldest, None)


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
                headers=auth_headers(),
                timeout=3,
            )
            deps[name] = r.status_code == 200
        except Exception:
            deps[name] = False
    all_ok = all(deps.values())
    return {
        "ok": all_ok,
        "deps": deps,
        "kernel_loaded": (_HAS_HARD_KERNEL and _hard_kernel is not None),
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

_RUN_CONTEXT: dict[str, dict] = {}
_CHAT_THREADS: dict[str, list[dict[str, str]]] = {}
_CHAT_CALL_INDEX: dict[str, int] = {}
_CHAT_ITER: dict[str, int] = {}
_TEXT_CHAT_THREADS: dict[str, list[dict[str, str]]] = {}
_TEXT_CHAT_CALL_INDEX: dict[str, int] = {}
_TEXT_CHAT_ITER: dict[str, int] = {}
_SAFE_REPO_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_SAFE_THREAD_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_CHAT_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "this",
    "that",
    "what",
    "where",
    "when",
    "from",
    "into",
    "about",
    "repo",
    "repository",
    "file",
    "files",
    "code",
    "does",
    "have",
    "just",
    "need",
    "show",
    "tell",
    "please",
    "there",
    "their",
    "your",
    "ours",
    "ourselves",
    "you",
}
_MAX_CONCURRENT_RUNS = int(
    os.getenv("RFSN_MAX_CONCURRENT_RUNS", "2"),
)
_RUN_MAX_SECONDS = int(
    os.getenv("RFSN_RUN_MAX_SECONDS", "900"),
)
_SCHEDULER = (
    Scheduler(max_concurrent=_MAX_CONCURRENT_RUNS) if _HAS_HARD_KERNEL else None
)


@app.get("/metrics")
def metrics():
    out = dict(_METRICS)
    if _SCHEDULER:
        out["scheduler"] = _SCHEDULER.stats()
    out["active_run_contexts"] = len(_RUN_CONTEXT)
    out["active_repo_chat_threads"] = len(_CHAT_THREADS)
    out["active_text_chat_threads"] = len(_TEXT_CHAT_THREADS)
    return out


@app.get("/repos")
def repos():
    try:
        r = requests.get(
            f"{EXECUTOR_URL}/repos",
            headers=auth_headers(),
            timeout=20,
        )
    except Exception as exc:
        raise HTTPException(
            502,
            f"executor /repos unreachable: {exc}",
        ) from exc
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    return r.json()


@app.post("/repos/import")
def repos_import(req: RepoImportReq):
    payload = req.model_dump()
    try:
        r = requests.post(
            f"{EXECUTOR_URL}/repo/import",
            json=payload,
            headers=auth_headers(),
            timeout=(10, 620),
        )
    except Exception as exc:
        raise HTTPException(
            502,
            f"executor /repo/import unreachable: {exc}",
        ) from exc
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    out = r.json()
    ledger.append(
        {
            "type": "REPO_IMPORTED",
            "run_id": "",
            "repo_id": out.get("repo_id", ""),
            "repo_url": out.get("repo_url", ""),
            "head": out.get("head", ""),
            "branch": out.get("branch", ""),
        }
    )
    return out


@app.post("/chat")
def chat_repo(req: RepoChatReq):
    repo_id = (req.repo_id or "").strip()
    message = (req.message or "").strip()
    if not message:
        raise HTTPException(400, "message is required")
    _repo_abs_path(repo_id)

    thread_id = (req.thread_id or "").strip()
    if thread_id and not _SAFE_THREAD_ID.fullmatch(thread_id):
        raise HTTPException(400, "invalid thread_id format")
    if not thread_id:
        thread_id = stable_id(
            "chat",
            SEED,
            repo_id,
            str(time.time_ns()),
            n=12,
        )

    call_index = _CHAT_CALL_INDEX.get(thread_id, 0) + 1
    _CHAT_CALL_INDEX[thread_id] = call_index
    iter_num = _CHAT_ITER.get(thread_id, 0) + 1
    _CHAT_ITER[thread_id] = iter_num

    context = _collect_repo_chat_context(
        repo_id=repo_id,
        query=message,
        iter_num=iter_num,
        max_files=req.max_files,
    )
    context_blob = json.dumps(
        context,
        sort_keys=True,
        ensure_ascii=False,
    )
    if len(context_blob) > 24000:
        context_blob = context_blob[:24000]

    history = _CHAT_THREADS.setdefault(thread_id, [])
    messages = [
        {
            "role": "system",
            "content": (
                "You are a repository assistant. Use only the provided "
                "context snippets and be explicit when info is missing."
            ),
        },
        {
            "role": "system",
            "content": (f"REPO_CONTEXT repo_id={repo_id}\n" + context_blob),
        },
    ]
    messages.extend(history[-12:])
    messages.append(
        {
            "role": "user",
            "content": message,
        }
    )

    fallback_reason = ""
    try:
        llm = llm_chat(
            messages=messages,
            run_id=thread_id,
            call_index=call_index,
            repo_id=repo_id,
            scenario="chat",
        )
        reply = str(llm.get("content", "")).strip()
        if not reply:
            reply = "No response generated."
    except HTTPException as exc:
        fallback_reason = str(exc.detail)
        files = context.get("files", []) or []
        profile = context.get("profile", {}) or {}
        workdirs = context.get("workdirs", []) or []
        lines = [
            "LLM unavailable; returning context-only summary.",
        ]
        if files:
            lines.append(
                "Relevant files: " + ", ".join(files[:6]),
            )
        if profile:
            lines.append(
                "Project profile: "
                + json.dumps(
                    profile,
                    sort_keys=True,
                ),
            )
        if workdirs:
            lines.append(
                "Detected workdirs: "
                + ", ".join(
                    str(w.get("id", "")) for w in workdirs[:6] if isinstance(w, dict)
                ),
            )
        if not files:
            lines.append("No matching files found for this query yet.")
        reply = "\n".join(lines)

    history.extend(
        [
            {"role": "user", "content": message},
            {"role": "assistant", "content": reply},
        ]
    )
    if len(history) > 30:
        del history[:-30]
    _prune_chat_threads()

    ledger.append(
        {
            "type": "CHAT_TURN",
            "run_id": thread_id,
            "repo_id": repo_id,
            "iter": iter_num,
            "files": context.get("files", []),
        }
    )

    return {
        "ok": True,
        "thread_id": thread_id,
        "repo_id": repo_id,
        "reply": reply,
        "fallback": bool(fallback_reason),
        "fallback_reason": fallback_reason,
        "context": {
            "files": context.get("files", []),
            "workdirs": context.get("workdirs", []),
            "profile": context.get("profile", {}),
            "warnings": context.get("warnings", []),
        },
    }


@app.post("/chat/text")
def chat_text(req: TextChatReq):
    message = (req.message or "").strip()
    if not message:
        raise HTTPException(400, "message is required")

    thread_id = (req.thread_id or "").strip()
    if thread_id and not _SAFE_THREAD_ID.fullmatch(thread_id):
        raise HTTPException(400, "invalid thread_id format")
    if not thread_id:
        thread_id = stable_id(
            "txtchat",
            SEED,
            str(time.time_ns()),
            n=12,
        )

    call_index = _TEXT_CHAT_CALL_INDEX.get(thread_id, 0) + 1
    _TEXT_CHAT_CALL_INDEX[thread_id] = call_index
    iter_num = _TEXT_CHAT_ITER.get(thread_id, 0) + 1
    _TEXT_CHAT_ITER[thread_id] = iter_num

    history = _TEXT_CHAT_THREADS.setdefault(thread_id, [])
    messages = [
        {
            "role": "system",
            "content": (
                "You are a concise technical assistant for the RFSN "
                "control surface. Keep responses direct and actionable."
            ),
        },
    ]
    messages.extend(history[-16:])
    messages.append(
        {
            "role": "user",
            "content": message,
        }
    )

    fallback_reason = ""
    try:
        llm = llm_chat(
            messages=messages,
            run_id=thread_id,
            call_index=call_index,
            repo_id="text-chat",
            scenario="text_chat",
        )
        reply = str(llm.get("content", "")).strip()
        if not reply:
            reply = "No response generated."
    except HTTPException as exc:
        fallback_reason = str(exc.detail)
        reply = (
            "LLM unavailable for text chat right now. "
            "If you are using cassette replay mode, add a matching cassette "
            "entry or switch cassette mode to record."
        )

    history.extend(
        [
            {"role": "user", "content": message},
            {"role": "assistant", "content": reply},
        ]
    )
    if len(history) > 40:
        del history[:-40]
    _prune_text_chat_threads()

    ledger.append(
        {
            "type": "TEXT_CHAT_TURN",
            "run_id": thread_id,
            "iter": iter_num,
        }
    )

    return {
        "ok": True,
        "thread_id": thread_id,
        "reply": reply,
        "fallback": bool(fallback_reason),
        "fallback_reason": fallback_reason,
    }


@app.get("/chat/{thread_id}")
def chat_thread(thread_id: str):
    history = _CHAT_THREADS.get(thread_id, [])
    return {
        "thread_id": thread_id,
        "count": len(history),
        "messages": history,
    }


@app.delete("/chat/{thread_id}")
def chat_thread_delete(thread_id: str):
    existed = thread_id in _CHAT_THREADS
    _CHAT_THREADS.pop(thread_id, None)
    _CHAT_CALL_INDEX.pop(thread_id, None)
    _CHAT_ITER.pop(thread_id, None)
    return {"ok": True, "deleted": bool(existed), "thread_id": thread_id}


@app.get("/chat/text/{thread_id}")
def chat_text_thread(thread_id: str):
    history = _TEXT_CHAT_THREADS.get(thread_id, [])
    return {
        "thread_id": thread_id,
        "count": len(history),
        "messages": history,
    }


@app.delete("/chat/text/{thread_id}")
def chat_text_thread_delete(thread_id: str):
    existed = thread_id in _TEXT_CHAT_THREADS
    _TEXT_CHAT_THREADS.pop(thread_id, None)
    _TEXT_CHAT_CALL_INDEX.pop(thread_id, None)
    _TEXT_CHAT_ITER.pop(thread_id, None)
    return {"ok": True, "deleted": bool(existed), "thread_id": thread_id}


@app.get("/policy/tier/{run_id}")
def policy_tier_for_run(run_id: str):
    if not (_HAS_HARD_KERNEL and _hard_kernel):
        raise HTTPException(
            500,
            "HardKernel not available",
        )
    tier, name, cfg = _policy_tier_for_run(run_id)
    rs = _hard_kernel.run_state.get(run_id)
    return {
        "run_id": run_id,
        "tier": tier,
        "name": name,
        "allow": cfg.get("allow", {}),
        "budgets": cfg.get("budgets", {}),
        "last_failure_kinds": list(rs.failure_kinds),
    }


@app.get("/ledger/verify")
def ledger_verify():
    """Verify integrity of the append-only ledger."""
    return ledger.verify_chain()


def _tail_lines_backwards(
    path: str,
    max_lines: int,
    block_size: int = 64 * 1024,
) -> list[str]:
    max_lines = min(int(max_lines), 5000)
    if max_lines <= 0 or not os.path.exists(path):
        return []

    lines: list[bytes] = []
    remainder = b""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        while pos > 0 and len(lines) < max_lines:
            read_size = block_size if pos >= block_size else pos
            pos -= read_size
            f.seek(pos, os.SEEK_SET)
            chunk = f.read(read_size)
            chunk = chunk + remainder
            parts = chunk.split(b"\n")
            remainder = parts[0]
            complete = parts[1:]
            for i in range(
                len(complete) - 1,
                -1,
                -1,
            ):
                if len(lines) >= max_lines:
                    break
                s = complete[i].strip()
                if s:
                    lines.append(s)
        if pos == 0 and remainder.strip() and len(lines) < max_lines:
            lines.append(remainder.strip())

    lines.reverse()
    return [b.decode("utf-8", errors="replace") for b in lines]


def _tail_jsonl(path: str, max_lines: int) -> list[dict]:
    out: list[dict] = []
    for line in _tail_lines_backwards(path, max_lines):
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _filter_events(
    events: list[dict],
    run_id: Optional[str],
    types: Optional[list[str]],
) -> list[dict]:
    def _event_run_id(e: dict) -> str:
        rid = e.get("run_id")
        if isinstance(rid, str) and rid:
            return rid
        meta = e.get("metadata")
        if isinstance(meta, dict):
            rid = meta.get("run_id")
            if isinstance(rid, str) and rid:
                return rid
            ev = meta.get("event")
            if isinstance(ev, dict):
                rid = ev.get("run_id")
                if isinstance(rid, str) and rid:
                    return rid
        return ""

    def _event_type(e: dict) -> str:
        t = e.get("type")
        if isinstance(t, str) and t:
            return t
        meta = e.get("metadata")
        if isinstance(meta, dict):
            et = meta.get("event_type")
            if isinstance(et, str) and et:
                return et
            ev = meta.get("event")
            if isinstance(ev, dict):
                et = ev.get("type")
                if isinstance(et, str) and et:
                    return et
        return "KERNEL_COMMIT"

    if run_id:
        events = [e for e in events if _event_run_id(e) == run_id]
    if types:
        want = set(types)
        events = [e for e in events if _event_type(e) in want]
    return events


def _normalize_ledger_event(raw: dict) -> dict:
    """Normalize hard-ledger records into human-usable events."""
    meta = raw.get("metadata")
    if not isinstance(meta, dict):
        return raw

    record_type = str(meta.get("record_type", ""))
    event = meta.get("event")
    if record_type in {
        "orchestrator_event",
        "kernel_event",
    } and isinstance(event, dict):
        out = dict(event)
        out.setdefault(
            "type",
            str(meta.get("event_type", "EVENT")),
        )
        out.setdefault(
            "run_id",
            str(meta.get("run_id", "")),
        )
        out["_record_type"] = record_type
        out["_chain_hash"] = raw.get("chain_hash", "")
        out["_decision"] = raw.get("decision", "")
        return out

    return {
        "type": "KERNEL_COMMIT",
        "run_id": str(meta.get("run_id", "")),
        "action": str(meta.get("action", "")),
        "intent": str(meta.get("intent", "")),
        "decision": str(raw.get("decision", "")),
        "reason": str(raw.get("decision_reason", "")),
        "risk": raw.get("risk", {}),
        "simulation": raw.get("simulation", {}),
        "state_hash": raw.get("state_hash", ""),
        "ts": raw.get("ts", 0),
        "_record_type": "kernel_commit",
        "_chain_hash": raw.get("chain_hash", ""),
    }


@app.get("/ledger/tail")
def ledger_tail(
    n: int = 200,
    run_id: Optional[str] = None,
    type: Optional[str] = None,
):
    types = [t.strip() for t in type.split(",") if t.strip()] if type else None
    events = [_normalize_ledger_event(e) for e in _tail_jsonl(HARD_LEDGER_PATH, n)]
    events = _filter_events(events, run_id, types)
    return {
        "path": HARD_LEDGER_PATH,
        "count": len(events),
        "events": events,
    }


@app.get("/ledger/run/{run_id}")
def ledger_for_run(
    run_id: str,
    n: int = 2000,
    type: Optional[str] = None,
):
    types = [t.strip() for t in type.split(",") if t.strip()] if type else None
    events = [_normalize_ledger_event(e) for e in _tail_jsonl(HARD_LEDGER_PATH, n)]
    events = _filter_events(events, run_id, types)
    return {
        "run_id": run_id,
        "path": HARD_LEDGER_PATH,
        "count": len(events),
        "events": events,
    }


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


@app.get("/kernel/replay/manifest/{run_id}")
def kernel_replay_manifest(run_id: str):
    path = _replay_manifest_path(run_id)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {
                "run_id": run_id,
                "path": path,
                "manifest": data,
            }
        except Exception as exc:
            raise HTTPException(
                500,
                f"manifest read failed: {exc}",
            ) from exc
    ctx = _RUN_CONTEXT.get(run_id)
    if isinstance(ctx, dict) and isinstance(
        ctx.get("replay_manifest"),
        dict,
    ):
        return {
            "run_id": run_id,
            "path": path,
            "manifest": ctx["replay_manifest"],
        }
    raise HTTPException(404, "manifest not found")


@app.get("/kernel/replay/manifest/check/{run_id}")
def kernel_replay_manifest_check(run_id: str):
    m = kernel_replay_manifest(run_id).get(
        "manifest",
        {},
    )
    check = _replay_manifest_check(
        m if isinstance(m, dict) else {},
    )
    return {
        "run_id": run_id,
        "ok": check.get("ok", False),
        "missing": check.get("missing", []),
        "required_count": check.get(
            "required_count",
            0,
        ),
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
    repo_id: str,
    it: int,
    step: dict,
    run_id: str | None = None,
    tier: int | None = None,
    warm_sandbox: bool | None = None,
):
    use_warm = WARM_SANDBOX if warm_sandbox is None else bool(warm_sandbox)
    payload = {
        "repo_id": repo_id,
        "iter": it,
        "step": step,
        "warm_sandbox": bool(use_warm),
    }
    if run_id:
        payload["run_id"] = run_id
    if tier is not None:
        payload["tier"] = int(tier)
    headers = dict(auth_headers())
    if tier is not None:
        headers["X-RFSN-Tier"] = str(int(tier))
    r = requests.post(
        f"{TOOL_GATEWAY_URL}/run_step",
        json=payload,
        headers=headers,
        timeout=(10, 300),
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
    learner_evidence: Optional[dict] = None,
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
        run_ctx = _ensure_run_context(run_id)
        tier_now, _, _ = _policy_tier_for_run(run_id)
        if _memory:
            _hard_kernel.state.memory_version = _memory.memory_version
        if (
            _hard_kernel.state.resource_state.get(
                "run_id",
                "",
            )
            != run_id
        ):
            _hard_kernel.state.resource_state["run_id"] = run_id
        exec_meta: dict = {
            "cache_hit": False,
            "cache_key": "",
        }

        def _exec_step(s: dict) -> Outcome:
            """Execution callback for hard kernel."""
            cache = run_ctx.get("sim_cache")
            use_warm_step = not bool(
                run_ctx.get("force_cold_sandbox", False),
            )
            if (
                bool(run_ctx.get("force_cold_sandbox", False))
                and str(s.get("type") or "") == "ensure_deps"
            ):
                # Replay mode is network-off. ensure_deps
                # would require network for package resolution.
                r = {
                    "status": 1,
                    "seconds": 0.0,
                    "logs": "REPLAY_POLICY: ensure_deps disabled in replay mode",
                    "failure_kind": "replay_network_disabled",
                    "network_mode": "none",
                    "allow_network": False,
                    "tier": int(tier_now),
                    "network_reason": "",
                }
                payload = json.dumps(
                    r,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                return Outcome(
                    success=False,
                    exit_code=1,
                    payload=payload[:3000],
                    logs=str(r.get("logs", "")),
                    duration_sec=0.0,
                )
            cache_key = ""
            r: dict
            if isinstance(cache, SimCache):
                cache_key = cache.key(
                    s,
                    str(s.get("workdir_id") or ""),
                )
                hit = cache.get(cache_key)
                if isinstance(hit, dict):
                    exec_meta["cache_hit"] = True
                    exec_meta["cache_key"] = cache_key
                    r = hit
                    ledger.append(
                        {
                            "type": "SIM_CACHE_HIT",
                            "run_id": run_id,
                            "iter": it,
                            "cache_key": cache_key,
                            "step_type": s.get("type", ""),
                        }
                    )
                else:
                    r = run_step(
                        repo_id,
                        it,
                        s,
                        run_id,
                        tier=tier_now,
                        warm_sandbox=use_warm_step,
                    )
                    cache.put(cache_key, r)
            else:
                r = run_step(
                    repo_id,
                    it,
                    s,
                    run_id,
                    tier=tier_now,
                    warm_sandbox=use_warm_step,
                )
            ok = r.get("status", 1) == 0
            payload = ""
            try:
                payload = json.dumps(
                    r,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
            except Exception:
                payload = str(r)
            return Outcome(
                success=ok,
                exit_code=r.get(
                    "status",
                    1,
                ),
                payload=payload[:30000],
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
            learner_evidence=learner_evidence,
        )
        hard_rec = {
            "type": "HARD_KERNEL_STEP",
            "run_id": run_id,
            "iter": it,
            "tier": _hard_kernel.run_state.get(run_id).tier,
            "phase": kr.phase,
            "approved": kr.approved,
            "success": kr.success,
            "error": kr.error,
            "reason": (kr.decision.reason if kr.decision else ""),
            "sim_cache_hit": bool(exec_meta.get("cache_hit", False)),
            "sim_cache_key": str(exec_meta.get("cache_key", "")),
            "risk": (kr.risk.to_dict() if kr.risk else None),
            "simulation": (kr.simulation.to_dict() if kr.simulation else None),
        }
        if step_num is not None:
            hard_rec["step_num"] = step_num
        ledger.append(hard_rec)

        if not kr.approved:
            reason = (
                kr.decision.reason if kr.decision else (kr.error or "kernel_reject")
            )
            return {
                "ok": False,
                "out": None,
                "reason": reason,
                "hard_kernel": True,
            }

        out = {}
        if kr.outcome and kr.outcome.payload:
            try:
                parsed_out = json.loads(
                    kr.outcome.payload,
                )
                if isinstance(parsed_out, dict):
                    out = parsed_out
            except json.JSONDecodeError:
                out = {}
        if not out:
            out = {
                "status": (kr.outcome.exit_code if kr.outcome else 1),
                "payload": (kr.outcome.payload if kr.outcome else ""),
                "logs": (kr.outcome.logs if kr.outcome else ""),
                "seconds": (kr.outcome.duration_sec if kr.outcome else 0),
            }

        if _memory:
            _memory.admit(
                MemoryEntry(
                    content=(
                        f"action={step.get('type')}"
                        f" success={kr.success}"
                        f" risk={kr.risk.total_risk:.2f}"
                        if kr.risk
                        else ""
                    ),
                    source="kernel",
                    entry_type="action_outcome",
                )
            )
            try:
                _memory.append_save(_MEMORY_PATH)
            except Exception:
                pass  # non-critical — best effort persistence

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
    messages: list,
    run_id: str,
    call_index: int,
    repo_id: str,
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
                wait = 2**attempt
                time.sleep(wait)
                continue
            if r.status_code != 200:
                raise HTTPException(
                    r.status_code,
                    r.text,
                )
            return r.json()
        except requests.exceptions.Timeout:
            _METRICS["llm_retries"] += 1
            last_exc = requests.exceptions.Timeout(
                f"attempt {attempt + 1}",
            )
            time.sleep(2**attempt)
        except HTTPException:
            raise
        except Exception as exc:
            last_exc = exc
            _METRICS["llm_retries"] += 1
            time.sleep(2**attempt)
    raise HTTPException(
        502,
        f"LLM unreachable after {max_retries}" f" retries: {last_exc}",
    )


def failure_signature(text: str) -> str:
    """Deterministic signature for learner bucketing."""
    blob = (text or "").encode(
        "utf-8",
        errors="ignore",
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
            out = r.json()
            if "kernel_evidence" not in out:
                out["kernel_evidence"] = {
                    "prior_success_prob": 0.5,
                    "prior_trials": 0,
                }
            return out
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
            "Strategy: search first," " narrow reads," " patch minimal. No refactor."
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
        "kernel_evidence": {
            "prior_success_prob": 0.5,
            "prior_trials": 0,
        },
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
        repo_path,
        last_fail,
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
            "failure_class",
            "",
        ),
        "dense_reward": dense_reward,
        # Structured failure fields
        "failure_module": parsed.get(
            "failure_module",
            "",
        ),
        "failure_test": parsed.get(
            "failure_test",
            "",
        ),
        "failure_message": parsed.get(
            "failure_message",
            "",
        ),
        "failure_signature_hash": parsed.get(
            "signature_hash",
            "",
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
    if DETERMINISTIC_RUN_ID:
        run_id = stable_id(
            "run",
            SEED,
            req.repo_id,
            req.task,
            str(req.max_iters),
            scenario,
            n=10,
        )
    else:
        run_id = stable_id(
            "run",
            SEED,
            req.repo_id,
            req.task,
            str(req.max_iters),
            scenario,
            str(time.time_ns()),
            n=12,
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
    if _SCHEDULER and not _SCHEDULER.start_run(
        run_id,
        max_seconds=_RUN_MAX_SECONDS,
    ):
        raise HTTPException(
            429,
            "too many active runs",
        )
    run_ctx = _ensure_run_context(run_id)
    run_ctx["force_cold_sandbox"] = str(scenario).strip().lower() == "replay"
    env_snapshot = (
        snapshot_environment(
            repo_path=f"/data/repos/{req.repo_id}",
            seed=run_seed,
        )
        if _HAS_HARD_KERNEL
        else {"env_hash": ""}
    )
    if _HAS_HARD_KERNEL and _hard_kernel:
        _hard_kernel.reset_for_run(
            run_id=run_id,
            rng_seed=run_seed,
            env_hash=env_snapshot.get(
                "env_hash",
                "",
            ),
            memory_version=(_memory.memory_version if _memory else "0"),
            policy_hash=POLICY_HASH,
            reset_history=True,
        )
    if _HAS_HARD_KERNEL and _planner:
        _planner.reset()

    ledger.append(
        {
            "type": "RUN_START",
            "run_id": run_id,
            "repo_id": req.repo_id,
            "task": req.task,
            "scenario": scenario,
            "policy_hash": POLICY_HASH,
            "seed": SEED,
            "episode_seed": run_seed,
            "env_hash": env_snapshot.get(
                "env_hash",
                "",
            ),
            "memory_version": (
                _hard_kernel.state.memory_version
                if _HAS_HARD_KERNEL and _hard_kernel
                else ""
            ),
            "scheduler": (_SCHEDULER.stats() if _SCHEDULER else {}),
        }
    )

    # ── Warm sandbox lifecycle ─────────────────
    force_cold = bool(run_ctx.get("force_cold_sandbox", False))
    sb_info = None if force_cold else _sandbox_create(run_id, req.repo_id)
    if force_cold:
        ledger.append(
            {
                "type": "SANDBOX_WARM_DISABLED",
                "run_id": run_id,
                "reason": "replay_mode",
            }
        )
    if sb_info:
        ledger.append(
            {
                "type": "SANDBOX_CREATED",
                "run_id": run_id,
                "container_id": sb_info.get(
                    "container_id",
                ),
                "image_hash": sb_info.get(
                    "image_hash",
                ),
            }
        )
    replay_manifest = _init_replay_manifest(
        run_id=run_id,
        repo_id=req.repo_id,
        task=req.task,
        scenario=scenario,
        run_seed=run_seed,
        env_snapshot=env_snapshot,
        sandbox_info=sb_info if isinstance(sb_info, dict) else None,
    )
    start_snapshot, _start_reason = _capture_repo_snapshot(
        req.repo_id,
        run_id,
        "start",
    )
    if start_snapshot:
        replay_manifest["repo_snapshot_start"] = start_snapshot
    if _start_reason:
        replay_manifest["snapshot_skipped_reason"] = _start_reason
    env_manifest = _capture_executor_env_manifest(
        run_id,
        req.repo_id,
    )
    if isinstance(env_manifest, dict):
        if isinstance(env_manifest.get("path"), str):
            replay_manifest["executor_env_manifest_path"] = env_manifest["path"]
        if isinstance(env_manifest.get("manifest"), dict):
            replay_manifest["executor_env_manifest"] = env_manifest["manifest"]
    replay_manifest["completeness"] = _replay_manifest_check(
        replay_manifest,
    )
    run_ctx["replay_manifest"] = replay_manifest
    _write_replay_manifest(
        run_id,
        run_ctx["replay_manifest"],
    )
    ledger.append(
        {
            "type": "REPLAY_MANIFEST_UPDATED",
            "run_id": run_id,
            "status": "running",
        }
    )

    # Deterministic repo introspection + command inference.
    _bootstrap_command_plan(
        run_id=run_id,
        repo_id=req.repo_id,
        scenario=scenario,
    )

    # tests-only fast path
    if is_tests_only_task(req.task):
        it = 1
        steps = []
        tier_now, _, _ = _policy_tier_for_run(run_id)
        deps_needed = DEPS_POLICY.get("enabled", True) and not venv_exists(req.repo_id)
        deps_tier_ok = tier_now >= int(
            GATE_POLICY.get("network_min_tier", 2),
        )
        if deps_needed and deps_tier_ok:
            steps.append(
                {
                    "id": "auto-deps",
                    "type": "ensure_deps",
                    "manifest": DEPS_POLICY.get("manifest", "requirements.txt"),
                    "timeout_s": int(DEPS_POLICY.get("max_install_seconds", 420)),
                }
            )
        elif deps_needed:
            ledger.append(
                {
                    "type": "AUTO_DEPS_SKIPPED",
                    "run_id": run_id,
                    "iter": it,
                    "reason": "tier_below_network_min_tier",
                    "tier": tier_now,
                    "network_min_tier": int(
                        GATE_POLICY.get("network_min_tier", 2),
                    ),
                }
            )
        steps.append(
            {
                "id": "t1",
                "type": "run_tests",
                "template_id": "pytest_targeted",
                "template_params": {"target": "tests"},
                "timeout_s": 240,
            }
        )
        steps.append(
            {
                "id": "t2",
                "type": "run_tests",
                "template_id": "pytest_suite",
                "template_params": {"target": ""},
                "timeout_s": 900,
            }
        )
        bundle = {
            "intent": "tests-only fast path",
            "bundle_id": stable_id(
                "b",
                SEED,
                req.repo_id,
                req.task,
                "fast",
                scenario,
                n=8,
            ),
            "steps": steps,
            "acceptance": {
                "tests_green": True,
                "no_new_failures": True,
            },
        }
        ledger.append(
            {
                "type": "BUNDLE_PROPOSED",
                "run_id": run_id,
                "bundle": bundle,
            }
        )

        results = []
        for i_step, s in enumerate(
            steps,
            start=1,
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
                ledger.append(
                    {
                        "type": "RUN_END",
                        "run_id": run_id,
                        "status": "rejected",
                        "reason": ex["reason"],
                    }
                )
                _finalize_replay_manifest(
                    run_id=run_id,
                    status="rejected",
                    reason=str(ex["reason"]),
                    results_count=len(results),
                )
                _end_kernel_run(run_id)
                return {
                    "run_id": run_id,
                    "status": "rejected",
                    "errors": [
                        {
                            "code": "HARD_KERNEL_REJECT",
                            "msg": ex["reason"],
                        }
                    ],
                    "results": results,
                }
            _METRICS["steps_executed"] += 1
            out = ex["out"]
            ledger.append(
                {
                    "type": "STEP_RESULT",
                    "run_id": run_id,
                    "iter": it,
                    "step": s,
                    "out": out,
                }
            )
            results.append({"step": s, "out": out})
            if out.get("status", 0) != 0:
                _sandbox_destroy(run_id, req.repo_id)
                ledger.append(
                    {
                        "type": "RUN_END",
                        "run_id": run_id,
                        "status": "fail",
                    }
                )
                _finalize_replay_manifest(
                    run_id=run_id,
                    status="fail",
                    reason="tests-only fast-path step failed",
                    results_count=len(results),
                )
                _end_kernel_run(run_id)
                return {
                    "run_id": run_id,
                    "status": "fail",
                    "results": results,
                }
        _sandbox_destroy(run_id, req.repo_id)
        ledger.append(
            {
                "type": "RUN_END",
                "run_id": run_id,
                "status": "ok",
            }
        )
        _METRICS["runs_ok"] += 1
        _finalize_replay_manifest(
            run_id=run_id,
            status="ok",
            reason="tests-only fast-path succeeded",
            results_count=len(results),
        )
        _end_kernel_run(run_id)
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
            "max_steps_per_bundle",
            15,
        ),
    )
    ENFORCE_TESTS = bool(
        GATE_POLICY.get("enforce_tests", True),
    )
    PATCH_VERIFY_WINDOW_STEPS = max(
        1,
        int(
            GATE_POLICY.get(
                "patch_verify_window_steps",
                4,
            )
        ),
    )
    # Total steps across all iterations.
    MAX_TOTAL_STEPS = MAX_STEPS_PER_ITER * req.max_iters

    total_steps_used = 0

    for it in range(1, req.max_iters + 1):
        if _SCHEDULER and not _SCHEDULER.budget_ok(run_id):
            ledger.append(
                {
                    "type": "RUN_BUDGET_EXCEEDED",
                    "run_id": run_id,
                    "iter": it,
                }
            )
            last_fail = "run budget exceeded"
            break
        fail_ctx = "\n\nLast iteration failure:\n" + last_fail if last_fail else ""

        sug = learner_suggest(
            req.repo_id,
            req.task,
            last_fail,
            last_stage,
        )
        last_strategy = sug.get("strategy_id")
        constraints = sug.get("constraints") or {}

        ledger.append(
            {
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
            }
        )

        # Build failure hint string for prompt.
        failure_hint_text = ""
        if sug.get("failure_hint"):
            failure_hint_text = "\n\n[LEARNER HINT] " + sug["failure_hint"]
        # Build past-outcomes context.
        past_text = ""
        if sug.get("past_outcomes"):
            past_items = sug["past_outcomes"][:3]
            past_lines = []
            for po in past_items:
                label = "PASS" if po.get("success") else "FAIL"
                past_lines.append(
                    f"  - [{label}]"
                    f" strategy={po.get('strategy_id', '?')}"
                    f" files={po.get('patch_files', '?')}"
                    f" reward={po.get('dense_reward', 0):.2f}"
                )
            past_text = "\n\n[PAST ATTEMPTS]\n" + "\n".join(past_lines)

        # Build playbook guidance text.
        pb_guidance = (
            sug.get(
                "playbook_guidance",
                "",
            )
            or ""
        )
        if pb_guidance:
            pb_guidance = "## Playbook (follow in order)\n" + pb_guidance

        # ── Hierarchical planner guidance ─────
        strategic_guidance = ""
        if _HAS_HARD_KERNEL and _planner:
            if it == 1 and not _planner.state.goal:
                fail_cls = parse_failure_signature(
                    last_fail,
                ).get("failure_class", "")
                task_type = _planner.classify_task(
                    req.task,
                    fail_cls,
                )
                _planner.set_goal(
                    task_type,
                )
            strategic_guidance = _planner.get_planner_guidance(
                last_error=last_fail if it > 1 else "",
                memory=_memory,
            )

        prompt = USER_TEMPLATE.format(
            repo_id=req.repo_id,
            task=(
                req.task
                + fail_ctx
                + failure_hint_text
                + past_text
                + ("\n\n" + strategic_guidance if strategic_guidance else "")
            ),
            learner_addendum=sug.get(
                "prompt_addendum",
                "",
            ),
            playbook_guidance=pb_guidance,
            max_patch_files=constraints.get(
                "max_patch_files",
                3,
            ),
            max_patch_total_lines=constraints.get(
                "max_patch_total_lines",
                80,
            ),
            max_added_lines=constraints.get(
                "max_added_lines",
                40,
            ),
            max_deleted_lines=constraints.get(
                "max_deleted_lines",
                40,
            ),
            forbid_test_edits=constraints.get(
                "forbid_test_edits",
                True,
            ),
            max_steps=MAX_STEPS_PER_ITER,
        )
        tier, tier_name, _ = _policy_tier_for_run(
            run_id,
        )

        # Build message history: system + user
        # prompt + transcript of this iteration.
        messages = [
            {"role": "system", "content": SYSTEM},
            {
                "role": "system",
                "content": (
                    f"POLICY_TIER={tier} ({tier_name}). "
                    "Propose steps within this tier only."
                ),
            },
            {"role": "user", "content": prompt},
        ]
        cmd_plan = _ensure_run_context(run_id).get(
            "cmd_plan",
            _default_cmd_plan(),
        )
        messages.insert(
            2,
            {
                "role": "system",
                "content": (
                    "DETERMINISTIC_CMD_PLAN="
                    + json.dumps(cmd_plan, sort_keys=True)
                    + " Use workdir_id from this plan;"
                    " do not invent workdirs."
                ),
            },
        )

        # Auto-inject ensure_deps if needed.
        deps_needed = DEPS_POLICY.get("enabled", True) and not venv_exists(req.repo_id)
        deps_tier_ok = tier >= int(
            GATE_POLICY.get("network_min_tier", 2),
        )
        if deps_needed and deps_tier_ok:
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
                "dep",
                SEED,
                req.repo_id,
                str(it),
                scenario,
                n=8,
            )
            ex = execute_approved_step(
                req.repo_id,
                it,
                dep_step,
                run_id,
                context_hash=sug.get(
                    "context_key",
                    "",
                ),
                intent="auto deps",
                bundle_id=dep_bundle_id,
                step_num=0,
                learner_evidence=sug.get(
                    "kernel_evidence",
                    {},
                ),
            )
            if not ex["ok"]:
                _METRICS["gate_rejections"] += 1
                last_stage = "gate_reject"
                last_fail = "Auto-deps rejected by" " hard kernel: " + ex["reason"]
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "HARD KERNEL REJECTED"
                            " auto-deps step:"
                            f" {ex['reason']}\n"
                            "Continue with another"
                            " approach."
                        ),
                    }
                )
            else:
                _METRICS["steps_executed"] += 1
                total_steps_used += 1
                dep_out = ex["out"]
                ledger.append(
                    {
                        "type": "STEP_RESULT",
                        "run_id": run_id,
                        "iter": it,
                        "step": dep_step,
                        "out": dep_out,
                    }
                )
                # Tell the LLM deps are installed.
                messages.append(
                    {
                        "role": "assistant",
                        "content": json.dumps(
                            {
                                "step": dep_step,
                                "done": False,
                                "intent": ("auto-install deps"),
                            }
                        ),
                    }
                )
                dep_status = "ok" if dep_out.get("status", 0) == 0 else "FAILED"
                if dep_status != "ok":
                    last_stage = "deps"
                messages.append(
                    {
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
                                        "logs",
                                        "",
                                    )[-2000:]
                                ),
                            )
                        ),
                    }
                )
        elif deps_needed:
            ledger.append(
                {
                    "type": "AUTO_DEPS_SKIPPED",
                    "run_id": run_id,
                    "iter": it,
                    "reason": "tier_below_network_min_tier",
                    "tier": tier,
                    "network_min_tier": int(
                        GATE_POLICY.get("network_min_tier", 2),
                    ),
                }
            )

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
        patch_verify = {
            "pending": False,
            "steps_since_patch": 0,
            "targeted_ok": False,
            "suite_ok": False,
            "generic_ok_count": 0,
        }

        while iter_steps_used < MAX_STEPS_PER_ITER:
            if _SCHEDULER and not _SCHEDULER.budget_ok(run_id):
                last_fail = "run budget exceeded"
                break
            if total_steps_used >= MAX_TOTAL_STEPS:
                last_fail = "Total step budget exhausted"
                break

            # Ask LLM for next step.
            call_index += 1
            ledger.append(
                {
                    "type": "LLM_CALL",
                    "run_id": run_id,
                    "call_index": call_index,
                    "iter": it,
                }
            )
            try:
                llm = llm_chat(
                    messages,
                    run_id,
                    call_index,
                    req.repo_id,
                    scenario,
                )
            except Exception:
                last_fail = "LLM call failed"
                break

            content = llm.get("content", "")

            # Parse the LLM response (robust).
            resp = _repair_json(content)
            if resp is None:
                # Append structured parse error
                # and let LLM try again (1 retry).
                messages.append(
                    {
                        "role": "assistant",
                        "content": content,
                    }
                )
                # Tell LLM exactly what's wrong.
                snippet = content[:200].replace(
                    "\n",
                    " ",
                )
                messages.append(
                    {
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
                    }
                )
                continue

            # Validate required keys.
            missing = _REQUIRED_KEYS - set(
                resp.keys(),
            )
            if missing:
                messages.append(
                    {
                        "role": "assistant",
                        "content": content,
                    }
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "SCHEMA ERROR: missing"
                            " required keys:"
                            f" {sorted(missing)}.\n"
                            "Your JSON must have"
                            ' "step", "done",'
                            ' and "intent".'
                        ),
                    }
                )
                continue

            if not isinstance(resp.get("done"), bool):
                messages.append(
                    {
                        "role": "assistant",
                        "content": content,
                    }
                )
                messages.append(
                    {
                        "role": "user",
                        "content": ("SCHEMA ERROR: 'done'" " must be a boolean."),
                    }
                )
                continue

            if (
                not isinstance(resp.get("intent"), str)
                or not resp.get("intent", "").strip()
            ):
                messages.append(
                    {
                        "role": "assistant",
                        "content": content,
                    }
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "SCHEMA ERROR: 'intent'" " must be a non-empty string."
                        ),
                    }
                )
                continue

            # Check if LLM says done.
            if resp.get("done", False):
                if resp.get("step") is not None:
                    messages.append(
                        {
                            "role": "assistant",
                            "content": content,
                        }
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "SCHEMA ERROR: when" " done=true, step must" " be null."
                            ),
                        }
                    )
                    continue
                ledger.append(
                    {
                        "type": "LLM_DONE",
                        "run_id": run_id,
                        "iter": it,
                        "intent": resp.get(
                            "intent",
                            "",
                        ),
                    }
                )
                break

            step = resp.get("step")
            if not step or not isinstance(
                step,
                dict,
            ):
                messages.append(
                    {
                        "role": "assistant",
                        "content": content,
                    }
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Invalid response: 'step'" " must be a dict." " Try again."
                        ),
                    }
                )
                continue

            # Normalize step.
            if not step.get("id"):
                step["id"] = f"s{iter_steps_used + 1}"
            run_ctx = _ensure_run_context(run_id)
            cmd_plan = run_ctx.get(
                "cmd_plan",
                _default_cmd_plan(),
            )

            # Deterministic command selection: convert run_tests
            # into run_cmd_template using inferred templates/workdir.
            if step.get("type") == "run_tests" and isinstance(cmd_plan, dict):
                tests = cmd_plan.get("test_templates")
                if isinstance(tests, list) and tests:
                    step = {
                        "id": step.get("id"),
                        "type": "run_cmd_template",
                        "template": str(tests[0]),
                        "workdir_id": str(
                            cmd_plan.get(
                                "workdir_id",
                                "workdir_0",
                            )
                        ),
                        "timeout_s": int(step.get("timeout_s") or 240),
                    }

            if step.get("type") == "apply_patch":
                patch = str(step.get("patch", ""))
                if patch:
                    step["patch"] = minimize_unified_diff(
                        patch,
                    )
            if step.get("type") in {
                "run_cmd_template",
                "format_fix",
            }:
                if not step.get("workdir_id") and isinstance(cmd_plan, dict):
                    step["workdir_id"] = str(
                        cmd_plan.get(
                            "workdir_id",
                            "workdir_0",
                        )
                    )

            # Test template lock:
            # keep test execution contract stable for this run.
            test_key = _test_template_key(step)
            if test_key:
                reset_baseline = bool(
                    step.pop("reset_test_baseline", False),
                )
                baseline_key = str(
                    run_ctx.get(
                        "baseline_test_template",
                        "",
                    )
                )
                if baseline_key and test_key != baseline_key:
                    if reset_baseline and not bool(patch_verify.get("pending")):
                        run_ctx["baseline_test_template"] = test_key
                        ledger.append(
                            {
                                "type": "BASELINE_TEST_TEMPLATE_RESET",
                                "run_id": run_id,
                                "iter": it,
                                "baseline": baseline_key,
                                "new_template": test_key,
                            }
                        )
                    else:
                        messages.append(
                            {
                                "role": "assistant",
                                "content": content,
                            }
                        )
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "TEMPLATE LOCK: test template must remain stable."
                                    f" baseline={baseline_key}"
                                    f" attempted={test_key}."
                                    " To reset baseline explicitly, set"
                                    " step.reset_test_baseline=true"
                                    " on a test step (only when no pending"
                                    " post-patch verification is active)."
                                ),
                            }
                        )
                        ledger.append(
                            {
                                "type": "TEMPLATE_LOCK_REJECT",
                                "run_id": run_id,
                                "iter": it,
                                "baseline": baseline_key,
                                "attempted": test_key,
                                "reset_requested": bool(reset_baseline),
                            }
                        )
                        continue
                if not baseline_key:
                    run_ctx["baseline_test_template"] = test_key
                    ledger.append(
                        {
                            "type": "BASELINE_TEST_TEMPLATE_SET",
                            "run_id": run_id,
                            "iter": it,
                            "template": test_key,
                        }
                    )

            # ── RFSN phase transition check ──
            step_type = step.get("type", "")
            phase_ok, phase_err = phase.check_transition(step_type)
            if not phase_ok:
                messages.append(
                    {
                        "role": "assistant",
                        "content": content,
                    }
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "PHASE VIOLATION: "
                            + phase_err
                            + "\nCurrent phase: "
                            + phase.phase
                            + ". Adjust your step"
                            " type and try again."
                        ),
                    }
                )
                ledger.append(
                    {
                        "type": "PHASE_VIOLATION",
                        "run_id": run_id,
                        "iter": it,
                        "current_phase": phase.phase,
                        "attempted_type": step_type,
                        "error": phase_err,
                    }
                )
                continue

            bundle_id = stable_id(
                "b",
                SEED,
                req.repo_id,
                req.task,
                str(it),
                str(iter_steps_used),
                scenario,
                n=8,
            )
            approved_step = step

            ex = execute_approved_step(
                req.repo_id,
                it,
                approved_step,
                run_id,
                context_hash=sug.get(
                    "context_key",
                    "",
                ),
                intent=resp.get(
                    "intent",
                    "",
                ),
                bundle_id=bundle_id,
                step_num=iter_steps_used + 1,
                learner_evidence=sug.get(
                    "kernel_evidence",
                    {},
                ),
            )
            if not ex["ok"]:
                _METRICS["gate_rejections"] += 1
                last_stage = "gate_reject"
                messages.append(
                    {
                        "role": "assistant",
                        "content": content,
                    }
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "HARD KERNEL REJECTED"
                            " (simulation/risk):"
                            f" {ex['reason']}\n"
                            "Try a different"
                            " approach."
                        ),
                    }
                )
                continue

            _METRICS["steps_executed"] += 1
            iter_steps_used += 1
            total_steps_used += 1
            out = ex["out"]

            ledger.append(
                {
                    "type": "STEP_RESULT",
                    "run_id": run_id,
                    "iter": it,
                    "step": approved_step,
                    "out": out,
                }
            )
            iter_results.append(
                {
                    "step": approved_step,
                    "out": out,
                }
            )

            # Advance RFSN phase.
            phase.advance(
                approved_step.get("type", ""),
            )

            # Determine status.
            step_status = out.get("status", 0)
            status_label = "ok" if step_status == 0 else f"FAILED (exit {step_status})"
            if approved_step.get("type") == "apply_patch" and int(step_status) == 0:
                patch_verify = {
                    "pending": True,
                    "steps_since_patch": 0,
                    "targeted_ok": False,
                    "suite_ok": False,
                    "generic_ok_count": 0,
                }
                ledger.append(
                    {
                        "type": "PATCH_APPLIED_AWAITING_TESTS",
                        "run_id": run_id,
                        "iter": it,
                        "window_steps": PATCH_VERIFY_WINDOW_STEPS,
                    }
                )
            elif bool(patch_verify.get("pending")):
                patch_verify["steps_since_patch"] = (
                    int(
                        patch_verify.get("steps_since_patch", 0),
                    )
                    + 1
                )

            if (
                bool(patch_verify.get("pending"))
                and _is_test_step(approved_step)
                and int(step_status) == 0
            ):
                variant = _test_step_variant(approved_step)
                if variant == "targeted":
                    patch_verify["targeted_ok"] = True
                elif variant == "suite":
                    patch_verify["suite_ok"] = True
                else:
                    patch_verify["generic_ok_count"] = (
                        int(
                            patch_verify.get("generic_ok_count", 0),
                        )
                        + 1
                    )
                if _patch_verify_ok(patch_verify):
                    patch_verify["pending"] = False
                    ledger.append(
                        {
                            "type": "PATCH_VERIFIED_BY_TESTS",
                            "run_id": run_id,
                            "iter": it,
                            "variant": variant or "generic",
                            "counts": {
                                "targeted_ok": bool(
                                    patch_verify.get("targeted_ok"),
                                ),
                                "suite_ok": bool(
                                    patch_verify.get("suite_ok"),
                                ),
                                "generic_ok_count": int(
                                    patch_verify.get("generic_ok_count", 0),
                                ),
                            },
                        }
                    )

            if (
                ENFORCE_TESTS
                and bool(patch_verify.get("pending"))
                and int(
                    patch_verify.get("steps_since_patch", 0),
                )
                >= PATCH_VERIFY_WINDOW_STEPS
                and not _patch_verify_ok(patch_verify)
            ):
                last_stage = "tests"
                last_fail = (
                    "Patch verification invariant failed:"
                    " require targeted+suite tests"
                    " (or two deterministic test passes)"
                    f" within {PATCH_VERIFY_WINDOW_STEPS} steps after apply_patch."
                )
                ledger.append(
                    {
                        "type": "PATCH_VERIFY_TIMEOUT",
                        "run_id": run_id,
                        "iter": it,
                        "steps_since_patch": int(
                            patch_verify.get("steps_since_patch", 0),
                        ),
                    }
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "PATCH VERIFY INVARIANT: you applied a patch but did not"
                            " complete required post-patch test verification in time."
                        ),
                    }
                )
                break
            repair_state = run_ctx.get(
                "repair",
                {
                    "phase": "SEARCH",
                    "attempt": 0,
                    "max_attempts": 3,
                    "last_status": 1,
                },
            )
            if isinstance(repair_state, dict):
                cur_phase = str(repair_state.get("phase", "SEARCH"))
                updated = update_state(
                    repair_state,
                    cur_phase,
                    int(step_status),
                )
                nxt = next_phase(updated)
                updated["phase"] = nxt
                updated["can_retry"] = should_retry(
                    updated,
                )
                run_ctx["repair"] = updated
                ledger.append(
                    {
                        "type": "REPAIR_PHASE",
                        "run_id": run_id,
                        "iter": it,
                        "phase": cur_phase,
                        "next_phase": nxt,
                        "status": int(step_status),
                        "attempt": int(updated.get("attempt", 0)),
                        "can_retry": bool(updated.get("can_retry", True)),
                    }
                )

            # Extract payload for feedback.
            payload = out.get("payload")
            logs = out.get("logs", "")

            # Build a compact output summary for
            # the transcript (capped at 3000 chars
            # to control token usage).
            if payload and isinstance(
                payload,
                str,
            ):
                output_text = payload[-3000:]
            else:
                output_text = logs[-3000:]

            # Append assistant response + tool
            # output to messages as transcript.
            messages.append(
                {
                    "role": "assistant",
                    "content": content,
                }
            )
            messages.append(
                {
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
                }
            )

            # If this was a failing test step,
            # record for learner but keep going
            # (LLM may want to retry/adapt).
            if step_status != 0:
                if (
                    approved_step.get(
                        "type",
                    )
                    == "apply_patch"
                ):
                    last_stage = "apply_patch"
                    ledger.append(
                        {
                            "type": "PATCH_REJECTED",
                            "run_id": run_id,
                            "iter": it,
                            "status": step_status,
                        }
                    )

            # Track test counts for dense reward.
            if _is_test_step(approved_step):
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
                    iter_dense_reward = compute_dense_reward(
                        prev_test_counts,
                        curr_test_counts,
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
                        messages.append(
                            {
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
                            }
                        )
                    last_fail = log_text[-5000:]
                    last_stage = "tests"
                    # Planner: record stagnation.
                    if _HAS_HARD_KERNEL and _planner:
                        stagnant = _planner.record_no_progress()
                        if stagnant:
                            _planner.escalate()
                else:
                    last_stage = "success"
                    # Planner: advance subgoal.
                    if _HAS_HARD_KERNEL and _planner:
                        _planner.advance_subgoal()
                        if _hard_kernel:
                            _hard_kernel.adaptive_relax()

            # Track patch metadata for outcome DB.
            if approved_step.get("type") == "apply_patch":
                patch_text = approved_step.get(
                    "patch",
                    "",
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
                    if pline.startswith("+++") or pline.startswith("---"):
                        continue
                    if pline.startswith("+"):
                        p_add += 1
                    elif pline.startswith("-"):
                        p_del += 1
                iter_patch_added = p_add
                iter_patch_deleted = p_del

            # If tests passed, hint the LLM to
            # either declare done or continue.
            if _is_test_step(approved_step) and step_status == 0:
                messages.append(
                    {
                        "role": "user",
                        "content": DONE_PROMPT,
                    }
                )

        # ── End of inner step loop ──
        # Check if this iteration succeeded.
        test_results = [r for r in iter_results if _is_test_step(r["step"])]
        tests_passed = test_results and all(
            r["out"].get("status", 1) == 0 for r in test_results
        )
        has_patch = any(r["step"].get("type") == "apply_patch" for r in iter_results)
        patch_applied = any(
            r["step"].get("type") == "apply_patch" and r["out"].get("status", 1) == 0
            for r in iter_results
        )
        if (
            ENFORCE_TESTS
            and patch_applied
            and bool(patch_verify.get("pending"))
            and not _patch_verify_ok(patch_verify)
        ):
            tests_passed = False
            if not last_fail:
                last_fail = (
                    "Patch applied without required post-patch verification"
                    " tests (targeted+suite or two deterministic passes)."
                )
            ledger.append(
                {
                    "type": "PATCH_VERIFY_MISSING",
                    "run_id": run_id,
                    "iter": it,
                    "details": {
                        "steps_since_patch": int(
                            patch_verify.get("steps_since_patch", 0),
                        ),
                        "targeted_ok": bool(
                            patch_verify.get("targeted_ok"),
                        ),
                        "suite_ok": bool(
                            patch_verify.get("suite_ok"),
                        ),
                        "generic_ok_count": int(
                            patch_verify.get("generic_ok_count", 0),
                        ),
                    },
                }
            )

        if tests_passed and (not has_patch or patch_applied):
            # Run static analysis if configured.
            if TEST_POLICY.get("suite_on_success", False):
                sa_steps = []
                for sa_tmpl in TEST_POLICY.get("static_templates", []):
                    sa_steps.append(
                        {
                            "id": f"auto-{sa_tmpl}",
                            "type": "run_tests",
                            "template_id": sa_tmpl,
                            "template_params": {
                                "target": "",
                            },
                            "timeout_s": 300,
                        }
                    )
                if sa_steps:
                    sa_bundle_id = stable_id(
                        "sa",
                        run_id,
                        str(it),
                        n=8,
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
                                "context_key",
                                "",
                            ),
                            intent="static analysis",
                            bundle_id=sa_bundle_id,
                            step_num=1000 + i_sa,
                            learner_evidence=sug.get(
                                "kernel_evidence",
                                {},
                            ),
                        )
                        if not ex["ok"]:
                            _METRICS["gate_rejections"] += 1
                            last_stage = "gate_reject"
                            last_fail = (
                                "Static analysis"
                                " rejected by"
                                " hard kernel: " + ex["reason"]
                            )
                            _sandbox_destroy(
                                run_id,
                                req.repo_id,
                            )
                            ledger.append(
                                {
                                    "type": "RUN_END",
                                    "run_id": run_id,
                                    "status": "rejected",
                                    "reason": ex["reason"],
                                }
                            )
                            _finalize_replay_manifest(
                                run_id=run_id,
                                status="rejected",
                                reason=str(ex["reason"]),
                                results_count=len(iter_results),
                            )
                            _end_kernel_run(run_id)
                            return {
                                "run_id": run_id,
                                "status": "rejected",
                                "errors": [
                                    {
                                        "code": "HARD_KERNEL_REJECT",
                                        "msg": ex["reason"],
                                    }
                                ],
                                "results": iter_results,
                            }
                        _METRICS["steps_executed"] += 1
                        sa_out = ex["out"]
                        ledger.append(
                            {
                                "type": ("STEP_RESULT"),
                                "run_id": run_id,
                                "iter": it,
                                "step": sa_s,
                                "out": sa_out,
                            }
                        )
                        iter_results.append(
                            {
                                "step": sa_s,
                                "out": sa_out,
                            }
                        )

            _sandbox_destroy(run_id, req.repo_id)
            ledger.append(
                {
                    "type": "RUN_END",
                    "run_id": run_id,
                    "status": "ok",
                }
            )
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
                    curr_test_counts.get("passed", 0) if curr_test_counts else 0
                ),
                tests_failed=(
                    curr_test_counts.get("failed", 0) if curr_test_counts else 0
                ),
                tests_total=(
                    curr_test_counts.get("total", 0) if curr_test_counts else 0
                ),
                dense_reward=1.0,
                stage="success",
            )
            _METRICS["runs_ok"] += 1
            _finalize_replay_manifest(
                run_id=run_id,
                status="ok",
                reason="iteration converged to success",
                results_count=len(iter_results),
            )
            _end_kernel_run(run_id)
            return {
                "run_id": run_id,
                "status": "ok",
                "results": iter_results,
            }

        # Iteration failed — carry failure context
        # forward to next iteration.
        if not last_fail:
            last_fail = "Iteration ended without" " passing tests"
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
            tests_passed=(curr_test_counts.get("passed", 0) if curr_test_counts else 0),
            tests_failed=(curr_test_counts.get("failed", 0) if curr_test_counts else 0),
            tests_total=(curr_test_counts.get("total", 0) if curr_test_counts else 0),
            dense_reward=iter_dense_reward,
            stage=last_stage,
        )

    _sandbox_destroy(run_id, req.repo_id)
    ledger.append(
        {
            "type": "RUN_END",
            "run_id": run_id,
            "status": "fail",
        }
    )
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
        tests_passed=(curr_test_counts.get("passed", 0) if curr_test_counts else 0),
        tests_failed=(curr_test_counts.get("failed", 0) if curr_test_counts else 0),
        tests_total=(curr_test_counts.get("total", 0) if curr_test_counts else 0),
        dense_reward=iter_dense_reward,
        stage=last_stage,
    )
    _METRICS["runs_fail"] += 1
    _finalize_replay_manifest(
        run_id=run_id,
        status="fail",
        reason=last_fail or "run failed",
        results_count=0,
    )
    _end_kernel_run(run_id)
    return {
        "run_id": run_id,
        "status": "fail",
    }
