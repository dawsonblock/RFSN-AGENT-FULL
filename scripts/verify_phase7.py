#!/usr/bin/env python3
"""Phase 7 Verification Script — Self-Healing & Hardening.

Runs all 34 Phase 7 unit tests and produces a summary report.

Usage:
    PYTHONPATH=. python3 scripts/verify_phase7.py
"""

import subprocess
import sys
import time

PHASE7_TESTS = [
    ("7.1 Execution Containment (Capsule)", "tests/test_capsule.py"),
    ("7.2 Cryptographic Ledger Chain", "tests/test_ledger_chain.py"),
    ("7.3 Self-Healing Core", "tests/test_self_healing.py"),
    ("7.4 Physical Deterministic Replay", "tests/test_physical_replay.py"),
]

BOLD = "\033[1m"
GREEN = "\033[92m"
RED = "\033[91m"
CYAN = "\033[96m"
RESET = "\033[0m"


def run_test(label: str, test_file: str) -> tuple[bool, int, float]:
    """Run a test file and return (passed, test_count, elapsed)."""
    start = time.time()
    result = subprocess.run(
        [sys.executable, "-m", "pytest", test_file, "-v", "--tb=short"],
        capture_output=True,
        text=True,
    )
    elapsed = time.time() - start

    # Count tests from pytest output
    count = 0
    for line in result.stdout.splitlines():
        if " passed" in line:
            # e.g. "16 passed in 0.01s"
            parts = line.strip().split()
            for i, p in enumerate(parts):
                if p == "passed" and i > 0:
                    try:
                        count = int(parts[i - 1])
                    except ValueError:
                        pass

    passed = result.returncode == 0
    if not passed:
        print(result.stdout[-500:] if len(result.stdout) > 500 else result.stdout)
        if result.stderr:
            print(result.stderr[-300:] if len(result.stderr) > 300 else result.stderr)

    return passed, count, elapsed


def main():
    print(f"\n{BOLD}{CYAN}╔══════════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}{CYAN}║   RFSN Phase 7: Self-Healing & Hardening     ║{RESET}")
    print(f"{BOLD}{CYAN}║   Verification Suite                         ║{RESET}")
    print(f"{BOLD}{CYAN}╚══════════════════════════════════════════════╝{RESET}\n")

    total_tests = 0
    total_passed = 0
    results = []

    for label, test_file in PHASE7_TESTS:
        print(f"  Running {BOLD}{label}{RESET} ... ", end="", flush=True)
        passed, count, elapsed = run_test(label, test_file)
        total_tests += count

        if passed:
            total_passed += count
            print(f"{GREEN}✓ {count} tests ({elapsed:.2f}s){RESET}")
            results.append((label, True, count))
        else:
            print(f"{RED}✗ FAILED{RESET}")
            results.append((label, False, count))

    # Summary
    print(f"\n{BOLD}{'─' * 50}{RESET}")
    print(f"{BOLD}  Summary:{RESET}\n")

    for label, passed, count in results:
        icon = f"{GREEN}✓{RESET}" if passed else f"{RED}✗{RESET}"
        print(f"    {icon}  {label} ({count} tests)")

    print(f"\n{BOLD}{'─' * 50}{RESET}")

    all_ok = all(r[1] for r in results)
    if all_ok:
        print(f"\n  {GREEN}{BOLD}ALL {total_passed} TESTS PASSED ✓{RESET}\n")
    else:
        failed = [r[0] for r in results if not r[1]]
        print(f"\n  {RED}{BOLD}FAILURES: {', '.join(failed)}{RESET}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
