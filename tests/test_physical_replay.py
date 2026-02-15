"""Tests for Phase 7.4: Physical Deterministic Replay."""

import os
import tempfile
import unittest

from services.replay_verifier.physical import (
    capture_environment,
    compare_environments,
    EnvironmentSnapshot,
    _sanitize_env,
    _hash_workspace,
)


class TestEnvironmentCapture(unittest.TestCase):

    def test_basic_capture(self):
        snap = capture_environment(
            workspace=".",
            include_packages=False,
            include_files=False,
            include_env=False,
        )
        self.assertTrue(snap.python_version)  # non-empty
        self.assertTrue(snap.platform_system)
        self.assertGreater(snap.capture_time, 0)

    def test_seed_capture(self):
        snap = capture_environment(
            seed=42,
            include_packages=False,
            include_files=False,
            include_env=False,
        )
        self.assertEqual(snap.random_seed, 42)

    def test_fingerprint_stability(self):
        snap = capture_environment(
            seed=42,
            include_packages=False,
            include_files=False,
            include_env=False,
        )
        fp1 = snap.fingerprint()
        fp2 = snap.fingerprint()
        self.assertEqual(fp1, fp2)


class TestSanitization(unittest.TestCase):

    def test_secrets_redacted(self):
        env = {
            "PATH": "/usr/bin",
            "API_KEY": "super_secret",
            "DATABASE_PASSWORD": "pass123",
            "HOME": "/home/user",
        }
        sanitized = _sanitize_env(env)
        self.assertEqual(sanitized["PATH"], "/usr/bin")
        self.assertEqual(sanitized["API_KEY"], "[REDACTED]")
        self.assertEqual(sanitized["DATABASE_PASSWORD"], "[REDACTED]")
        self.assertEqual(sanitized["HOME"], "/home/user")


class TestWorkspaceHashing(unittest.TestCase):

    def test_hash_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create test files
            (open(os.path.join(tmpdir, "a.py"), "w")).write("print('hello')")
            (open(os.path.join(tmpdir, "b.py"), "w")).write("print('world')")

            agg, manifest = _hash_workspace(tmpdir)
            self.assertEqual(len(manifest), 2)
            self.assertIn("a.py", manifest)
            self.assertIn("b.py", manifest)
            self.assertTrue(agg)

    def test_deterministic_hashing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (open(os.path.join(tmpdir, "x.txt"), "w")).write("content")
            h1, _ = _hash_workspace(tmpdir)
            h2, _ = _hash_workspace(tmpdir)
            self.assertEqual(h1, h2)


class TestDriftDetection(unittest.TestCase):

    def test_identical_environments(self):
        snap = capture_environment(
            seed=42,
            include_packages=False,
            include_files=False,
            include_env=False,
        )
        report = compare_environments(snap, snap)
        self.assertTrue(report.is_identical)

    def test_seed_drift(self):
        a = EnvironmentSnapshot(random_seed=42)
        b = EnvironmentSnapshot(random_seed=99)
        report = compare_environments(a, b)
        self.assertFalse(report.seed_match)
        self.assertFalse(report.is_identical)

    def test_package_drift(self):
        a = EnvironmentSnapshot(installed_packages={"numpy": "1.24.0"})
        b = EnvironmentSnapshot(installed_packages={"numpy": "1.25.0"})
        report = compare_environments(a, b)
        self.assertFalse(report.is_identical)
        self.assertGreater(len(report.package_diffs), 0)

    def test_file_drift(self):
        a = EnvironmentSnapshot(
            workspace_hash="aaa",
            file_manifest={"main.py": "hash1"},
        )
        b = EnvironmentSnapshot(
            workspace_hash="bbb",
            file_manifest={"main.py": "hash2"},
        )
        report = compare_environments(a, b)
        self.assertFalse(report.workspace_match)
        self.assertGreater(len(report.file_diffs), 0)


if __name__ == "__main__":
    unittest.main()
