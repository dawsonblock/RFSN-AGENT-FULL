"""Local in-process executor for integration tests and dev mode.

Bridges the orchestrator's step-dict format to ``dispatch_tool`` so that
end-to-end repair tests can run without Docker or HTTP services.

Usage
-----
Patch ``services.orchestrator.executor_client.run_step`` with
``make_local_run_step(workspace_root)`` in test fixtures::

    from rfsn_kernel.local_executor import make_local_run_step

    monkeypatch.setattr(
        "services.orchestrator.executor_client.run_step",
        make_local_run_step(str(tmp_path / "repo")),
    )

The returned callable has the same signature as ``executor_client.run_step``
and returns the same ``{"status": int, "logs": str, ...}`` format.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from rfsn_kernel.dispatcher import ExecutionContext, dispatch_tool


def _step_to_tool_args(step: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    """Map a step dict to (tool_name, args) for ``dispatch_tool``.

    Handles both the canonical registry format (``type`` key) and the
    executor service's ``step`` envelope format.
    """
    tool_name = step.get("type", "")
    # Pass through all keys except ``type`` as tool args.
    args = {k: v for k, v in step.items() if k != "type"}
    return tool_name, args


def execute_step_locally(
    step: Dict[str, Any],
    workspace_root: str,
    *,
    run_id: str = "",
    iter_count: int = 0,
) -> Dict[str, Any]:
    """Execute *step* locally using ``dispatch_tool``.

    Returns a dict shaped like the executor service response::

        {"status": 0, "logs": "...", "seconds": 0.12, "ok": True}

    ``status == 0`` means success.
    """
    import time
    tool_name, args = _step_to_tool_args(step)
    ctx = ExecutionContext(
        workspace_root=workspace_root,
        run_id=run_id,
        iter_count=iter_count,
        sandbox_mode="local_dev",
        dev_mode=True,   # Required for real patch + test execution.
    )
    t0 = time.monotonic()
    result = dispatch_tool(tool_name, args, ctx)
    elapsed = time.monotonic() - t0

    logs = "\n".join(filter(None, [result.output, result.stdout, result.stderr, result.error]))
    return {
        "status": 0 if result.success else 1,
        "logs": logs,
        "seconds": elapsed,
        "ok": result.success,
        "files_changed": result.files_changed,
        "tool": result.tool,
        "error": result.error or "",
    }


def make_local_run_step(workspace_root: str):
    """Return a ``run_step`` replacement that executes steps locally.

    The returned function has the same signature as
    ``executor_client.run_step`` and delegates to ``execute_step_locally``.
    """
    def _local_run_step(
        repo_id: str,
        it: int,
        step: Dict[str, Any],
        run_id: Optional[str] = None,
        tier: Optional[int] = None,
        warm_sandbox: Optional[bool] = None,
    ) -> Dict[str, Any]:
        return execute_step_locally(
            step,
            workspace_root=workspace_root,
            run_id=run_id or "",
            iter_count=it,
        )

    return _local_run_step
