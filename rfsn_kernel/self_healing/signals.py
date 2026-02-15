"""Signal extraction from raw execution logs.

Converts unstructured log output into structured diagnostic signals
that the SelfHealingCore can reason about.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional


# ── Known failure patterns ────────────────────────────────────────────
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("import_error", re.compile(r"(?:ModuleNotFoundError|ImportError):\s*(.+)", re.I)),
    ("syntax_error", re.compile(r"SyntaxError:\s*(.+)", re.I)),
    ("type_error", re.compile(r"TypeError:\s*(.+)", re.I)),
    ("attribute_error", re.compile(r"AttributeError:\s*(.+)", re.I)),
    ("recursion_error", re.compile(r"RecursionError:\s*(.+)", re.I)),
    ("timeout", re.compile(r"\[TIMEOUT\]", re.I)),
    ("oom", re.compile(r"(MemoryError|Killed|OOMKilled)", re.I)),
    ("permission_denied", re.compile(r"PermissionError:\s*(.+)", re.I)),
    ("file_not_found", re.compile(r"FileNotFoundError:\s*(.+)", re.I)),
    ("assertion_error", re.compile(r"AssertionError", re.I)),
    ("test_failure", re.compile(r"FAILED\s+\S+", re.I)),
    ("patch_apply_failed", re.compile(r"error: patch failed|REJECTED:", re.I)),
]


@dataclass
class Signal:
    """A structured diagnostic signal extracted from logs."""

    failure_type: str  # e.g. "import_error", "timeout", "unknown"
    stack_fingerprint: str  # SHA-256 of normalized traceback
    anomaly_score: float  # 0.0 (normal) → 1.0 (highly anomalous)
    detail: str = ""  # First matched detail string
    raw_traceback: str = ""  # The raw traceback text if found


def _extract_traceback(logs: str) -> str:
    """Pull the last Python traceback from logs."""
    tb_pattern = re.compile(
        r"Traceback \(most recent call last\):.*?(?=\n\S|\Z)",
        re.DOTALL,
    )
    matches = tb_pattern.findall(logs)
    return matches[-1].strip() if matches else ""


def _fingerprint(text: str) -> str:
    """Create a stable fingerprint from a traceback.

    Strips line numbers and memory addresses to group
    structurally identical tracebacks together.
    """
    if not text:
        return hashlib.sha256(b"no_traceback").hexdigest()[:16]
    # Normalize: strip line numbers, hex addresses, timestamps
    normalized = re.sub(r"line \d+", "line N", text)
    normalized = re.sub(r"0x[0-9a-fA-F]+", "0xADDR", normalized)
    normalized = re.sub(
        r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", "TIMESTAMP", normalized
    )
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _compute_anomaly_score(
    logs: str,
    status: int,
    failure_type: str,
) -> float:
    """Heuristic anomaly score.

    Higher = more unusual / dangerous.
    """
    score = 0.0

    # Non-zero exit is baseline anomaly
    if status != 0:
        score += 0.3

    # Certain failure types are more dangerous
    dangerous = {"recursion_error", "oom", "permission_denied", "timeout"}
    if failure_type in dangerous:
        score += 0.4

    # Very long logs often indicate runaway output
    if len(logs) > 500_000:
        score += 0.2

    # Multiple tracebacks = cascading failure
    tb_count = logs.count("Traceback (most recent call last)")
    if tb_count > 2:
        score += 0.1

    return min(score, 1.0)


def extract_signals(logs: str, status: int) -> Signal:
    """Extract a structured Signal from raw execution output.

    Args:
        logs: Raw stdout/stderr from execution.
        status: Process exit code (0 = success).

    Returns:
        A Signal dataclass with failure classification.
    """
    if status == 0:
        return Signal(
            failure_type="success",
            stack_fingerprint=_fingerprint(""),
            anomaly_score=0.0,
        )

    # Classify failure type
    failure_type = "unknown"
    detail = ""
    for name, pattern in _PATTERNS:
        m = pattern.search(logs)
        if m:
            failure_type = name
            detail = m.group(1) if m.lastindex else m.group(0)
            break

    # Extract traceback
    raw_tb = _extract_traceback(logs)
    fp = _fingerprint(raw_tb)
    anomaly = _compute_anomaly_score(logs, status, failure_type)

    return Signal(
        failure_type=failure_type,
        stack_fingerprint=fp,
        anomaly_score=anomaly,
        detail=detail.strip()[:256],
        raw_traceback=raw_tb[:2048],
    )
