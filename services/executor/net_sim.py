"""Network egress simulation/control."""

import socket
from fnmatch import fnmatch
from typing import List


class NetworkViolation(Exception):
    pass


class NetworkPolicy:
    def __init__(self, allowlist: List[str]):
        """
        Initialize with a list of allowed patterns.
        e.g. ['*.google.com', 'pypi.org', 'files.pythonhosted.org']
        """
        self.allowlist = allowlist or []

    def check_connection(self, host: str, port: int = 443) -> bool:
        """
        Check if a connection to host:port is allowed.
        Raises NetworkViolation if blocked.
        """
        # 1. Check if host matches allowlist patterns
        allowed = False
        for pattern in self.allowlist:
            if fnmatch(host, pattern):
                allowed = True
                break

        if not allowed:
            raise NetworkViolation(f"Connection denied to {host}:{port}")

        return True


# Default strict policy for RFSN agents
# Only allow package repositories and known APIs
DEFAULT_POLICY = NetworkPolicy(
    [
        "pypi.org",
        "files.pythonhosted.org",
        "github.com",
        "api.deepseek.com",  # If agent calls LLM directly (unlikely in sandbox, but possible)
        "*.googleapis.com",
    ]
)


def check_egress(host: str):
    """Global helper to check egress."""
    return DEFAULT_POLICY.check_connection(host)
