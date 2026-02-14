import os
from pathlib import Path


def _auth_ok(dev):
    if dev:
        return True, []
    if not Path("/shared/auth.py").exists():
        return False, ["auth required but missing"]
    return True, []


def _patch_gate_ok(dev):
    try:
        import rfsn_swebench.gate.patch_risk_gate  # noqa

        return True, []
    except Exception:
        if dev:
            return True, []
        return False, ["patch gate required but missing"]


def _warm_sandbox(dev):
    repairs = []
    if not dev and os.getenv("RFSN_WARM_SANDBOX", "0") == "1":
        os.environ["RFSN_WARM_SANDBOX"] = "0"
        repairs.append("disabled warm sandbox")
    return True, repairs


def _venv_mode(dev):
    repairs = []
    if not dev and os.getenv("RFSN_VENV_MODE", "per_run") != "per_run":
        os.environ["RFSN_VENV_MODE"] = "per_run"
        repairs.append("forced per_run venv")
    return True, repairs


def run_checks(dev=False):
    repairs = []
    fatals = []

    ok, f = _auth_ok(dev)
    if not ok:
        fatals += f

    ok, f = _patch_gate_ok(dev)
    if not ok:
        fatals += f

    _, r = _warm_sandbox(dev)
    repairs += r
    _, r = _venv_mode(dev)
    repairs += r

    return (len(fatals) == 0), repairs, fatals
