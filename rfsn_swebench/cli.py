"""CLI entry-point for running SWE-bench-style tasks.

Usage
-----
    python -m rfsn_swebench.cli \\
        --task task.json \\
        --out result.json \\
        [--replay-base /tmp/replays] \\
        [--proposer orchestrator|direct|placeholder] \\
        [--orchestrator-url http://localhost:8000] \\
        [--executor-url http://localhost:8003] \\
        [--ledger-path /data/kernel_ledger.jsonl] \\
        [--data-dir /data]
"""

from __future__ import annotations

import argparse
import json
import os
import re
from typing import Optional

import requests  # type: ignore[import-untyped]

try:
    import jsonschema  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover — optional for minimal installs
    jsonschema = None  # type: ignore[assignment]

from .contracts import BenchTask, TaskCommands, TaskHints, TaskLimits
from .runner import bench_run
from .util import read_text, write_json


# ---------------------------------------------------------------------------
# Task schema path (shared with bundle_schema.json)
# ---------------------------------------------------------------------------
_TASK_SCHEMA_SEARCH_PATHS = [
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "shared",
        "task_schema.json",
    ),
    "/shared/task_schema.json",  # Docker container mount
]


def _load_task_schema() -> Optional[dict]:
    for p in _TASK_SCHEMA_SEARCH_PATHS:
        p = os.path.normpath(p)
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    return None


# ---------------------------------------------------------------------------
# Task loader (with optional schema validation)
# ---------------------------------------------------------------------------


def load_task(path: str) -> BenchTask:
    with open(path, "r", encoding="utf-8") as f:
        j = json.load(f)

    # Validate against JSON Schema when jsonschema is available
    if jsonschema is not None:
        schema = _load_task_schema()
        if schema is not None:
            jsonschema.validate(instance=j, schema=schema)

    hints_data = j.get("hints", {})
    hints = TaskHints(**hints_data)
    commands = TaskCommands(**j.get("commands", {}))
    limits = TaskLimits(**j.get("limits", {}))
    return BenchTask(
        task_id=j["task_id"],
        repo_url=j["repo_url"],
        repo_ref=j.get("repo_ref"),
        workdir=j["workdir"],
        issue_text=j["issue_text"],
        hints=hints,
        commands=commands,
        limits=limits,
    )


# ---------------------------------------------------------------------------
# Proposer implementations
# ---------------------------------------------------------------------------


def make_orchestrator_proposer(
    orchestrator_url: str,
    *,
    data_dir: str = "/data",
    scenario: str = "swebench",
):
    """Return a proposer callback that delegates to the RFSN Orchestrator.

    The Orchestrator's ``/run`` endpoint drives its own LLM → Kernel →
    Tool-Gateway → Executor loop and returns results including any
    ``apply_patch`` step output.  We extract the final unified diff from
    the results.
    """

    def _proposer(task: BenchTask, replay_dir: str) -> str:
        repo_id = re.sub(r"[^A-Za-z0-9_.-]", "_", task.task_id)

        # Ensure the repo is symlinked / copied into /data/repos/<repo_id>
        # so the Executor can find it.
        repo_link = os.path.join(data_dir, "repos", repo_id)
        if not os.path.exists(repo_link):
            os.makedirs(os.path.dirname(repo_link), exist_ok=True)
            os.symlink(os.path.abspath(task.workdir), repo_link)

        payload = {
            "repo_id": repo_id,
            "task": task.issue_text,
            "max_iters": task.limits.max_iters,
            "scenario": scenario,
        }
        headers: dict[str, str] = {}
        tok = os.environ.get("RFSN_SERVICE_TOKEN", "")
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        r = requests.post(
            f"{orchestrator_url}/run",
            json=payload,
            headers=headers,
            timeout=600,
        )
        r.raise_for_status()
        data = r.json()

        # Extract the patch from the orchestrator results.
        # The orchestrator returns results with apply_patch step output.
        patch = ""
        for res in data.get("results", []):
            step = res.get("step", {})
            if step.get("type") == "apply_patch":
                patch = step.get("patch", "")
                break

        if not patch:
            raise RuntimeError(
                f"Orchestrator returned no patch (status={data.get('status')})"
            )
        return patch

    return _proposer


def make_direct_proposer(
    *,
    api_key: Optional[str] = None,
    base_url: str = "https://api.deepseek.com",
    model: str = "deepseek-chat",
    outcome_memory_path: Optional[str] = None,
):
    """Return a two-stage agentic proposer using any OpenAI-compatible API.

    **Stage 1 (Locate)**: Identify files to modify using repo tree + issue.
    **Stage 2 (Patch)**: Generate full file content with real code context,
    then extract the patch via ``git diff`` (Apply-and-Diff).

    Supports any OpenAI-compatible API (DeepSeek, OpenAI, Anthropic via
    proxy, Gemini, local models, etc.) through ``base_url`` and ``model``.

    Env-var resolution order for API key:
      1. Explicit *api_key* kwarg
      2. ``DEEPSEEK_API_KEY``
      3. ``OPENAI_API_KEY``
      4. ``LLM_API_KEY``
    """
    resolved_key = (
        api_key
        or os.environ.get("DEEPSEEK_API_KEY", "")
        or os.environ.get("OPENAI_API_KEY", "")
        or os.environ.get("LLM_API_KEY", "")
    )
    if not resolved_key:
        raise RuntimeError(
            "Proposer requires an API key. Set DEEPSEEK_API_KEY, "
            "OPENAI_API_KEY, LLM_API_KEY, or pass --api-key."
        )

    from .locator import build_repo_tree, locate_files, read_file_context

    # ------------------------------------------------------------------
    # System prompts
    # ------------------------------------------------------------------
    _LOCATE_SYSTEM = (
        "You are a code repair agent performing file localization.\n"
        "Given a bug report and a repository file listing, identify the "
        "1-5 source files MOST LIKELY to require changes to fix the bug.\n"
        "Focus on SOURCE files, not test files.\n"
        "Output ONLY a JSON array of relative file paths, e.g.:\n"
        '["src/module/foo.py", "src/utils/bar.py"]\n'
        "No explanation, no markdown — just the JSON array.\n"
    )

    _PATCH_SYSTEM = (
        "You are an expert code repair agent. Your task is to fix a bug "
        "in a software project.\n\n"
        "## Instructions\n"
        "1. **Analyze** the bug report and understand the root cause.\n"
        "2. **Study** the provided source code and test expectations.\n"
        "3. **Plan** your fix — explain WHY the bug occurs and HOW "
        "you will fix it (2-3 sentences).\n"
        "4. **Output** the COMPLETE modified file content for EACH file "
        "you need to change.\n\n"
        "## Output Format\n"
        "For each file you modify, output:\n"
        "```\n"
        "### FILE: path/to/file.py\n"
        "```python\n"
        "<entire file content with your fix applied>\n"
        "```\n"
        "```\n\n"
        "## Rules\n"
        "- Output the COMPLETE file — every line, not just the changed "
        "parts. The system will diff it against the original.\n"
        "- Only modify files that need changes. Do NOT modify test files.\n"
        "- Do NOT skip/xfail tests or modify CI configs.\n"
        "- Keep changes minimal and surgical.\n"
        "- If you modify multiple files, output each with its own "
        "### FILE: header.\n"
    )

    _RETRY_SYSTEM = (
        "You are an expert code repair agent. Your previous fix attempt "
        "FAILED the test suite. You will be shown the test errors.\n\n"
        "## Instructions\n"
        "1. **Analyze** the test failure output to understand WHAT went "
        "wrong with your previous attempt.\n"
        "2. **Diagnose** the remaining issue — is it a logic error, "
        "edge case, wrong variable, or incomplete fix?\n"
        "3. **Output** the corrected COMPLETE file content.\n\n"
        "## Output Format\n"
        "Same as before — for each file:\n"
        "### FILE: path/to/file.py\n"
        "```python\n"
        "<entire corrected file content>\n"
        "```\n\n"
        "## Rules\n"
        "- Output the COMPLETE file content, not a diff.\n"
        "- Only modify source files, never test files.\n"
        "- Learn from the error output to make the RIGHT fix this time.\n"
    )

    # ------------------------------------------------------------------
    # LLM call helper
    # ------------------------------------------------------------------
    def _llm_call(
        system: str,
        user: str,
        *,
        max_tokens: int = 16384,
        temperature: float = 0.0,
    ) -> str:
        """Make a single chat completion call."""
        payload: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        # Reasoner models don't support temperature/max_tokens
        # Gemini 3 has always-on reasoning
        is_reasoner = "reasoner" in model or "gemini-3" in model
        if not is_reasoner:
            payload["temperature"] = temperature
            payload["max_tokens"] = max_tokens

        r = requests.post(
            f"{base_url.rstrip('/')}/chat/completions",
            json=payload,
            headers={
                "Authorization": f"Bearer {resolved_key}",
                "Content-Type": "application/json",
            },
            timeout=600,
        )
        r.raise_for_status()
        data = r.json()
        msg = data["choices"][0]["message"]
        raw = msg.get("content") or ""

        # Reasoner models: content may be empty, reasoning_content has answer
        if not raw.strip():
            reasoning = msg.get("reasoning_content", "")
            if reasoning:
                raw = reasoning

        return raw

    # ------------------------------------------------------------------
    # Apply-and-Diff: write full files, extract diff via git
    # ------------------------------------------------------------------
    def _extract_file_blocks(text: str) -> dict[str, str]:
        """Parse ``### FILE: path`` blocks from LLM output.

        Returns {relative_path: file_content}.
        """
        blocks: dict[str, str] = {}
        # Pattern: ### FILE: path/to/file.py followed by ```python ... ```
        pattern = re.compile(
            r"###\s*FILE:\s*(.+?)\s*\n" r"```(?:python|py)?\s*\n" r"(.*?)" r"```",
            re.DOTALL,
        )
        for m in pattern.finditer(text):
            path = m.group(1).strip().strip("`")
            content = m.group(2)
            if path and content:
                blocks[path] = content

        # Fallback: if no ### FILE: blocks found, try to extract a single
        # code block and match it to the first focus file
        if not blocks:
            code_blocks = re.findall(
                r"```(?:python|py)?\s*\n(.*?)```",
                text,
                re.DOTALL,
            )
            if code_blocks:
                # Use the largest code block as the primary fix
                largest = max(code_blocks, key=len)
                if len(largest.strip()) > 50:
                    blocks["__single__"] = largest

        return blocks

    def _apply_and_diff(
        workdir: str,
        file_blocks: dict[str, str],
        focus_files: list[str],
    ) -> str:
        """Write LLM-generated file content to disk, then extract
        the diff via ``git diff``.

        This ensures patches are always syntactically valid.
        """
        from .repo import git_diff_unified

        written_any = False
        for path, content in file_blocks.items():
            if path == "__single__" and focus_files:
                # Map the single block to the first focus file
                path = focus_files[0]

            abs_path = os.path.join(workdir, path)
            if not os.path.isfile(abs_path):
                # Don't create new files for paths we can't verify
                continue

            try:
                with open(abs_path, "w", encoding="utf-8") as f:
                    f.write(content)
                written_any = True
            except Exception:
                continue

        if not written_any:
            return ""

        # Extract the diff from git
        return git_diff_unified(workdir)

    # ------------------------------------------------------------------
    # Legacy diff extraction (fallback)
    # ------------------------------------------------------------------
    def _extract_diff(text: str) -> str:
        """Pull a unified diff from LLM output (fallback path)."""
        blocks = re.findall(
            r"```(?:diff)?\s*\n(.*?)```",
            text,
            re.DOTALL,
        )
        if blocks:
            for block in reversed(blocks):
                if "--- a/" in block and "+++ b/" in block:
                    return block.strip() + "\n"
            return "\n".join(b.strip() for b in blocks) + "\n"

        diff_lines: list[str] = []
        in_hunk = False
        for line in text.splitlines():
            stripped = line.rstrip()
            if stripped.startswith("diff --git "):
                in_hunk = True
                diff_lines.append(stripped)
            elif stripped.startswith("--- a/") or stripped.startswith("+++ b/"):
                in_hunk = True
                diff_lines.append(stripped)
            elif stripped.startswith("@@ ") and "@@" in stripped[3:]:
                in_hunk = True
                diff_lines.append(stripped)
            elif in_hunk and (
                stripped.startswith("+")
                or stripped.startswith("-")
                or stripped.startswith(" ")
                or stripped == ""
            ):
                diff_lines.append(line.rstrip())
            else:
                if not (in_hunk and not stripped):
                    in_hunk = False

        if diff_lines:
            return "\n".join(diff_lines) + "\n"
        return text.strip() + "\n"

    # ------------------------------------------------------------------
    # Main proposer callback
    # ------------------------------------------------------------------
    _cached_repo_tree: str | None = None
    _cached_target_files: list[str] | None = None

    def _format_feedback(feedback: list[dict]) -> str:
        """Format structured iteration feedback for the LLM."""
        parts = ["## Previous Attempts (learn from these)\n"]
        for fb in feedback:
            iter_n = fb.get("iter", "?")
            fb_type = fb.get("type", "unknown")
            if fb_type == "gate_reject":
                reasons = ", ".join(fb.get("reasons", []))
                parts.append(
                    f"**Iteration {iter_n} — GATE REJECTED**: {reasons}\n"
                    f"Your patch was blocked by the safety gate. "
                    f"Avoid: {reasons}\n"
                )
            elif fb_type == "apply_fail":
                err = fb.get("error", "unknown error")[:300]
                parts.append(
                    f"**Iteration {iter_n} — PATCH APPLY FAILED**: {err}\n"
                    f"Your diff was malformed. Output the COMPLETE file "
                    f"content, not a partial diff.\n"
                )
            elif fb_type == "test_fail":
                stdout = fb.get("stdout_tail", "")[:2000]
                stderr = fb.get("stderr_tail", "")[:500]
                parts.append(
                    f"**Iteration {iter_n} — TESTS FAILED**:\n" f"```\n{stdout}\n```\n"
                )
                if stderr.strip():
                    parts.append(f"Stderr:\n```\n{stderr}\n```\n")
            elif fb_type == "propose_error":
                err = fb.get("error", "unknown")[:300]
                parts.append(f"**Iteration {iter_n} — PROPOSER ERROR**: {err}\n")
        return "\n".join(parts)

    def _proposer(task: BenchTask, replay_dir: str) -> str:
        from .replay import log_event
        import glob

        # Closure-level caches — persist across iterations
        nonlocal _cached_repo_tree, _cached_target_files

        log_event(
            replay_dir,
            {
                "type": "direct_llm_request",
                "model": model,
                "strategy": "two_stage_apply_and_diff",
            },
        )

        # --- Build repo tree (cached across iterations) ---
        if _cached_repo_tree is None:
            _cached_repo_tree = build_repo_tree(task.workdir, max_files=200)
        repo_tree = _cached_repo_tree

        # --- Load structured feedback from ALL previous iterations ---
        feedback_path = os.path.join(replay_dir, "feedback.json")
        iter_feedback: list[dict] = []
        if os.path.isfile(feedback_path):
            try:
                import json as _json

                with open(feedback_path, "r", encoding="utf-8") as f:
                    iter_feedback = _json.load(f)
            except Exception:
                pass

        # Legacy blob fallback (in case feedback.json not yet written)
        last_test_output = ""
        last_test_stderr = ""
        if not iter_feedback:
            stdout_blobs = sorted(
                glob.glob(os.path.join(replay_dir, "blobs", "quick_stdout_*")),
            )
            if stdout_blobs:
                last_test_output = read_text(stdout_blobs[-1])[-4000:]

            stderr_blobs = sorted(
                glob.glob(os.path.join(replay_dir, "blobs", "quick_stderr_*")),
            )
            if stderr_blobs:
                last_test_stderr = read_text(stderr_blobs[-1])[-2000:]

        is_retry = bool(
            iter_feedback or last_test_output.strip() or last_test_stderr.strip()
        )

        # --- Determine files to read (cached across iterations) ---
        if _cached_target_files is not None:
            target_files = _cached_target_files
        else:
            # Start with hint focus_files (from gold patch in SWE-bench)
            target_files = list(task.hints.focus_files or [])
        # If no target files cached or provided, run Stage 1: Locate
        if not target_files:
            locate_prompt = (
                f"## Bug Report\n{task.issue_text}\n\n"
                f"## Repository File Listing\n```\n{repo_tree}\n```\n"
            )
            if task.hints.failing_tests:
                locate_prompt += (
                    "\n## Known Failing Tests\n"
                    + "\n".join(task.hints.failing_tests)
                    + "\n"
                )

            log_event(replay_dir, {"type": "locate_start"})
            locate_response = _llm_call(
                _LOCATE_SYSTEM,
                locate_prompt,
                max_tokens=1024,
                temperature=0.0,
            )
            target_files = locate_files(locate_response)
            log_event(
                replay_dir,
                {
                    "type": "locate_result",
                    "files": target_files,
                },
            )

            if not target_files:
                # Absolute fallback: try to find Python files mentioned
                # in the issue text
                mentioned = re.findall(
                    r"([A-Za-z0-9_/]+\.py)\b",
                    task.issue_text,
                )
                target_files = mentioned[:3] if mentioned else []

            # Cache for subsequent iterations
            _cached_target_files = target_files

        # --- Read file contents ---
        file_context = read_file_context(
            task.workdir,
            target_files,
            max_chars_per_file=15000,
            max_total_chars=60000,
        )

        # --- Read test file contents ---
        test_context = ""
        if task.hints.failing_tests:
            test_files_seen: set[str] = set()
            test_paths: list[str] = []
            for tnode in task.hints.failing_tests:
                tfile = tnode.split("::")[0]
                if tfile not in test_files_seen and tfile.endswith(".py"):
                    test_files_seen.add(tfile)
                    test_paths.append(tfile)
            if test_paths:
                test_context = read_file_context(
                    task.workdir,
                    test_paths,
                    max_chars_per_file=8000,
                    max_total_chars=16000,
                )

        # --- Build Stage 2 prompt ---
        user_parts = [
            f"## Bug Report\n{task.issue_text}\n",
        ]

        if task.hints.failing_tests:
            user_parts.append(
                "## Known Failing Tests\n" + "\n".join(task.hints.failing_tests) + "\n"
            )

        if repo_tree:
            user_parts.append(
                f"## Repository Structure\n```\n{repo_tree[:3000]}\n```\n"
            )

        if file_context:
            user_parts.append(f"## Source Code\n{file_context}\n")

        if test_context:
            user_parts.append(
                f"## Test Code (read-only — do NOT modify)\n" f"{test_context}\n"
            )

        # Retry: include structured feedback from ALL previous attempts
        if is_retry:
            if iter_feedback:
                user_parts.append(_format_feedback(iter_feedback))
            else:
                # Legacy path — blob-based feedback
                if last_test_output.strip():
                    user_parts.append(
                        f"## Previous Test Failure Output\n"
                        f"```\n{last_test_output}\n```\n"
                    )
                if last_test_stderr.strip():
                    user_parts.append(
                        f"## Previous Test Stderr\n" f"```\n{last_test_stderr}\n```\n"
                    )

        # Cross-task learnings from outcome memory
        if outcome_memory_path:
            from .outcome_memory import OutcomeMemory

            mem = OutcomeMemory(outcome_memory_path)
            # Extract repo family from task_id (e.g. "django__django" from "django__django-11049")
            repo = (
                task.task_id.rsplit("-", 1)[0] if "-" in task.task_id else task.task_id
            )
            learnings = mem.format_learnings(repo)
            if learnings:
                user_parts.append(f"## Learnings from Past Tasks\n{learnings}\n")

        system_prompt = _RETRY_SYSTEM if is_retry else _PATCH_SYSTEM
        user_prompt = "\n".join(user_parts)

        # --- Stage 2: Generate fix ---
        log_event(
            replay_dir,
            {
                "type": "patch_start",
                "is_retry": is_retry,
                "target_files": target_files,
            },
        )

        raw_response = _llm_call(
            system_prompt,
            user_prompt,
            max_tokens=16384,
            temperature=0.2 if is_retry else 0.0,
        )

        # --- Try Apply-and-Diff first ---
        file_blocks = _extract_file_blocks(raw_response)
        if file_blocks:
            log_event(
                replay_dir,
                {
                    "type": "apply_and_diff",
                    "file_count": len(file_blocks),
                    "files": list(file_blocks.keys()),
                },
            )
            diff = _apply_and_diff(
                task.workdir,
                file_blocks,
                target_files,
            )
            if diff.strip():
                return diff

        # --- Fallback: extract diff directly from response ---
        log_event(replay_dir, {"type": "fallback_diff_extract"})
        return _extract_diff(raw_response)

    return _proposer


def _placeholder_proposer(task: BenchTask, replay_dir: str) -> str:
    raise RuntimeError(
        "No proposer wired. Use --proposer orchestrator or --proposer direct. "
        "See 'python -m rfsn_swebench.cli --help'."
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(
        description="SWE-bench-style bench runner for RFSN Agent",
    )
    ap.add_argument("--task", required=True, help="Path to task.json")
    ap.add_argument("--out", required=True, help="Path to write result.json")
    ap.add_argument(
        "--replay-base",
        default=None,
        help="Base directory for replay artifacts (default: workdir)",
    )
    ap.add_argument(
        "--proposer",
        choices=["orchestrator", "direct", "placeholder"],
        default=None,
        help="Proposer strategy: 'orchestrator' (full RFSN stack), "
        "'direct' (DeepSeek API), 'placeholder' (abort). "
        "Default: auto-detect (orchestrator if --orchestrator-url given, "
        "direct if DEEPSEEK_API_KEY set, else placeholder).",
    )
    ap.add_argument(
        "--orchestrator-url",
        default=None,
        help="RFSN Orchestrator URL (e.g. http://localhost:8000). "
        "When set, patches are proposed by the full RFSN stack.",
    )
    ap.add_argument(
        "--executor-url",
        default=None,
        help="RFSN Executor URL (e.g. http://localhost:8003). "
        "When set, test execution is delegated to the sandboxed executor "
        "instead of running locally via subprocess.",
    )
    ap.add_argument(
        "--gateway-url",
        default=None,
        help="RFSN Tool Gateway URL (e.g. http://localhost:8002). "
        "When set alongside --executor-url, test execution "
        "routes through the tool gateway for policy "
        "enforcement before reaching the executor.",
    )
    ap.add_argument(
        "--ledger-path",
        default=None,
        help="Path to hash-chained ledger JSONL for integrated mode",
    )
    ap.add_argument(
        "--data-dir",
        default="/data",
        help="Shared data directory (repos/venv/artifacts)",
    )
    ap.add_argument(
        "--scenario",
        default="swebench",
        help="Scenario tag for the orchestrator cassette system",
    )
    ap.add_argument(
        "--api-key",
        default=None,
        help="LLM API key (default: $DEEPSEEK_API_KEY) for --proposer direct",
    )
    ap.add_argument(
        "--model",
        default="deepseek-chat",
        help="Model name for --proposer direct (default: deepseek-chat)",
    )
    ap.add_argument(
        "--base-url",
        default="https://api.deepseek.com",
        help="LLM API base URL for --proposer direct",
    )
    ap.add_argument(
        "--outcome-memory",
        default=None,
        help="Path to outcome memory JSONL file for cross-task learning",
    )

    args = ap.parse_args(argv)
    task = load_task(args.task)

    # Choose proposer
    proposer_choice = args.proposer
    if proposer_choice is None:
        # Auto-detect
        if args.orchestrator_url:
            proposer_choice = "orchestrator"
        elif os.environ.get("DEEPSEEK_API_KEY") or args.api_key:
            proposer_choice = "direct"
        else:
            proposer_choice = "placeholder"

    if proposer_choice == "orchestrator":
        url = args.orchestrator_url or os.environ.get(
            "ORCHESTRATOR_URL", "http://localhost:8000"
        )
        proposer = make_orchestrator_proposer(
            url,
            data_dir=args.data_dir,
            scenario=args.scenario,
        )
    elif proposer_choice == "direct":
        proposer = make_direct_proposer(
            api_key=args.api_key,
            base_url=args.base_url,
            model=args.model,
            outcome_memory_path=args.outcome_memory,
        )
    else:
        proposer = _placeholder_proposer

    try:
        res = bench_run(
            task,
            proposer,
            replay_base=args.replay_base,
            ledger_path=args.ledger_path,
            executor_url=args.executor_url,
            gateway_url=args.gateway_url,
        )
        write_json(args.out, res.to_dict())
        print(json.dumps(res.to_dict(), indent=2))
    except Exception as e:
        write_json(
            args.out,
            {"task_id": task.task_id, "status": "ABORT", "error": str(e)},
        )
        raise


if __name__ == "__main__":
    main()
