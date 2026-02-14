import os, json, time, sys
from pathlib import Path
from services.hardening_guard.checks import run_checks

STRICT = os.getenv("RFSN_HARDENING_STRICT", "1") == "1"
DEV = os.getenv("RFSN_DEV_MODE", "0") == "1"
STATE = Path("/data/hardening_state.json")


def fatal(msg):
    print("HARDENING_FATAL:", msg, flush=True)
    if STRICT and not DEV:
        sys.exit(2)


def main():
    ok, repairs, fatals = run_checks(dev=DEV)
    for f in fatals:
        fatal(f)

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(
        json.dumps({"ok": ok, "repairs": repairs, "time": time.time()}, indent=2)
    )

    print("HARDENING_OK" if ok else "HARDENING_REPAIRED", flush=True)


if __name__ == "__main__":
    main()
