"""Shared helpers: hashing, JSON I/O, subprocess runner."""
from __future__ import annotations

import hashlib
import json
import os
import re
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


# Shell metacharacters that indicate a command needs shell=True.
_SHELL_META = re.compile(r"[&|;<>()$`\\\"'\n]|&&|\|\|")


def needs_shell(cmd: str) -> bool:
    """Return True if *cmd* contains shell operators (``&&``, ``|``, etc.)."""
    return bool(_SHELL_META.search(cmd))


def run_cmd(
    cmd: str | list[str],
    cwd: str,
    timeout_sec: int,
    *,
    shell: bool | None = None,
) -> Tuple[int, str, str, float]:
    """Run *cmd* as a subprocess.

    Returns (exit_code, stdout, stderr, elapsed_sec).

    **Shell mode** (``shell`` parameter):

    * ``None`` (default) — auto-detect.  If *cmd* is a string containing
      shell metacharacters (``&&``, ``|``, ``;``, etc.) it is executed via
      ``/bin/sh``.  Otherwise it is split with ``shlex.split`` and run
      without a shell (safe from injection).
    * ``True``  — always use ``shell=True`` (string passed to ``/bin/sh``).
    * ``False`` — always use ``shell=False`` (list passed to ``execvp``).

    When *shell* is ``False`` and *cmd* is a string, ``shlex.split`` is
    used.  Leading ``KEY=VALUE`` tokens are extracted into the process
    environment automatically (since ``shell=False`` doesn't interpret
    them).
    """
    import shlex as _shlex

    # Resolve auto-detect
    use_shell: bool
    if shell is not None:
        use_shell = shell
    elif isinstance(cmd, list):
        use_shell = False
    else:
        use_shell = needs_shell(cmd)

    # Build argv / shell string
    if use_shell:
        # Pass the raw string to /bin/sh — all operators are interpreted.
        shell_cmd = cmd if isinstance(cmd, str) else " ".join(cmd)
        argv: str | list[str] = shell_cmd
        env = None
    else:
        argv_list = _shlex.split(cmd) if isinstance(cmd, str) else list(cmd)

        # Extract leading KEY=VALUE tokens and add them to env
        env_override: dict[str, str] = {}
        _ENV_VAR = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
        while argv_list:
            m = _ENV_VAR.match(argv_list[0])
            if m:
                env_override[m.group(1)] = m.group(2)
                argv_list = argv_list[1:]
            else:
                break

        env = None
        if env_override:
            env = dict(os.environ)
            env.update(env_override)
        argv = argv_list

    t0 = time.time()
    p = subprocess.Popen(
        argv,
        cwd=cwd,
        shell=use_shell,
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
