"""Parse structured logs and test output into failure records.

Reads JSON log lines, pytest output, or raw text and extracts
actionable failure records.
"""

import json
import os
import re
from typing import Any, Dict, List, Optional


class FailureRecord:
    """A single failure extracted from logs."""

    def __init__(
        self,
        source: str,
        message: str,
        kind: str = "unknown",
        file: str = "",
        line: int = 0,
        traceback: str = "",
    ):
        self.source = source
        self.message = message
        self.kind = kind
        self.file = file
        self.line = line
        self.traceback = traceback

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "message": self.message,
            "kind": self.kind,
            "file": self.file,
            "line": self.line,
            "traceback": self.traceback,
        }


def parse_jsonl(log_path: str) -> List[FailureRecord]:
    """Parse JSONL log files for error-level entries."""
    records = []
    try:
        with open(log_path) as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                level = entry.get("level", "").lower()
                if level in ("error", "fatal", "critical"):
                    records.append(
                        FailureRecord(
                            source=log_path,
                            message=entry.get("message", entry.get("msg", str(entry))),
                            kind=entry.get("error_type", "log_error"),
                            file=entry.get("file", ""),
                            line=entry.get("line", line_no),
                            traceback=entry.get("traceback", ""),
                        )
                    )
    except FileNotFoundError:
        pass
    return records


def parse_pytest_output(text: str, source: str = "pytest") -> List[FailureRecord]:
    """Parse pytest output for FAILED tests."""
    records = []
    # Match "FAILED test_file.py::test_name - reason"
    pattern = re.compile(r"FAILED\s+([\w/\\.]+)::(\w+)(?:\s*-\s*(.+))?")
    for match in pattern.finditer(text):
        file_path = match.group(1)
        test_name = match.group(2)
        reason = match.group(3) or ""
        records.append(
            FailureRecord(
                source=source,
                message=f"{test_name}: {reason}" if reason else test_name,
                kind="test_failure",
                file=file_path,
            )
        )

    # Match tracebacks
    tb_pattern = re.compile(
        r'File "([^"]+)", line (\d+)',
    )
    for match in tb_pattern.finditer(text):
        # Only create records for non-stdlib files
        fpath = match.group(1)
        if "/site-packages/" not in fpath and "/lib/python" not in fpath:
            records.append(
                FailureRecord(
                    source=source,
                    message=f"Traceback in {os.path.basename(fpath)}",
                    kind="traceback",
                    file=fpath,
                    line=int(match.group(2)),
                )
            )

    return records


def parse_text_errors(text: str, source: str = "raw") -> List[FailureRecord]:
    """Parse raw text for common error patterns."""
    records = []
    patterns = [
        (r"ImportError:\s*(.+)", "import_error"),
        (r"SyntaxError:\s*(.+)", "syntax_error"),
        (r"ModuleNotFoundError:\s*(.+)", "import_error"),
        (r"TimeoutError:\s*(.+)", "timeout"),
        (r"MemoryError", "oom"),
        (r"OSError:\s*(.+)", "os_error"),
        (r"FATAL:\s*(.+)", "fatal"),
    ]
    for pattern, kind in patterns:
        for match in re.finditer(pattern, text):
            records.append(
                FailureRecord(
                    source=source,
                    message=match.group(0),
                    kind=kind,
                )
            )
    return records


def ingest(
    log_paths: Optional[List[str]] = None,
    raw_text: str = "",
) -> List[Dict[str, Any]]:
    """Ingest failures from all sources.

    Returns list of failure record dicts.
    """
    all_records: List[FailureRecord] = []

    for path in log_paths or []:
        if path.endswith(".jsonl") or path.endswith(".ndjson"):
            all_records.extend(parse_jsonl(path))
        else:
            try:
                with open(path) as f:
                    text = f.read()
                all_records.extend(parse_pytest_output(text, source=path))
                all_records.extend(parse_text_errors(text, source=path))
            except FileNotFoundError:
                pass

    if raw_text:
        all_records.extend(parse_pytest_output(raw_text))
        all_records.extend(parse_text_errors(raw_text))

    return [r.to_dict() for r in all_records]
