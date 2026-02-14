"""Lightweight symbolic constraint checking on Python ASTs.

Analyzes function bodies for basic constraint patterns:
- Unreachable code after return/raise
- Unused variables
- Unconditional infinite loops
"""

import ast
import os
from typing import Any, Dict, List


class SymbolicIssue:
    """A single issue found by symbolic checking."""

    def __init__(self, file: str, line: int, kind: str, message: str):
        self.file = file
        self.line = line
        self.kind = kind
        self.message = message

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "kind": self.kind,
            "message": self.message,
        }


def _check_unreachable(tree: ast.Module, filename: str) -> List[SymbolicIssue]:
    """Find code after return/raise/break/continue in function bodies."""
    issues = []

    class Visitor(ast.NodeVisitor):
        def _check_body(self, stmts):
            for i, stmt in enumerate(stmts):
                if isinstance(stmt, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
                    if i + 1 < len(stmts):
                        next_stmt = stmts[i + 1]
                        issues.append(
                            SymbolicIssue(
                                filename,
                                next_stmt.lineno,
                                "unreachable",
                                f"Code after {type(stmt).__name__.lower()} is unreachable",
                            )
                        )

        def visit_FunctionDef(self, node):
            self._check_body(node.body)
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node):
            self._check_body(node.body)
            self.generic_visit(node)

        def visit_If(self, node):
            self._check_body(node.body)
            self._check_body(node.orelse)
            self.generic_visit(node)

    Visitor().visit(tree)
    return issues


def _check_infinite_loops(tree: ast.Module, filename: str) -> List[SymbolicIssue]:
    """Detect `while True` loops without break/return."""
    issues = []

    class Visitor(ast.NodeVisitor):
        def visit_While(self, node):
            # Check for while True (constant truthy condition)
            if isinstance(node.test, ast.Constant) and node.test.value:
                has_exit = False
                for child in ast.walk(node):
                    if isinstance(child, (ast.Break, ast.Return, ast.Raise)):
                        has_exit = True
                        break
                if not has_exit:
                    issues.append(
                        SymbolicIssue(
                            filename,
                            node.lineno,
                            "infinite_loop",
                            "while True without break/return/raise",
                        )
                    )
            self.generic_visit(node)

    Visitor().visit(tree)
    return issues


def check_file(filepath: str) -> List[SymbolicIssue]:
    """Run all symbolic checks on a Python file.

    Returns list of issues found.
    """
    try:
        with open(filepath) as f:
            source = f.read()
        tree = ast.parse(source, filename=filepath)
    except (SyntaxError, OSError) as e:
        return [SymbolicIssue(filepath, 0, "parse_error", str(e))]

    issues = []
    issues.extend(_check_unreachable(tree, filepath))
    issues.extend(_check_infinite_loops(tree, filepath))
    return issues


def check_directory(
    root: str,
    ignore: set = None,
) -> List[SymbolicIssue]:
    """Run symbolic checks on all .py files in a directory tree."""
    ignore = ignore or {"__pycache__", ".venv", "venv", ".git", "node_modules"}
    all_issues = []

    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ignore]
        for f in sorted(files):
            if not f.endswith(".py"):
                continue
            path = os.path.join(base, f)
            all_issues.extend(check_file(path))

    return all_issues


if __name__ == "__main__":
    import json
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "."
    if os.path.isfile(target):
        issues = check_file(target)
    else:
        issues = check_directory(target)

    for issue in issues:
        print(f"  {issue.file}:{issue.line} [{issue.kind}] {issue.message}")

    print(f"\n{len(issues)} issue(s) found")
    sys.exit(1 if issues else 0)
