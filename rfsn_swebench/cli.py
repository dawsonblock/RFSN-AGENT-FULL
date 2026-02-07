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
        [--ledger-path /data/ledger.jsonl] \\
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
        "..", "shared", "task_schema.json",
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
        r = requests.post(
            f"{orchestrator_url}/run", json=payload, timeout=600
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
):
    """Return a proposer that calls DeepSeek (or compatible) API directly.

    Requires the ``DEEPSEEK_API_KEY`` env-var (or *api_key* kwarg).
    """
    resolved_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
    if not resolved_key:
        raise RuntimeError(
            "direct proposer requires DEEPSEEK_API_KEY env-var or --api-key"
        )

    _SYSTEM = (
        "You are a code repair agent.  You MUST output "
        "ONLY a single unified diff patch (git diff format) "
        "inside a ```diff code block and nothing else.\n"
        "The patch must be minimal and only change files "
        "necessary to fix the issue.\n"
        "Do not modify CI configs, dependency files, or "
        "skip/xfail tests.\n"
        "Do not modify test files — only fix the source code.\n"
        "Reproduce the failing tests and fix the root cause.\n"
        "Output format:\n"
        "```diff\n"
        "--- a/path/to/file.py\n"
        "+++ b/path/to/file.py\n"
        "@@ -line,count +line,count @@\n"
        " context\n"
        "-old line\n"
        "+new line\n"
        "```\n"
    )

    def _extract_diff(text: str) -> str:
        """Pull the unified diff out of a chat completion.

        Handles several common LLM output patterns:
        1. Clean diff (just the diff text)
        2. Diff inside a single ```diff ... ``` code block
        3. Multiple code blocks with reasoning text between them
        4. Reasoning text with diff lines interspersed

        Strategy: find ALL lines that look like unified-diff content
        (--- a/, +++ b/, @@ ... @@, context/add/remove lines within hunks)
        and reconstruct a single coherent patch.
        """
        # First, try to extract from fenced code blocks
        import re as _re
        blocks = _re.findall(
            r"```(?:diff)?\s*\n(.*?)```",
            text,
            _re.DOTALL,
        )
        if blocks:
            # Use the LAST complete diff block — LLMs often iterate and
            # the final block is the refined/corrected version.
            # Find the last block that contains actual diff content
            for block in reversed(blocks):
                if "--- a/" in block and "+++ b/" in block:
                    return block.strip() + "\n"
            # If no block has full diff markers, concatenate all
            return "\n".join(b.strip() for b in blocks) + "\n"

        # No code fences — scan for diff-like lines
        diff_lines: list[str] = []
        in_hunk = False
        for line in text.splitlines():
            stripped = line.rstrip()
            if stripped.startswith("diff --git "):
                in_hunk = True
                diff_lines.append(stripped)
            elif (
                stripped.startswith("--- a/")
                or stripped.startswith("+++ b/")
            ):
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
            elif in_hunk and stripped.startswith("diff --git "):
                diff_lines.append(stripped)
            else:
                # Non-diff line — if we were in a hunk, check if it's
                # just a blank separator
                if in_hunk and not stripped:
                    diff_lines.append("")
                else:
                    in_hunk = False

        if diff_lines:
            return "\n".join(diff_lines) + "\n"

        # Fallback: return original text stripped
        return text.strip() + "\n"

    def _proposer(task: BenchTask, replay_dir: str) -> str:
        from .replay import log_event

        # Build context: issue + optional last-failure stderr
        user_parts = [
            f"## Issue\n{task.issue_text}\n",
        ]
        if task.hints.failing_tests:
            tests_str = chr(10).join(
                task.hints.failing_tests
            )
            user_parts.append(
                f"## Known failing tests\n{tests_str}\n"
            )
        if task.hints.focus_files:
            user_parts.append(
                f"## Likely files\n{chr(10).join(task.hints.focus_files)}\n"
            )
            # Include actual file contents so the LLM sees the real code
            for fpath in task.hints.focus_files:
                abs_path = os.path.join(task.workdir, fpath)
                if os.path.isfile(abs_path):
                    try:
                        content = read_text(abs_path)[:8000]
                        user_parts.append(
                            f"## File: {fpath}\n```python\n{content}\n```\n"
                        )
                    except Exception:
                        pass

        # Also include failing test file contents
        if task.hints.failing_tests:
            seen = set()
            for tnode in task.hints.failing_tests:
                tfile = tnode.split("::")[0]
                if tfile in seen:
                    continue
                seen.add(tfile)
                abs_path = os.path.join(task.workdir, tfile)
                if os.path.isfile(abs_path):
                    try:
                        content = read_text(abs_path)[:8000]
                        user_parts.append(
                            f"## File: {tfile}\n```python\n{content}\n```\n"
                        )
                    except Exception:
                        pass

        # Try to include last quick-test failure from replay dir
        import glob

        # Include stdout (pytest writes failure details there, not stderr)
        stdout_blobs = sorted(
            glob.glob(os.path.join(replay_dir, "blobs", "quick_stdout_*"))
        )
        if stdout_blobs:
            last_out = read_text(stdout_blobs[-1])[-3000:]
            user_parts.append(f"## Last test output\n```\n{last_out}\n```\n")

        stderr_blobs = sorted(
            glob.glob(os.path.join(replay_dir, "blobs", "quick_stderr_*"))
        )
        if stderr_blobs:
            last_err = read_text(stderr_blobs[-1])[-2000:]
            if last_err.strip():
                user_parts.append(
                    "## Last test stderr\n"
                    f"```\n{last_err}\n```\n"
                )

        payload: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": "\n".join(user_parts)},
            ],
        }
        # deepseek-reasoner (R1) doesn't support temperature/max_tokens
        if "reasoner" not in model:
            payload["temperature"] = 0.0
            payload["max_tokens"] = 4096

        log_event(replay_dir, {"type": "direct_llm_request", "model": model})

        r = requests.post(
            f"{base_url.rstrip('/')}/chat/completions",
            json=payload,
            headers={
                "Authorization": f"Bearer {resolved_key}",
                "Content-Type": "application/json",
            },
            timeout=300,
        )
        r.raise_for_status()
        data = r.json()
        msg = data["choices"][0]["message"]
        raw = msg.get("content") or ""

        # deepseek-reasoner puts chain-of-thought in reasoning_content
        # and the final answer in content. But sometimes content is empty
        # or just a summary without a diff.
        if raw.strip() and ("--- a/" in raw or "diff --git" in raw):
            # Content has actual diff markers — use it
            pass
        else:
            reasoning = msg.get("reasoning_content", "")
            if reasoning and (
                "--- a/" in reasoning
                or "diff --git" in reasoning
            ):
                raw = reasoning

        return _extract_diff(raw)

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
