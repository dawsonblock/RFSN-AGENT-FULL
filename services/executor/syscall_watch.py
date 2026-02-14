"""Syscall/Process Execution Monitor."""

import re
from typing import List, NamedTuple


class SyscallAlert(NamedTuple):
    severity: str  # HIGH, MEDIUM, LOW
    syscall: str
    args: str
    message: str


class SyscallMonitor:
    """
    Monitors system calls or command executions for suspicious activity.
    In a real deployment, this would attach to ptrace or parse strace logs.
    Here, check_command() analyzes shell commands before execution.
    """

    # Suspicious shell patterns
    DANGEROUS_CMDS = [
        (
            re.compile(r"rm\s+-[rf]*.*[\*\?]+"),
            "HIGH",
            "Recursively deleting wildcard files",
        ),
        (
            re.compile(r"rm\s+-[rf]*.*/"),
            "HIGH",
            "Recursively deleting root or directory",
        ),
        (re.compile(r"mkfs"), "HIGH", "Formatting filesystem"),
        (re.compile(r"dd\s+if="), "MEDIUM", "Low-level data copy (dd)"),
        (re.compile(r"wget\s+|curl\s+"), "LOW", "Outbound network request"),
        (re.compile(r"chmod\s+777"), "MEDIUM", "Setting permissive permissions"),
        (re.compile(r":\(\)\{ :\|:& \};:"), "HIGH", "Fork bomb detected"),
    ]

    def check_command(self, cmd: str) -> List[SyscallAlert]:
        alerts = []
        for pattern, severity, msg in self.DANGEROUS_CMDS:
            if pattern.search(cmd):
                alerts.append(
                    SyscallAlert(
                        severity=severity, syscall="execve", args=cmd, message=msg
                    )
                )
        return alerts


def audit_exec(cmd: str) -> List[SyscallAlert]:
    """Global helper to audit a command string."""
    monitor = SyscallMonitor()
    return monitor.check_command(cmd)
