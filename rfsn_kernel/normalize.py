"""Proposal normalization — canonicalize planner output.

Converts raw planner JSON into a strict Proposal contract.
No free-form execution config allowed.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict

from rfsn_kernel.state import Proposal
from rfsn_kernel.tool_registry import CANONICAL_TOOLS


# Fields the planner is allowed to set per step type.
# Derived from the canonical tool registry so the two contracts stay in sync.
# Do NOT add entries here directly — edit tool_registry.py instead.
_ALLOWED_PARAMS: Dict[str, set] = {
    name: set(spec.allowed_params)
    for name, spec in CANONICAL_TOOLS.items()
}


def normalize(
    raw_step: Dict[str, Any],
    intent: str = "",
    context_hash: str = "",
    bundle_id: str = "",
) -> Proposal:
    """Normalize a raw step dict into a Proposal.

    Strips unknown fields, enforces type contract,
    computes hashes.
    """
    action = str(raw_step.get("type", ""))
    allowed = _ALLOWED_PARAMS.get(action, set())

    # Strip fields the planner is not allowed to control.
    params: Dict[str, Any] = {}
    for k, v in raw_step.items():
        if k in ("type", "id"):
            continue
        if k in allowed:
            params[k] = v

    # Preserve original step ID if present.
    if "id" in raw_step:
        params["_step_id"] = raw_step["id"]

    # Compute planner output hash.
    planner_blob = json.dumps(
        raw_step, sort_keys=True,
        separators=(",", ":"), default=str,
    )
    planner_hash = hashlib.sha256(
        planner_blob.encode("utf-8"),
    ).hexdigest()[:16]

    return Proposal(
        action=action,
        params=params,
        context_hash=context_hash,
        planner_hash=planner_hash,
        intent=intent,
        bundle_id=bundle_id,
    )


def proposal_to_step(proposal: Proposal) -> Dict[str, Any]:
    """Convert a Proposal back to an executor-compatible step dict."""
    step: Dict[str, Any] = {"type": proposal.action}
    step_id = proposal.params.get("_step_id", "")
    if step_id:
        step["id"] = step_id
    for k, v in proposal.params.items():
        if k.startswith("_"):
            continue
        step[k] = v
    return step
