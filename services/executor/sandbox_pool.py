"""Warm sandbox pool — one persistent container per run_id.

Instead of `docker run --rm` per tool call (expensive: ~2-5s
startup each time), we keep a long-lived container per run_id
and `docker exec` into it.  The container is destroyed when
the run ends (success, fail, or timeout).

Security model is identical to the ephemeral path:
  - --user 1000:1000 (non-root)
  - --no-new-privileges
  - --memory 2g / --cpus 2 / --pids-limit 256
  - --cap-drop ALL
  - network disabled by default
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional


BLESSED_IMAGE = os.getenv(
    "BLESSED_IMAGE", "rfsn-blessed:0.2",
)

# Max idle time before auto-reap (seconds).
_IDLE_TTL = int(os.getenv("SANDBOX_IDLE_TTL", "600"))


@dataclass
class Sandbox:
    """A running sandbox container."""

    container_id: str
    run_id: str
    image: str
    image_hash: str
    created_at: float
    last_used_at: float
    exec_count: int = 0
    lock: threading.Lock = field(
        default_factory=threading.Lock,
    )


class SandboxPool:
    """Manages warm per-run sandbox containers.

    Thread-safe: each sandbox has its own lock
    for exec serialization, and the pool-level
    dict is guarded by _pool_lock.
    """

    def __init__(self) -> None:
        self._pool: Dict[str, Sandbox] = {}
        self._pool_lock = threading.Lock()
        # Start background reaper.
        self._reaper = threading.Thread(
            target=self._reap_loop,
            daemon=True,
        )
        self._reaper.start()

    # ── Public API ──────────────────────────

    def get_or_create(
        self,
        run_id: str,
        repo_host: str,
        art_host: str,
        venv_host: str,
        wheels_host: str,
        network: str = "none",
    ) -> Sandbox:
        """Get existing sandbox or create one."""
        with self._pool_lock:
            sb = self._pool.get(run_id)
            if sb and self._is_alive(sb):
                return sb

        # Create new container (outside pool lock
        # so we don't block other runs).
        sb = self._create(
            run_id, repo_host, art_host,
            venv_host, wheels_host, network,
        )

        with self._pool_lock:
            # Race: another thread may have created.
            existing = self._pool.get(run_id)
            if existing and self._is_alive(existing):
                self._destroy(sb)
                return existing
            self._pool[run_id] = sb
        return sb

    def exec_in(
        self,
        sandbox: Sandbox,
        script: str,
        data_files: Dict[str, str],
        timeout_s: int,
        workdir: str = "/work",
    ) -> dict:
        """Execute a script inside the sandbox.

        Data files are copied in via `docker cp`.
        Returns {"status": int, "seconds": float,
                 "logs": str}.
        """
        with sandbox.lock:
            sandbox.last_used_at = time.time()
            sandbox.exec_count += 1

            cid = sandbox.container_id
            script_path = self._write_tmp(
                script, ".sh",
            )
            copied: list[str] = [script_path]

            try:
                # Copy script into container.
                subprocess.run(
                    [
                        "docker", "cp",
                        script_path,
                        f"{cid}:/tmp/rfsn_script.sh",
                    ],
                    check=True, timeout=10,
                    capture_output=True,
                )

                # Copy data files.
                for cpath, hpath in (
                    data_files.items()
                ):
                    # Ensure target dir exists.
                    cdir = os.path.dirname(cpath)
                    subprocess.run(
                        [
                            "docker", "exec", cid,
                            "mkdir", "-p", cdir,
                        ],
                        check=False, timeout=5,
                        capture_output=True,
                    )
                    subprocess.run(
                        [
                            "docker", "cp",
                            hpath,
                            f"{cid}:{cpath}",
                        ],
                        check=True, timeout=10,
                        capture_output=True,
                    )
                    copied.append(hpath)

                # Execute.
                start = time.time()
                try:
                    p = subprocess.run(
                        [
                            "docker", "exec",
                            "-w", workdir,
                            cid,
                            "bash",
                            "/tmp/rfsn_script.sh",
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        timeout=timeout_s,
                        text=True,
                    )
                    out = p.stdout.replace(
                        "\r\n", "\n",
                    )[-200_000:]
                    return {
                        "status": p.returncode,
                        "seconds": time.time() - start,
                        "logs": out,
                    }
                except subprocess.TimeoutExpired as e:
                    raw_out = e.stdout or ""
                    if isinstance(raw_out, bytes):
                        raw_out = raw_out.decode(
                            "utf-8", errors="replace",
                        )
                    raw_str: str = str(raw_out)
                    return {
                        "status": 124,
                        "seconds": time.time() - start,
                        "logs": (
                            raw_str + "\n[TIMEOUT]\n"
                        )[-200_000:],
                    }

            finally:
                # Clean up local temp files.
                for fp in copied:
                    try:
                        os.unlink(fp)
                    except OSError:
                        pass

    def destroy_run(self, run_id: str) -> Optional[str]:
        """Destroy the sandbox for a run.

        Returns the image hash for ledger recording,
        or None if no sandbox existed.
        """
        with self._pool_lock:
            sb = self._pool.pop(run_id, None)
        if sb:
            img_hash = sb.image_hash
            self._destroy(sb)
            return img_hash
        return None

    def active_count(self) -> int:
        with self._pool_lock:
            return len(self._pool)

    def stats(self) -> dict:
        with self._pool_lock:
            return {
                "active": len(self._pool),
                "sandboxes": {
                    rid: {
                        "container_id": sb.container_id[:12],
                        "exec_count": sb.exec_count,
                        "age_s": round(
                            time.time() - sb.created_at,
                            1,
                        ),
                        "idle_s": round(
                            time.time() - sb.last_used_at,
                            1,
                        ),
                    }
                    for rid, sb in self._pool.items()
                },
            }

    # ── Internal ───────────────────────────

    def _create(
        self,
        run_id: str,
        repo_host: str,
        art_host: str,
        venv_host: str,
        wheels_host: str,
        network: str,
    ) -> Sandbox:
        """Start a new persistent container."""
        # Generate a deterministic container name.
        tag = hashlib.sha256(
            run_id.encode(),
        ).hexdigest()[:12]
        name = f"rfsn-sandbox-{tag}"

        args = [
            "docker", "run", "-d",
            "--name", name,
            "--network", network,
            "--user", "1000:1000",
            "--no-new-privileges",
            "--memory", "2g",
            "--cpus", "2",
            "--pids-limit", "256",
            "--cap-drop", "ALL",
            "-v", f"{repo_host}:/work/repo:rw",
            "-v", f"{art_host}:/work/artifacts:rw",
            "-v", f"{venv_host}:/work/venv:rw",
            "-v", f"{wheels_host}:/work/wheels:rw",
            "-w", "/work",
            BLESSED_IMAGE,
            # Keep alive with tail -f /dev/null.
            "tail", "-f", "/dev/null",
        ]

        p = subprocess.run(
            args,
            capture_output=True, text=True,
            timeout=30,
        )
        if p.returncode != 0:
            raise RuntimeError(
                f"Failed to create sandbox:"
                f" {p.stderr.strip()}"
            )
        cid = p.stdout.strip()

        # Get image hash.
        img_hash = ""
        try:
            ih = subprocess.run(
                [
                    "docker", "inspect",
                    "--format",
                    "{{.Image}}",
                    cid,
                ],
                capture_output=True, text=True,
                timeout=5,
            )
            if ih.returncode == 0:
                img_hash = ih.stdout.strip()[:24]
        except Exception:
            pass

        now = time.time()
        return Sandbox(
            container_id=cid,
            run_id=run_id,
            image=BLESSED_IMAGE,
            image_hash=img_hash,
            created_at=now,
            last_used_at=now,
        )

    def _destroy(self, sb: Sandbox) -> None:
        """Force-remove a sandbox container."""
        try:
            subprocess.run(
                [
                    "docker", "rm", "-f",
                    sb.container_id,
                ],
                capture_output=True,
                timeout=15,
            )
        except Exception:
            pass

    def _is_alive(self, sb: Sandbox) -> bool:
        """Check if container is still running."""
        try:
            p = subprocess.run(
                [
                    "docker", "inspect",
                    "--format",
                    "{{.State.Running}}",
                    sb.container_id,
                ],
                capture_output=True, text=True,
                timeout=5,
            )
            return (
                p.returncode == 0
                and "true" in p.stdout.lower()
            )
        except Exception:
            return False

    def _reap_loop(self) -> None:
        """Background thread: reap idle sandboxes."""
        while True:
            time.sleep(60)
            now = time.time()
            to_reap: list[str] = []

            with self._pool_lock:
                for rid, sb in self._pool.items():
                    idle = now - sb.last_used_at
                    if idle > _IDLE_TTL:
                        to_reap.append(rid)

            for rid in to_reap:
                self.destroy_run(rid)

    @staticmethod
    def _write_tmp(
        content: str, suffix: str,
    ) -> str:
        fd = tempfile.NamedTemporaryFile(
            delete=False,
            suffix=suffix,
            mode="w",
            encoding="utf-8",
            dir="/tmp",
        )
        fd.write(content)
        fd.close()
        return fd.name
