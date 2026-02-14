"""Verification loop with Chain-of-Thought reasoning.

When a patch fails tests, instead of blinding retrying, this module
analyzes the failure to produce a "thought" — a structured diagnosis
of WHY it failed. This thought is then fed back into the next iteration.
"""

from typing import Dict, Any, Optional
from .prompts import SimpleTemplate

_VERIFY_SYSTEM = """You are a Senior QA Engineer.
Analyze the provided test failure and the patch that caused it.
Diagnose the root cause.

Output JSON:
{
    "root_cause": "brief explanation",
    "suggested_fix": "what needs to change",
    "confidence": 0.0-1.0
}
"""

_VERIFY_USER = """
Issue: {issue_text}

Patch Applied:
{patch_diff}

Test Output:
{test_output}

Diagnose why the tests failed.
"""


class Verifier:
    def __init__(self, llm_callable):
        self._llm = llm_callable
        self._sys = SimpleTemplate(_VERIFY_SYSTEM)
        self._usr = SimpleTemplate(_VERIFY_USER)

    def analyze_failure(
        self, issue_text: str, patch_diff: str, test_output: str
    ) -> Dict[str, Any]:
        """Produce a structured diagnosis of a test failure."""

        # Simple heuristic: if output is massive, truncate middle
        if len(test_output) > 5000:
            test_output = (
                test_output[:2500] + "\n...[truncated]...\n" + test_output[-2500:]
            )

        prompt = self._usr.render(
            issue_text=issue_text, patch_diff=patch_diff, test_output=test_output
        )

        try:
            raw = self._llm(
                system_prompt=self._sys.render(),
                user_prompt=prompt,
                temperature=0.0,
                json_mode=True,
            )
            # Parse JSON from raw string (assuming LLM returns valid JSON)
            import json

            # Strip markdown fences if present
            raw = raw.strip()
            if raw.startswith("```json"):
                raw = raw[7:]
            if raw.endswith("```"):
                raw = raw[:-3]
            return json.loads(raw)
        except Exception as e:
            return {
                "root_cause": "Analysis failed",
                "suggested_fix": "Retry with simplified approach",
                "confidence": 0.0,
                "error": str(e),
            }
