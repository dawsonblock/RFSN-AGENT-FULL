"""
AST Context Slicer for RFSN-AGENT.
Generates compressed 'Repo Maps' to prevent context poisoning.
"""

import ast
import os
from typing import List, Optional, Set


def generate_repo_map(filepath: str, focus_nodes: Optional[List[str]] = None) -> str:
    """
    Parses a Python file and returns a skeletonized version.

    Args:
        filepath: Absolute path to the file.
        focus_nodes: List of function/class names to keep full bodies for.
                     others will be replaced with '... # [Body omitted]'

    Returns:
        Skeletonized source code string.
    """
    if not os.path.exists(filepath):
        return f"# Error: File not found: {filepath}"

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            source = f.read()

        tree = ast.parse(source)
    except SyntaxError as e:
        return f"# Error parsing {filepath}: {e}"
    except Exception as e:
        return f"# Error reading {filepath}: {e}"

    focussed = set(focus_nodes or [])

    class Skeletonizer(ast.NodeTransformer):
        def visit_FunctionDef(self, node):
            # Keep if focused
            if node.name in focussed:
                return node

            # Prune body
            # Create a localized docstring if present, else just pass
            new_body = []
            if ast.get_docstring(node):
                # Keep docstring
                new_body.append(node.body[0])

            # Add ellipsis
            new_body.append(ast.parse("... # [Body omitted]").body[0])

            node.body = new_body
            return node

        def visit_ClassDef(self, node):
            # If class itself is focused, we might want to keep its structure or methods?
            # The prompt says: "If focus_nodes ... include their full source code bodies."
            # If a class is focused, we probably keep it entirely.
            if node.name in focussed:
                return node

            # If not focused, we visit children (methods) to see if *they* are focused
            # But we also skeletonize the class body itself

            # Check if any child method is focused
            has_focused_child = False
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if child.name in focussed:
                        has_focused_child = True
                        break

            # If strict class skeleton:
            if not has_focused_child:
                # Just keep docstring and ellipsis?
                # Usually for classes we want to see method signatures at least.
                # "Skeletonized version... keep only function signatures"
                # So we should recurse but enforce pruning on methods.
                self.generic_visit(node)
                return node

            # If it has a focused child, we still visit to prune non-focused siblings
            self.generic_visit(node)
            return node

        def visit_AsyncFunctionDef(self, node):
            return self.visit_FunctionDef(node)

    transformer = Skeletonizer()
    new_tree = transformer.visit(tree)

    try:
        # ast.unparse available in Python 3.9+
        return ast.unparse(new_tree)
    except AttributeError:
        # Fallback for older python if needed, but RFSN is modern
        return "# Error: AST unparse requires Python 3.9+"


def build_repo_tree(
    root: str,
    max_files: int = 200,
    _hidden_prefix: str = ".",
) -> str:
    """Return a newline-separated list of relative file paths under *root*.

    Hidden directories (starting with ``_hidden_prefix``) are skipped.
    Limited to *max_files* file entries.

    Returns a string where each non-empty line is a relative file path.
    """
    lines: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip hidden directories in-place.
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(_hidden_prefix)
        )
        for fname in sorted(filenames):
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, root)
            lines.append(rel)
            if len(lines) >= max_files:
                return "\n".join(lines)
    return "\n".join(lines)


def locate_files(llm_response: str) -> List[str]:
    """Parse an LLM response and extract file path references.

    Handles several common formats:
    * JSON array: ``["a.py", "b.py"]``
    * JSON object with ``"files"`` key: ``{"files": ["a.py"]}``
    * JSON inside a fenced code block
    * Markdown bullet list: ``- src/foo.py``
    * Numbered list with optional backticks: ``1. src/foo.py``

    Returns a deduplicated list of paths in order of first appearance.
    """
    import json
    import re

    text = llm_response.strip()

    # Strip fenced code block wrapper if present.
    code_block_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if code_block_match:
        text = code_block_match.group(1).strip()

    # Try JSON parse first.
    try:
        data = json.loads(text)
        if isinstance(data, list):
            paths = [str(p) for p in data if str(p).endswith((".py", ".js", ".ts", ".go", ".java", ".rs", ".c", ".cpp", ".h", ".md", ".yaml", ".yml", ".toml", ".txt"))]
            # If no extension match, accept all strings that look like paths.
            if not paths:
                paths = [str(p) for p in data if "/" in str(p) or "." in str(p)]
            seen: dict = {}
            return [p for p in paths if not (p in seen or seen.update({p: True}))]  # type: ignore[func-returns-value]
        if isinstance(data, dict):
            files = data.get("files", [])
            if isinstance(files, list):
                seen2: dict = {}
                return [str(f) for f in files if not (str(f) in seen2 or seen2.update({str(f): True}))]  # type: ignore[func-returns-value]
    except (json.JSONDecodeError, ValueError):
        pass

    # Markdown / numbered list extraction.
    paths_found: List[str] = []
    seen_set: set = set()
    # Match lines like: - path, * path, 1. path, 1. `path`
    for line in llm_response.splitlines():
        m = re.match(r"^\s*[-*]?\s*\d*\.?\s*`?([^\s`]+(?:\.[a-zA-Z0-9]+|/[^\s`]*))`?\s*$", line)
        if m:
            candidate = m.group(1).strip("`").strip()
            if ("/" in candidate or "." in candidate) and candidate not in seen_set:
                paths_found.append(candidate)
                seen_set.add(candidate)

    return paths_found


def read_file_context(
    root: str,
    files: List[str],
    max_total_chars: int = 65536,
    max_chars_per_file: Optional[int] = None,
) -> str:
    """Read *files* from *root* and return a formatted context string.

    Each file is rendered as:
    ``## File: <path>``
    followed by numbered lines (``   N | content``).

    Missing files render as ``## File: <path>\n(file not found)\n``.
    Per-file output is capped at *max_chars_per_file* (if given), and total
    output is capped at *max_total_chars*.
    """
    parts: List[str] = []
    total = 0
    root_real = os.path.realpath(root)
    for relpath in files:
        header = f"## File: {relpath}\n"
        if os.path.isabs(relpath):
            block = header + "(file not found)\n"
        else:
            full = os.path.realpath(os.path.join(root_real, relpath))
            if os.path.commonpath([root_real, full]) != root_real:
                block = header + "(file not found)\n"
            elif not os.path.isfile(full):
                block = header + "(file not found)\n"
            else:
                try:
                    with open(full, "r", encoding="utf-8", errors="replace") as fh:
                        raw_lines = fh.readlines()
                    numbered = "".join(
                        f"{i + 1:4d} | {line}" for i, line in enumerate(raw_lines)
                    )
                    if max_chars_per_file is not None and len(numbered) > max_chars_per_file:
                        numbered = numbered[:max_chars_per_file]
                    block = header + numbered
                except OSError as exc:
                    block = header + f"(error reading file: {exc})\n"
        if total + len(block) > max_total_chars:
            block = block[: max_total_chars - total]
        parts.append(block)
        total += len(block)
        if total >= max_total_chars:
            break
    return "\n".join(parts)
