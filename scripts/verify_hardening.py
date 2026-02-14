import os, subprocess, sys


def expect_fail(cmd):
    p = subprocess.run(cmd, shell=True)
    if p.returncode == 0:
        print("FAIL: expected failure", cmd)
        sys.exit(5)


def main():
    # Auth missing should fail
    print("Verifying hardening checks fail when auth missing...")
    os.environ["RFSN_DEV_MODE"] = "0"
    os.environ["RFSN_AUTH_REQUIRED"] = "1"
    # We must ensure STRICT mode is on for it to exit with code 2
    os.environ["RFSN_HARDENING_STRICT"] = "1"

    expect_fail("python3 -m services.hardening_guard.app")

    print("VERIFY_OK")


if __name__ == "__main__":
    main()
