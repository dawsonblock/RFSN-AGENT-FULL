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
