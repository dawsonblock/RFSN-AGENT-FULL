import os
import sys
import time
import subprocess
from pathlib import Path


def main():
    print("Starting Hardening Supervisor...", flush=True)
    while True:
        # 1. Check integrity
        # 2. Check drift
        # 3. Check invariants
        time.sleep(60)


if __name__ == "__main__":
    main()
