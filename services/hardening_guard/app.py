"""Self-Healing Hardening Module (SHH).

Detects and repairs hardening drift. Never weakens settings.
Run at orchestrator startup or as a periodic check.

Usage:  python -m services.hardening_guard.app
"""

import json
import os
import sys
import time
from pathlib import Path

STRICT = os.getenv("RFSN_HARDENING_STRICT", "1") == "1"
DEV = os.getenv("RFSN_DEV_MODE", "0") == "1"
STATE_PATH = Path(os.getenv("RFSN_HARDENING_STATE", "/data/hardening_state.json"))


def _fatal(msg: str) -> str:
    """Record a fatal finding. In strict+prod mode, exit."""
    print(f"HARDENING_FATAL: {msg}", flush=True)
    if STRICT and not DEV:
        raise SystemExit(2)
    return msg


def _check_auth() -> tuple:
    repairs, fatals = [], []
    if not Path("/shared/auth.py").exists() and not DEV:
        fatals.append(_fatal("auth module required but /shared/auth.py missing"))
    return repairs, fatals


def _check_patch_gate() -> tuple:
    repairs, fatals = [], []
    try:
        import rfsn_swebench.gate.patch_risk_gate  # noqa: F401
    except Exception:
        if not DEV:
            fatals.append(_fatal("patch_risk_gate required but not importable"))
    return repairs, fatals


def _check_warm_sandbox() -> tuple:
    repairs = []
    if not DEV and os.getenv("RFSN_WARM_SANDBOX", "0") == "1":
        os.environ["RFSN_WARM_SANDBOX"] = "0"
        repairs.append("disabled warm sandbox in prod")
    return repairs, []


def _check_venv_mode() -> tuple:
    repairs = []
    mode = os.getenv("RFSN_VENV_MODE", "per_run")
    if not DEV and mode != "per_run":
        os.environ["RFSN_VENV_MODE"] = "per_run"
        repairs.append(f"forced venv mode from {mode!r} to per_run")
    return repairs, []


def _check_templates() -> tuple:
    """Reject templates containing 'eval' in their cmd argv."""
    repairs, fatals = [], []
    try:
        import yaml

        tmpl_path = "/policies/command_templates.yaml"
        if os.path.isfile(tmpl_path):
            with open(tmpl_path) as f:
                data = yaml.safe_load(f) or {}
            for name, defn in (data.get("templates") or {}).items():
                cmd = defn.get("cmd", [])
                if "eval" in cmd:
                    fatals.append(
                        _fatal(f"template {name!r} uses 'eval' — quarantined")
                    )
    except ImportError:
        pass  # yaml not available in this context
    return repairs, fatals


def run_checks() -> dict:
    """Run all hardening checks. Returns state dict."""
    all_repairs = []
    all_fatals = []

    for check_fn in (
        _check_auth,
        _check_patch_gate,
        _check_warm_sandbox,
        _check_venv_mode,
        _check_templates,
    ):
        repairs, fatals = check_fn()
        all_repairs.extend(repairs)
        all_fatals.extend(fatals)

    ok = len(all_fatals) == 0
    state = {
        "ok": ok,
        "repairs": all_repairs,
        "fatals": all_fatals,
        "dev_mode": DEV,
        "strict": STRICT,
        "checked_at": time.time(),
    }
    return state


def main() -> None:
    state = run_checks()

    # Persist state.
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2))
    except Exception as exc:
        print(f"WARN: could not write hardening state: {exc}", flush=True)

    if state["ok"]:
        print("HARDENING_OK", flush=True)
    else:
        print(f"HARDENING_FAILED: {len(state['fatals'])} fatal(s)", flush=True)
        if not DEV:
            sys.exit(2)


if __name__ == "__main__":
    main()
