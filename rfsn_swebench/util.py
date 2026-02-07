"""Shared helpers: hashing, JSON I/O, subprocess runner."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from typing import Tuple


def now_ms() -> int:
    return int(time.time() * 1000)


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def write_json(path: str, obj: object) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def run_cmd(
    cmd: str | list[str],
    cwd: str,
    timeout_sec: int,
) -> Tuple[int, str, str, float]:
    """Run *cmd* WITHOUT a shell.

    Returns (exit_code, stdout, stderr, elapsed_sec).

    If *cmd* is a string it is split with ``shlex.split`` — shell=False
    eliminates command-injection via crafted arguments.

    Environment variable assignments at the start of the command (e.g.
    ``PYTHONPATH=. pytest ...``) are extracted and added to the process
    environment automatically, since shell=False doesn't interpret them.
    """
    import re as _re
    import shlex as _shlex

    argv = _shlex.split(cmd) if isinstance(cmd, str) else list(cmd)

    # Extract leading KEY=VALUE tokens and add them to env
    env_override: dict[str, str] = {}
    _ENV_VAR = _re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
    while argv:
        m = _ENV_VAR.match(argv[0])
        if m:
            env_override[m.group(1)] = m.group(2)
            argv = argv[1:]
        else:
            break

    env = None
    if env_override:
        env = dict(os.environ)
        env.update(env_override)

    t0 = time.time()
    p = subprocess.Popen(
        argv,
        cwd=cwd,
        shell=False,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        out, err = p.communicate(timeout=timeout_sec)
        dt = time.time() - t0
        return p.returncode, out, err, dt
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
        dt = time.time() - t0
        return 124, out, err + "\n[TIMEOUT]", dt
