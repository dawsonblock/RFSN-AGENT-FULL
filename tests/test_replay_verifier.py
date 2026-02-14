import os
import sys
import json
import pytest
from unittest.mock import patch, mock_open

# Add project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.replay_verifier import hashutil, verify


def test_sha256_tree_stability(tmp_path):
    # Create two identical directory structures
    d1 = tmp_path / "d1"
    d1.mkdir()
    (d1 / "file1.txt").write_text("content1")
    (d1 / "subdir").mkdir()
    (d1 / "subdir" / "file2.txt").write_text("content2")

    d2 = tmp_path / "d2"
    d2.mkdir()
    (d2 / "file1.txt").write_text("content1")
    (d2 / "subdir").mkdir()
    (d2 / "subdir" / "file2.txt").write_text("content2")

    h1 = hashutil.sha256_tree(str(d1))
    h2 = hashutil.sha256_tree(str(d2))

    assert h1 == h2
    assert len(h1) == 64


def test_sha256_tree_change(tmp_path):
    d1 = tmp_path / "d1"
    d1.mkdir()
    (d1 / "f.txt").write_text("a")

    d2 = tmp_path / "d2"
    d2.mkdir()
    (d2 / "f.txt").write_text("b")

    assert hashutil.sha256_tree(str(d1)) != hashutil.sha256_tree(str(d2))


@patch("services.replay_verifier.verify.load")
@patch("sys.exit")
def test_verify_main_match(mock_exit, mock_load):
    # Setup identical manifests
    manifest = {"deps": "abc", "env": "def", "patch_hash": "123", "kernel_trace": "xyz"}
    mock_load.side_effect = [manifest, manifest]

    # Should run without exit(3)
    verify.main("path_a", "path_b")
    mock_exit.assert_not_called()


@patch("services.replay_verifier.verify.load")
@patch("sys.exit")
def test_verify_main_mismatch(mock_exit, mock_load):
    m1 = {"deps": "abc", "env": "def", "patch_hash": "123", "kernel_trace": "xyz"}
    m2 = {
        "deps": "abc",
        "env": "def",
        "patch_hash": "999",
        "kernel_trace": "xyz",
    }  # diff patch
    mock_load.side_effect = [m1, m2]

    verify.main("path_a", "path_b")
    mock_exit.assert_called_with(3)
