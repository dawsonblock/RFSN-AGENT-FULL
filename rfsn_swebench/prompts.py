"""Dynamic prompt templates for RFSN SWE-bench.

Centralizes all system and user prompts to avoid hardcoding strings in logic.
Supports simple {variable} substitution and conditional blocks.
"""

from typing import Any, Dict, Optional


class SimpleTemplate:
    """A lightweight template engine avoiding heavy dependencies."""

    def __init__(self, template_str: str):
        self.template = template_str

    def render(self, **kwargs: Any) -> str:
        """Render template with variable substitution."""
        # 1. Handle simple {if var} blocks (very basic)
        # Only supports boolean checks: {if var} ... {endif}
        # This is a hacky parser, but sufficient for prompts
        out = []
        lines = self.template.split("\n")
        stack = []  # (True/False keep_line)

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("{if ") and stripped.endswith("}"):
                var_name = stripped[4:-1].strip()
                val = kwargs.get(var_name)
                stack.append(bool(val))
                continue
            elif stripped == "{endif}":
                if stack:
                    stack.pop()
                continue

            # If any false in stack, skip line
            if not all(stack):
                continue

            # Variable substitution
            # We use format() but need to be careful about braces
            # So we manually replace known keys
            result_line = line
            for k, v in kwargs.items():
                start_tag = f"{{{k}}}"
                if start_tag in result_line:
                    result_line = result_line.replace(start_tag, str(v))
            out.append(result_line)

        return "\n".join(out)


# ---------------------------------------------------------------------------
# Locator Prompts
# ---------------------------------------------------------------------------

LOCATOR_SYSTEM = """You are a Principal Software Architect analyzing a GitHub issue.
Your goal is to identify ALL files that need to be modified to fix the issue.

Output Format:
Return a strictly valid JSON list of file paths.
Example:
[
    "django/core/handlers/base.py",
    "tests/handlers/test_base.py"
]

Do not include markdown fences ```json ... ```. Just the raw JSON list.
"""

LOCATOR_USER = """
Repo Structure:
{file_list}

Issue Title: {issue_title}
Issue Description:
{issue_body}

Instructions:
1. Analyze the issue to understand the bug or feature.
2. Look at the repo structure to find relevant files.
3. List every file that likely needs modification (including test files).
4. Do not list files that don't exist in the repo structure.

Relevant Files JSON:
"""

# ---------------------------------------------------------------------------
# Patcher Prompts
# ---------------------------------------------------------------------------

PATCHER_SYSTEM = """You are a Staff Engineer at a top tech company.
You are tasked with fixing a specific issue in a Python repository.
You have access to the codebase and the issue description.

Your Guiding Principles:
1. MINIMAL CHANGES directly addressing the issue.
2. PRESERVE existing coding style and logic.
3. FIX correctness first, then style.
4. NO placeholders or "..." - produce full, valid code.

Output Format (Unified Diff):
Return a single standard Unified Diff that applies cleanly with `patch -p1`.
Start with `diff --git a/path b/path`.
"""

PATCHER_USER = """
Issue: {issue_text}

Context Files:
{context_files}

{if retrieval_context}
Retrieval-Augmented Context (snippets relevant to issue):
{retrieval_context}
{endif}

Instructions:
Generate a unified diff to fix the issue described above.
Ensure imports are correct.
"""

# ---------------------------------------------------------------------------
# Verifier Prompts
# ---------------------------------------------------------------------------

VERIFIER_SYSTEM = """You are a QA Lead.
Your job is to analyze the execution result of a patch and determine if it fixed the issue.
"""

VERIFIER_USER = """
Issue: {issue_text}

Patch Applied:
{patch_diff}

Execution Output (Tests/Logs):
{exec_output}

Task:
1. Did the patch apply successfully?
2. Did the relevant tests pass?
3. Are there any new regressions?

Return JSON:
{
    "success": true/false,
    "confidence": 0.0-1.0,
    "reasoning": "..."
}
"""
