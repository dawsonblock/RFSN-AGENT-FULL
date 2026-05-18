import os
import shlex
from typing import List, Dict, Optional
from dataclasses import dataclass


@dataclass
class Mount:
    source: str
    target: str
    read_only: bool = True
    type: str = "bind"  # bind, volume, tmpfs


class Capsule:
    """Encapsulates the execution isolation logic (The 'Capsule').

    Generates Docker arguments to enforce:
    1. Read-Only Host Repo (/mnt/repo_ro)
    2. Ephemeral/Isolated Workspace (/work/repo)
    3. Copy-on-Write Startup
    4. Network Airlock
    """

    def __init__(
        self,
        container_name: str,
        image: str,
        repo_host_path: str,
        work_type: str = "tmpfs",  # 'tmpfs' or 'bind' (for cold persist)
        work_host_path: Optional[str] = None,  # Required if work_type='bind'
        env: Optional[Dict[str, str]] = None,
        network_mode: str = "none",
        mem_limit: str = "2g",
    ):
        self.container_name = container_name
        self.image = image
        self.repo_host_path = repo_host_path
        self.work_type = work_type
        self.work_host_path = work_host_path
        self.env = env or {}
        self.network_mode = network_mode
        self.mem_limit = mem_limit

    def docker_args(self) -> List[str]:
        """Generate the docker run arguments."""
        args = [
            "docker",
            "run",
            "-d",
            "--name",
            self.container_name,
            "--network",
            self.network_mode,
            "--user",
            "1000:1000",
            "--security-opt",
            "no-new-privileges:true",
            "--read-only",
            "--memory",
            self.mem_limit,
            "--cpus",
            "2",
            "--pids-limit",
            "256",
            "--cap-drop",
            "ALL",
            "-e",
            "HOME=/tmp",
            "-w",
            "/work/repo",
        ]

        # Environment
        for k, v in self.env.items():
            args.extend(["-e", f"{k}={v}"])

        # Mounts
        # 1. Host Repo -> Read-Only Base
        args.extend(["-v", f"{self.repo_host_path}:/mnt/repo_ro:ro"])

        # 2. Workspace
        if self.work_type == "tmpfs":
            # RAM-backed workspace (Fast, Ephemeral)
            args.extend(["--tmpfs", "/work/repo:rw,exec,nosuid,nodev,size=512m"])
        elif self.work_type == "bind":
            # Disk-backed workspace (Persistent, Slower)
            if not self.work_host_path:
                raise ValueError("work_host_path required for bind workspace")
            args.extend(["-v", f"{self.work_host_path}:/work/repo:rw"])
        else:
            raise ValueError(f"Unknown work_type: {self.work_type}")

        # 3. Tmp (System) — noexec to prevent code execution from /tmp
        args.extend(["--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=256m"])

        # 4. Artifacts/Venv (Passthrough for now, can be tightened later)
        # In a strict capsule, these should also handled carefully.
        # For now, we assume caller handles these additional mounts if needed via subclassing or mixin.
        # But wait, `sandbox_pool` adds them. We should probably allow adding extra mounts.

        return args

    def entrypoint_cmd(self, user_cmd: str = "tail -f /dev/null") -> List[str]:
        """Generate the shell entrypoint to perform Copy-on-Write."""
        # 1. If /work/repo is empty (tmpfs), copy from /mnt/repo_ro
        # 2. Execute user command

        # Note: We use 'cp -rT' or 'cp -r' followed by dot.
        # We rely on bash.

        init_script = (
            'if [ -z "$(ls -A /work/repo)" ]; then '
            "  echo 'Initializing Capsule...'; "
            "  cp -r /mnt/repo_ro/. /work/repo/; "
            "fi; "
            f"exec {user_cmd}"
        )

        return [self.image, "bash", "-c", init_script]
