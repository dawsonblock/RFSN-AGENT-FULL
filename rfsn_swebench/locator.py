"""Multi-file localization and repo-context utilities.

Provides helpers for:
  - Building a compact repo tree listing for LLM context
  - Parsing LLM file-localization responses
  - Reading and concatenating file contents within token budgets
"""

from __future__ import annotations

import json
import os
import re


# Directories to skip when building the repo tree
_SKIP_DIRS = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".eggs",
    "dist",
    "build",
    "*.egg-info",
    ".hypothesis",
}

# Extensions to include (common source files)
_INCLUDE_EXTS = {
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".java",
    ".go",
    ".rs",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".rb",
    ".php",
    ".yaml",
    ".yml",
    ".toml",
    ".cfg",
    ".ini",
    ".json",
    ".md",
    ".rst",
    ".txt",
}


def build_repo_tree(
    workdir: str,
    *,
    max_files: int = 200,
    max_depth: int = 6,
) -> str:
    """Walk the repo and return a compact tree listing.

    Skips hidden/cache directories and limits output to *max_files*
    entries to stay within LLM context budgets.
    """
    entries: list[str] = []
    workdir = os.path.abspath(workdir)

    for root, dirs, files in os.walk(workdir):
        # Calculate depth from workdir
        rel_root = os.path.relpath(root, workdir)
        depth = 0 if rel_root == "." else rel_root.count(os.sep) + 1

        if depth > max_depth:
            dirs[:] = []
            continue

        # Prune skipped directories (in-place to prevent os.walk descent)
        dirs[:] = sorted(
            [
                d
                for d in dirs
                if d not in _SKIP_DIRS
                and not d.startswith(".")
                and not d.endswith(".egg-info")
            ]
        )

        for fname in sorted(files):
            if len(entries) >= max_files:
                break

            ext = os.path.splitext(fname)[1].lower()
            if ext not in _INCLUDE_EXTS:
                continue

            rel_path = os.path.relpath(os.path.join(root, fname), workdir)
            entries.append(rel_path)

        if len(entries) >= max_files:
            break

    return "\n".join(entries)


def locate_files(llm_response: str) -> list[str]:
    """Parse file paths from an LLM localization response.

    Handles several output formats:
    1. JSON array: ``["path/to/file.py", ...]``
    2. JSON object with a "files" key
    3. Markdown list: ``- path/to/file.py``
    4. Plain newline-separated paths

    Returns a deduplicated, order-preserved list of file paths.
    """
    paths: list[str] = []

    # Try JSON first (may be inside a code block)
    json_str = llm_response.strip()
    # Extract from code blocks if present
    code_blocks = re.findall(
        r"```(?:json)?\s*\n(.*?)```",
        json_str,
        re.DOTALL,
    )
    if code_blocks:
        json_str = code_blocks[-1].strip()

    try:
        parsed = json.loads(json_str)
        if isinstance(parsed, list):
            paths = [str(p).strip() for p in parsed if p]
        elif isinstance(parsed, dict):
            # Try common keys
            for key in ("files", "paths", "file_paths", "targets"):
                if key in parsed and isinstance(parsed[key], list):
                    paths = [str(p).strip() for p in parsed[key] if p]
                    break
        if paths:
            return _dedupe(paths)
    except (json.JSONDecodeError, TypeError):
        pass

    # Try markdown list
    md_lines = re.findall(
        r"^[-*]\s+`?([^`\n]+\.(?:py|js|ts|java|go|rs|c|cpp|h))`?",
        llm_response,
        re.MULTILINE,
    )
    if md_lines:
        return _dedupe([p.strip() for p in md_lines])

    # Try numbered list
    numbered = re.findall(
        r"^\d+\.\s+`?([^`\n]+\.(?:py|js|ts|java|go|rs|c|cpp|h))`?",
        llm_response,
        re.MULTILINE,
    )
    if numbered:
        return _dedupe([p.strip() for p in numbered])

    # Fallback: find anything that looks like a file path
    file_patterns = re.findall(
        r"(?:^|\s)([A-Za-z0-9_./]+\.(?:py|js|ts|java|go|rs|c|cpp|h))\b",
        llm_response,
    )
    if file_patterns:
        return _dedupe([p.strip() for p in file_patterns])

    return []


def _ast_safe_truncate(content: str, max_chars: int) -> str:
    """Truncate Python source at a complete function/class boundary.

    Uses the ``ast`` module to find where top-level definitions end,
    then truncates at the last complete definition that fits within
    *max_chars*.  Falls back to raw truncation on parse error.
    """
    if len(content) <= max_chars:
        return content
    try:
        import ast

        tree = ast.parse(content)
    except SyntaxError:
        return content[:max_chars] + "\n...(truncated)"

    # Collect the end-line of every top-level node
    lines = content.splitlines(keepends=True)
    last_safe_end = 0
    char_offset = 0
    line_offsets = [0]
    for line in lines:
        char_offset += len(line)
        line_offsets.append(char_offset)

    for node in ast.iter_child_nodes(tree):
        if not hasattr(node, "end_lineno") or node.end_lineno is None:
            continue
        end_offset = line_offsets[min(node.end_lineno, len(lines))]
        if end_offset <= max_chars:
            last_safe_end = end_offset

    if last_safe_end > 0:
        return content[:last_safe_end] + "\n...(truncated at class/function boundary)"
    return content[:max_chars] + "\n...(truncated)"


def read_file_context(
    workdir: str,
    paths: list[str],
    *,
    max_chars_per_file: int = 15000,
    max_total_chars: int = 60000,
) -> str:
    """Read and concatenate file contents for LLM context.

    Truncates individual files at *max_chars_per_file* and the total
    output at *max_total_chars* to stay within token budgets.
    Python files use AST-aware truncation to avoid cutting functions
    in half.
    """
    parts: list[str] = []
    total = 0

    for rel_path in paths:
        if total >= max_total_chars:
            break

        abs_path = os.path.join(workdir, rel_path)
        if not os.path.isfile(abs_path):
            parts.append(f"## File: {rel_path}\n(file not found)\n")
            continue

        try:
            read_limit = min(max_chars_per_file, max_total_chars - total)
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(read_limit + 2000)  # read extra for AST boundary
        except Exception:
            parts.append(f"## File: {rel_path}\n(read error)\n")
            continue

        remaining = max_total_chars - total
        effective_limit = min(read_limit, remaining)

        # AST-aware truncation for Python files
        if rel_path.endswith(".py") and len(content) > effective_limit:
            content = _ast_safe_truncate(content, effective_limit)
        elif len(content) > effective_limit:
            content = content[:effective_limit] + "\n...(truncated)"

        # Add line numbers for easier LLM reference
        numbered_lines = []
        for i, line in enumerate(content.splitlines(), 1):
            numbered_lines.append(f"{i:4d} | {line}")
        numbered_content = "\n".join(numbered_lines)

        block = f"## File: {rel_path}\n```python\n{numbered_content}\n```\n"
        parts.append(block)
        total += len(content)

    return "\n".join(parts)


def _dedupe(items: list[str]) -> list[str]:
    """Deduplicate while preserving order."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result
