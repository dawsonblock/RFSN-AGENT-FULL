import json
import os
import re
import shlex
import subprocess
import tempfile
import time

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

app = FastAPI()
if _HAS_AUTH:
    app.add_middleware(
        ServiceAuthMiddleware  # type: ignore[possibly-unbound]
    )

BLESSED_IMAGE = os.getenv("BLESSED_IMAGE", "rfsn-blessed:0.2")
HOST_DATA_DIR = os.getenv("HOST_DATA_DIR", "/data")

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
            f"FATAL: Cannot load policy file {path}:"
            f" {exc}",
            flush=True,
        )
        raise SystemExit(1) from exc


DEPS = _load_yaml("/policies/deps_policy.yaml")
CMD_TEMPLATES = (
    _load_yaml("/policies/command_templates.yaml")
    .get("templates", {})
)

_SAFE_REPO_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class ExecReq(BaseModel):
    repo_id: str
    iter: int
    step: dict


@app.get("/health")
def health():
    pool_stats = (
        _sandbox_pool.stats()
        if _sandbox_pool else {"active": 0}
    )
    return {
        "ok": True,
        "image": BLESSED_IMAGE,
        "sandbox_pool": pool_stats,
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
            501, "sandbox pool not available",
        )
    _validate_repo_id(req.repo_id)
    repo_host, art_host, venv_host, wheels_host = (
        _paths(req.repo_id)
    )
    sb = _sandbox_pool.get_or_create(
        req.run_id, repo_host, art_host,
        venv_host, wheels_host, req.network,
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
        return run(ExecReq(
            repo_id=req.repo_id,
            iter=0,
            step=req.step,
        ))

    _validate_repo_id(req.repo_id)
    repo_host, art_host, venv_host, wheels_host = (
        _paths(req.repo_id)
    )

    sb = _sandbox_pool.get_or_create(
        req.run_id, repo_host, art_host,
        venv_host, wheels_host,
    )

    step = req.step
    t: str = step.get("type") or ""
    timeout_s = int(step.get("timeout_s") or 300)

    # Build script + data files for this step
    # (reuse the same logic as cold path).
    script, data_files = _build_step_script(
        t, step, req.repo_id,
    )

    out = _sandbox_pool.exec_in(
        sb, script, data_files, timeout_s,
    )

    # For apply_patch, add verification.
    if t == "apply_patch":
        out = _verify_patch_result(
            out, step, sb,
        )

    # Wrap in standard response format.
    payload = None
    if t in ("repo_search", "repo_read_range"):
        payload = out.get("logs", "").strip()
    return {
        "status": out["status"],
        "seconds": out["seconds"],
        "logs": out["logs"],
        "payload": payload,
    }


def _validate_repo_id(repo_id: str) -> None:
    if not _SAFE_REPO_ID.match(repo_id):
        raise HTTPException(
            400,
            "invalid repo_id: must match"
            f" {_SAFE_REPO_ID.pattern}",
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
    repo_host = os.path.join(HOST_DATA_DIR, "repos", repo_id)
    art_host = os.path.join(HOST_DATA_DIR, "artifacts", repo_id)
    venv_host = os.path.join(HOST_DATA_DIR, "venv", repo_id)
    wheels_host = os.path.join(HOST_DATA_DIR, "wheels", repo_id)
    return repo_host, art_host, venv_host, wheels_host


def _write_data_file(data: str, suffix: str = ".txt") -> str:
    """Write data to a temp file for mounting into container (no heredoc)."""
    fd = tempfile.NamedTemporaryFile(
        delete=False, suffix=suffix, mode="w", encoding="utf-8", dir="/tmp",
    )
    fd.write(data)
    fd.close()
    return fd.name


def _run_docker_with_data(
    script: str, data_files: dict,
    repo_host, art_host, venv_host, wheels_host,
    timeout_s: int, network_disabled: bool,
):
    """Run script in blessed container.

    Data passed via mounted files — NEVER heredocs.

    Security features:
    - --user 1000:1000 (non-root)
    - --no-new-privileges
    - --memory 2g / --cpus 2 / --pids-limit 256
    - --cap-drop ALL
    """
    script_path = _write_data_file(script, suffix=".sh")
    try:
        net = "none" if network_disabled else "bridge"
        extra_mounts = ["-v", f"{script_path}:/tmp/rfsn_script.sh:ro"]
        for cpath, hpath in data_files.items():
            extra_mounts.extend(["-v", f"{hpath}:{cpath}:ro"])

        args = [
            "docker", "run", "--rm",
            "--network", net,
            "--user", "1000:1000",
            "--no-new-privileges",
            "--memory", "2g",
            "--cpus", "2",
            "--pids-limit", "256",
            "--cap-drop", "ALL",
        ] + extra_mounts + [
            "-v", f"{repo_host}:/work/repo:rw",
            "-v", f"{art_host}:/work/artifacts:rw",
            "-v", f"{venv_host}:/work/venv:rw",
            "-v", f"{wheels_host}:/work/wheels:rw",
            "-w", "/work",
            BLESSED_IMAGE,
            "bash", "/tmp/rfsn_script.sh",
        ]

        start = time.time()
        try:
            p = subprocess.run(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout_s,
                text=True,
            )
            out = p.stdout.replace("\r\n", "\n")[-200000:]
            return {
                "status": p.returncode,
                "seconds": time.time() - start,
                "logs": out,
            }
        except subprocess.TimeoutExpired as e:
            raw = e.stdout or ""
            if isinstance(raw, bytes):
                raw = raw.decode(
                    "utf-8", errors="replace"
                )
            out = raw + "\n[TIMEOUT]\n"
            return {
                "status": 124,
                "seconds": time.time() - start,
                "logs": out.replace(
                    "\r\n", "\n"
                )[-200000:],
            }
        except FileNotFoundError:
            raise HTTPException(
                500,
                "docker not found inside executor",
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


def _ensure_deps(
    repo_id, repo_host, art_host,
    venv_host, wheels_host, timeout_s,
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
        script, {},
        repo_host, art_host,
        venv_host, wheels_host,
        timeout_s, network_disabled=False,
    )


def _repo_search(
    pattern, repo_host, art_host,
    venv_host, wheels_host, timeout_s,
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
        '; sys.exit(1)\n'
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
        repo_host, art_host,
        venv_host, wheels_host,
        timeout_s, network_disabled=True,
    )


def _repo_read_range(
    path, line_start, line_end,
    repo_host, art_host, venv_host,
    wheels_host, timeout_s,
):
    """SAFE: path & lines passed via mounted JSON, never in bash."""
    if path.startswith("/") or path.startswith("~") or ".." in path.split("/"):
        raise HTTPException(403, f"path rejected: {path}")
    config = json.dumps({
        "path": path,
        "start": line_start,
        "end": line_end,
    })
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
        repo_host, art_host,
        venv_host, wheels_host,
        timeout_s, network_disabled=True,
    )


def _apply_patch(
    patch, repo_host, art_host,
    venv_host, wheels_host, timeout_s,
):
    """SAFE: patch written to file, not heredoc."""
    if not patch.strip():
        return {
            "status": 1,
            "seconds": 0.0,
            "logs": "REJECTED: empty patch has no effect",
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
        repo_host, art_host,
        venv_host, wheels_host,
        timeout_s, network_disabled=True,
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
            "files_touched", 0,
        ) == 0
    ):
        out["status"] = 1
        out["logs"] += (
            "\nREJECTED: patch had no effect"
            " (diff is empty after apply)"
        )

    return out


def _run_tests(
    template_id, target, repo_host,
    art_host, venv_host, wheels_host,
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
                "target rejected by regex:"
                f" {target!r} !~ {allowed_re}",
            )
    elif target:
        raise HTTPException(
            403,
            f"target not allowed for template"
            f" {template_id}",
        )
    safe_target = shlex.quote(target) if target else ""
    cmd_str = " ".join([shlex.quote(x) for x in cmd])
    cmd_str = cmd_str.replace("{target}", safe_target)
    script = (
        "#!/bin/bash\nset -euo pipefail\n"
        'if [ ! -f /work/venv/bin/activate ]; then\n'
        '  echo "Missing venv; ensure_deps first"\n'
        '  exit 39\n'
        'fi\n'
        f"{cmd_str}\n"
    )
    return _run_docker_with_data(
        script, {},
        repo_host, art_host,
        venv_host, wheels_host,
        timeout_s, network_disabled=True,
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
        "#!/bin/bash\ncd /work/repo\n"
        "git diff --numstat 2>/dev/null || true\n"
    )
    stat_out = _sandbox_pool.exec_in(
        sandbox, stat_script, {}, 10,
    )
    meta = _parse_patch_stat(stat_out.get("logs", ""))
    out["patch_meta"] = meta

    if meta["files_touched"] == 0:
        out["status"] = 1
        out["logs"] += (
            "\nREJECTED: patch had no effect"
            " (diff is empty after apply)"
        )
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
            "manifest", "requirements.txt",
        )
        require_hashes = bool(
            DEPS.get("require_hashes", True),
        )
        only_binary = bool(
            DEPS.get("only_binary", True),
        )
        cache_dir = DEPS.get(
            "pip_cache_dir", "/work/wheels",
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
            f"  if ! grep -q -- \"--hash=\""
            f" {shlex.quote(manifest)}; then\n"
            "    echo \"Policy: requirements.txt"
            " must include --hash entries\"\n"
            "    exit 33\n  fi\nfi\n"
            "python -m venv /work/venv\n"
            ". /work/venv/bin/activate\n"
            "python -m pip install --upgrade pip\n"
            "BIN_FLAG=\"\"\n"
            f"if [ {ob} -eq 1 ]; then"
            " BIN_FLAG=\"--only-binary=:all:\";"
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
            pattern, suffix=".pattern",
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
            '; sys.exit(1)\n'
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
            search_py, suffix=".py",
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
        config = json.dumps({
            "path": path,
            "start": ls,
            "end": le,
        })
        cfg_file = _write_data_file(
            config, suffix=".json",
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
            read_py, suffix=".py",
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

    if step_type == "apply_patch":
        patch = step.get("patch") or ""
        p_file = _write_data_file(
            patch, suffix=".patch",
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
            "template_id", "",
        )
        params = step.get(
            "template_params", {},
        ) or {}
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
        safe_target = (
            shlex.quote(target)
            if target else ""
        )
        cmd_str = " ".join(
            [shlex.quote(x) for x in cmd],
        )
        cmd_str = cmd_str.replace(
            "{target}", safe_target,
        )
        script = (
            "#!/bin/bash\nset -euo pipefail\n"
            "if [ ! -f"
            " /work/venv/bin/activate ]; then\n"
            '  echo "Missing venv;'
            ' ensure_deps first"\n'
            "  exit 39\nfi\n"
            f"{cmd_str}\n"
        )
        return script, data_files

    # Unknown step type.
    script = (
        "#!/bin/bash\n"
        f"echo 'unknown step type: {step_type}'\n"
        "exit 1\n"
    )
    return script, data_files


@app.post("/run")
def run(req: ExecReq):
    repo_host, art_host, venv_host, wheels_host = _paths(req.repo_id)
    step = req.step
    t = step.get("type")

    if t == "ensure_deps":
        timeout_s = int(
            step.get("timeout_s")
            or DEPS.get("max_install_seconds", 420)
        )
        out = _ensure_deps(
            req.repo_id, repo_host, art_host,
            venv_host, wheels_host, timeout_s,
        )
        return {
            "status": out["status"],
            "seconds": out["seconds"],
            "logs": out["logs"],
            "payload": None,
        }

    if t == "repo_search":
        pattern = step.get("pattern") or ""
        timeout_s = int(step.get("timeout_s") or 30)
        out = _repo_search(
            pattern, repo_host, art_host,
            venv_host, wheels_host, timeout_s,
        )
        payload = out["logs"].strip()
        return {
            "status": out["status"],
            "seconds": out["seconds"],
            "logs": out["logs"],
            "payload": payload,
        }

    if t == "repo_read_range":
        path = step.get("path") or ""
        ls = int(step.get("line_start") or 1)
        le = int(step.get("line_end") or ls)
        timeout_s = int(step.get("timeout_s") or 30)
        out = _repo_read_range(
            path, ls, le,
            repo_host, art_host,
            venv_host, wheels_host, timeout_s,
        )
        payload = out["logs"]
        return {
            "status": out["status"],
            "seconds": out["seconds"],
            "logs": out["logs"],
            "payload": payload,
        }

    if t == "apply_patch":
        patch = step.get("patch") or ""
        timeout_s = int(step.get("timeout_s") or 60)
        out = _apply_patch(
            patch, repo_host, art_host,
            venv_host, wheels_host, timeout_s,
        )
        return {
            "status": out["status"],
            "seconds": out["seconds"],
            "logs": out["logs"],
            "payload": None,
        }

    if t == "run_tests":
        template_id = step.get("template_id") or ""
        params = step.get("template_params") or {}
        target = params.get("target") or ""
        timeout_s = int(
            step.get("timeout_s")
            or CMD_TEMPLATES.get(
                template_id, {}
            ).get("max_seconds", 240)
        )
        out = _run_tests(
            template_id, target, repo_host,
            art_host, venv_host, wheels_host,
            timeout_s,
        )
        return {
            "status": out["status"],
            "seconds": out["seconds"],
            "logs": out["logs"],
            "payload": None,
        }

    raise HTTPException(400, f"unknown step type: {t}")
