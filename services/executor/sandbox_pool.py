"""Warm sandbox pool — one persistent container per run_id.

Instead of `docker run --rm` per tool call (expensive: ~2-5s
startup each time), we keep a long-lived container per run_id
and `docker exec` into it.  The container is destroyed when
the run ends (success, fail, or timeout).

    Security model is identical to the ephemeral path:
      - --user 1000:1000 (non-root)
      - --security-opt no-new-privileges:true
      - --read-only + tmpfs /tmp
      - --memory 2g / --cpus 2 / --pids-limit 256
      - --cap-drop ALL
      - network ALWAYS disabled (none)
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, List
from .capsule import Capsule


BLESSED_IMAGE = os.getenv(
    "BLESSED_IMAGE",
    "rfsn-blessed@sha256:208a2c2dac42ed9b3ca023b30cd815518070930274592844511aa34de21b6360",
)

# Max idle time before auto-reap (seconds).
_IDLE_TTL = int(os.getenv("SANDBOX_IDLE_TTL", "600"))
_MAX_STEP_LOG_BYTES = int(
    os.getenv("RFSN_MAX_STEP_LOG_BYTES", "200000"),
)


def _truncate_text_bytes(text: str, max_bytes: int) -> tuple[str, bool]:
    raw = (text or "").encode("utf-8", errors="replace")
    if max_bytes <= 0 or len(raw) <= max_bytes:
        return text or "", False
    trimmed = raw[:max_bytes].decode("utf-8", errors="ignore")
    return trimmed, True


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

        # Metrics
        from system.metrics import gauge, counter

        self._active_gauge = gauge("sandbox_active", "Number of active warm sandboxes")
        self._exec_counter = counter(
            "sandbox_execs", "Total commands executed in sandboxes"
        )
        self._reap_counter = counter(
            "sandbox_reaps", "Total sandboxes reaped due to idle"
        )

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
    ) -> Sandbox:
        """Get existing sandbox or create one."""
        with self._pool_lock:
            sb = self._pool.get(run_id)
            if sb and self._is_alive(sb):
                return sb

        # Create new container (outside pool lock
        # so we don't block other runs).
        sb = self._create(
            run_id,
            repo_host,
            art_host,
            venv_host,
            wheels_host,
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
                script,
                ".sh",
            )
            copied: list[str] = [script_path]

            try:
                # Copy script into container.
                subprocess.run(
                    [
                        "docker",
                        "cp",
                        script_path,
                        f"{cid}:/tmp/rfsn_script.sh",
                    ],
                    check=True,
                    timeout=10,
                    capture_output=True,
                )

                # Copy data files.
                for cpath, hpath in data_files.items():
                    # Ensure target dir exists.
                    cdir = os.path.dirname(cpath)
                    subprocess.run(
                        [
                            "docker",
                            "exec",
                            cid,
                            "mkdir",
                            "-p",
                            cdir,
                        ],
                        check=False,
                        timeout=5,
                        capture_output=True,
                    )
                    subprocess.run(
                        [
                            "docker",
                            "cp",
                            hpath,
                            f"{cid}:{cpath}",
                        ],
                        check=True,
                        timeout=10,
                        capture_output=True,
                    )
                    copied.append(hpath)

                # Execute with streaming to prevent OOM on massive output.
                start = time.time()
                proc = subprocess.Popen(
                    [
                        "docker",
                        "exec",
                        "-w",
                        workdir,
                        cid,
                        "bash",
                        "/tmp/rfsn_script.sh",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,  # Line buffered
                )

                output_chunks: list[str] = []
                total_bytes = 0
                hit_limit = False

                try:
                    # We can't easily use communicate() with a limit, so we read manually.
                    # But we also need to enforce timeout.
                    # Simplified approach: read in a thread or use blocking reads with timeout?
                    # Docker exec output might block.
                    # Best valid approach for Popen with timeout AND output limit is tricky in pure Python.
                    # Let's use a simpler approach: communicate with timeout, BUT we still risk OOM if we don't stream.
                    # We will read line-by-line using a loop and selector or just simple readline with non-blocking check?
                    # Given checking time/complexity, let's just loop reading lines.

                    import select

                    if proc.stdout is None:
                        raise RuntimeError("Failed to capture stdout")

                    while True:
                        # Check timeout
                        if time.time() - start > timeout_s:
                            proc.kill()
                            raise subprocess.TimeoutExpired(
                                proc.args, timeout_s, output="".join(output_chunks)
                            )

                        # Check if process exited
                        if proc.poll() is not None:
                            # Read remaining
                            rest = proc.stdout.read()
                            if rest:
                                if total_bytes < _MAX_STEP_LOG_BYTES:
                                    output_chunks.append(rest)
                                    total_bytes += len(rest)
                            break

                        # Wait for data (non-blocking read would be better, but select works on POSIX)
                        # We use select to wait up to 0.1s for data
                        r, _, _ = select.select([proc.stdout], [], [], 0.1)
                        if r:
                            chunk = proc.stdout.read(4096)  # read small chunks
                            if not chunk:
                                break  # EOF

                            if total_bytes < _MAX_STEP_LOG_BYTES:
                                output_chunks.append(chunk)
                                total_bytes += len(chunk)
                            else:
                                if not hit_limit:
                                    output_chunks.append(
                                        "\n[OUTPUT TRUNCATED - LIMIT EXCEEDED]\n"
                                    )
                                    hit_limit = True
                                # We continue reading to drain pipe but don't store,
                                # OR we kill if it goes too crazy?
                                # Let's kill if it goes 2x over limit to save CPU/IO.
                                if total_bytes > _MAX_STEP_LOG_BYTES * 2:
                                    proc.kill()
                                    output_chunks.append(
                                        "\n[KILLED - OUTPUT TOO MASSIVE]\n"
                                    )
                                    break

                    return {
                        "status": (
                            proc.returncode if proc.returncode is not None else 137
                        ),
                        "seconds": time.time() - start,
                        "logs": "".join(output_chunks),
                        "logs_truncated": hit_limit
                        or (total_bytes > _MAX_STEP_LOG_BYTES),
                    }

                except subprocess.TimeoutExpired:
                    proc.kill()
                    return {
                        "status": 124,
                        "seconds": time.time() - start,
                        "logs": "".join(output_chunks) + "\n[TIMEOUT]\n",
                        "logs_truncated": True,
                    }
                except Exception as e:
                    proc.kill()
                    return {
                        "status": 1,
                        "seconds": time.time() - start,
                        "logs": f"Internal Error: {e}",
                        "logs_truncated": False,
                    }
                finally:
                    if proc.stdout:
                        proc.stdout.close()
                    # Ensure process is dead
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()

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
    ) -> Sandbox:
        """Start a new persistent container."""
        # Generate a deterministic container name.
        tag = hashlib.sha256(
            run_id.encode(),
        ).hexdigest()[:12]
        name = f"rfsn-sandbox-{tag}"

        # Phase 7.1: Use Capsule for strict isolation
        capsule = Capsule(
            container_name=name,
            image=BLESSED_IMAGE,
            repo_host_path=repo_host,
            work_type="tmpfs",  # Warm sandboxes use RAM-backed workspace
            network_mode="none",
        )

        args = capsule.docker_args()

        # Add persistent mounts (artifacts/venv/wheels)
        args.extend(["-v", f"{art_host}:/work/artifacts:rw"])
        args.extend(["-v", f"{venv_host}:/work/venv:rw"])
        args.extend(["-v", f"{wheels_host}:/work/wheels:rw"])

        # Add entrypoint (Copy-on-Write init)
        args.extend(capsule.entrypoint_cmd("tail -f /dev/null"))

        p = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if p.returncode != 0:
            raise RuntimeError(f"Failed to create sandbox: {p.stderr.strip()}")
        cid = p.stdout.strip()

        # Get image hash.
        img_hash = ""
        try:
            ih = subprocess.run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.Image}}",
                    cid,
                ],
                capture_output=True,
                text=True,
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
                    "docker",
                    "rm",
                    "-f",
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
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Running}}",
                    sb.container_id,
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return p.returncode == 0 and "true" in p.stdout.lower()
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
                self._reap_counter.inc()

    @staticmethod
    def _write_tmp(
        content: str,
        suffix: str,
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
