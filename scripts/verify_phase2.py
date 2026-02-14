import os
import sys
import time
import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.executor.sandbox_pool import SandboxPool
from services.replay_verifier.hashutil import sha256_tree
from system.metrics import get_registry


def test_metrics_integration():
    print("\n[Metric Integration Test]")
    pool = SandboxPool()
    reg = get_registry()

    # Check if metrics exist in registry
    prom = reg.export_prometheus()
    if "sandbox_active" not in prom:
        print("FAIL: sandbox_active metric not found")
        return False

    print("PASS: Metrics registered")

    # Simulate activity
    pool._active_gauge.set(5)
    pool._exec_counter.inc()

    prom = reg.export_prometheus()
    if "sandbox_active 5" in prom and "sandbox_execs 1" in prom:
        print("PASS: Metrics updating correctly")
        return True

    print(f"FAIL: Metrics not updating. Dump:\n{prom}")
    return False


def test_incremental_hashing():
    print("\n[Incremental Hashing Test]")
    with tempfile.TemporaryDirectory() as tmp:
        # Create a large-ish file structure
        os.makedirs(os.path.join(tmp, "a/b"))
        f1 = os.path.join(tmp, "a/f1.txt")
        f2 = os.path.join(tmp, "a/b/f2.txt")

        with open(f1, "w") as f:
            f.write("content1" * 1000)
        with open(f2, "w") as f:
            f.write("content2" * 1000)

        # 1. Initial Hash
        t0 = time.time()
        hash1 = sha256_tree(tmp)
        t1 = time.time()
        print(f"Initial hash: {hash1[:8]} ({t1-t0:.4f}s)")

        # 2. Mock Snapshot
        s1 = os.stat(f1)
        s2 = os.stat(f2)
        snapshot = {
            "a/f1.txt": {"hash": "fakehash1", "size": s1.st_size, "mtime": s1.st_mtime},
            "a/b/f2.txt": {
                "hash": "fakehash2",
                "size": s2.st_size,
                "mtime": s2.st_mtime,
            },
        }

        # 3. Incremental Hash (should use fake hashes)
        t2 = time.time()
        hash2 = sha256_tree(tmp, previous_snapshot=snapshot)
        t3 = time.time()
        print(f"Incremental hash: {hash2[:8]} ({t3-t2:.4f}s)")

        if hash1 == hash2:
            print(
                "FAIL: Incremental hash matched initial hash (meant it ignored the snapshot)"
            )
            return False

        print("PASS: Incremental hash used snapshot (different result)")
        return True


def test_parallel_cli_imports():
    print("\n[Parallel CLI Imports Test]")
    try:
        from rfsn_swebench import cli
        import concurrent.futures

        print("PASS: CLI imports OK")
        return True
    except ImportError as e:
        print(f"FAIL: {e}")
        return False


if __name__ == "__main__":
    print("=== PHASE 2 VERIFICATION ===")
    results = [
        test_metrics_integration(),
        test_incremental_hashing(),
        test_parallel_cli_imports(),
    ]

    if all(results):
        print("\n=== ALL TESTS PASSED ===")
        exit(0)
    else:
        print("\n=== SOME TESTS FAILED ===")
        exit(1)
