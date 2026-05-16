"""
Structured Semantic Patcher for RFSN-AGENT.

Applies exact SEARCH/REPLACE operations to file content.

API
---
The canonical interface is:

    apply_semantic_patch_to_file(
        workspace_root: str,
        path: str,
        search: str,
        replace: str,
    ) -> PatchResult

Rules enforced
--------------
* ``search`` must be non-empty.
* ``search`` must exist exactly once in the file.
* ``replace`` must differ from ``search`` (no-op is an error).
* ``path`` must exist and must not escape the workspace root.
* Patching test files, CI configs, and dependency manifests is
  rejected (same gate as ``apply_patch``).

Errors
------
* ``PatchConflictError``  — search text not found, or ambiguous (>1 match).
* ``NoOpPatchError``      — search == replace; no change would occur.
* ``PatchPathError``      — path traversal, missing file, or blocked path.
* ``PatchGateError``      — attempt to patch a protected file class.

Fuzzy matching
--------------
This module implements **exact-only** matching.  Do not add fuzzy/whitespace-
tolerant matching until it is fully unit-tested and the tests in
``tests/test_semantic_patch_safety.py`` all pass.
"""

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import List

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

_DEP_MANIFESTS = frozenset({
    "pyproject.toml",
    "poetry.lock",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements.in",
    "constraints.txt",
    "setup.py",
    "setup.cfg",
    "pipfile",
    "pipfile.lock",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "go.mod",
    "go.sum",
    "cargo.toml",
    "cargo.lock",
})


class PatchConflictError(Exception):
    """Search text not found, or found multiple times (ambiguous)."""


class NoOpPatchError(Exception):
    """Search and replace are identical — patch would change nothing."""


class PatchPathError(Exception):
    """Path traversal, missing file, or otherwise invalid path."""


class PatchGateError(Exception):
    """Attempt to patch a protected file class (tests, CI, deps)."""


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class PatchResult:
    files_changed: List[str] = field(default_factory=list)
    lines_added: int = 0
    lines_removed: int = 0
    diff_preview: str = ""
    before_hash: str = ""
    after_hash: str = ""


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _safe_path(workspace_root: str, rel_path: str) -> str:
    """Resolve *rel_path* inside *workspace_root* and verify it does not escape.

    Returns the absolute path on success.
    Raises ``PatchPathError`` on traversal or empty input.
    """
    if not rel_path or not rel_path.strip():
        raise PatchPathError("path must not be empty")
    norm = os.path.normpath(rel_path)
    if os.path.isabs(norm):
        raise PatchPathError(f"absolute paths are not allowed: {rel_path!r}")
    if norm.startswith(".."):
        raise PatchPathError(f"path traversal detected: {rel_path!r}")
    full = os.path.realpath(os.path.join(workspace_root, norm))
    root = os.path.realpath(workspace_root)
    if not full.startswith(root + os.sep) and full != root:
        raise PatchPathError(f"path escapes workspace root: {rel_path!r}")
    return full


def _is_test_path(path: str) -> bool:
    p = path.replace("\\", "/").lstrip("/")
    return (
        p.startswith("tests/")
        or "/tests/" in f"/{p}"
        or p.startswith("test/")
        or p.endswith("_test.py")
        or p.endswith("test.py")
    )


def _is_ci_path(path: str) -> bool:
    p = path.replace("\\", "/").lstrip("/")
    return (
        p.startswith(".github/workflows/")
        or p.startswith("ci/")
        or p.startswith("scripts/")
    )


def _is_dep_manifest(path: str) -> bool:
    base = path.replace("\\", "/").split("/")[-1].lower()
    return base in _DEP_MANIFESTS


def _check_patch_gate(rel_path: str) -> None:
    """Raise ``PatchGateError`` if the path is a protected file class."""
    if _is_test_path(rel_path):
        raise PatchGateError(
            f"semantic patch rejected: {rel_path!r} is a test file"
        )
    if _is_ci_path(rel_path):
        raise PatchGateError(
            f"semantic patch rejected: {rel_path!r} is a CI config file"
        )
    if _is_dep_manifest(rel_path):
        raise PatchGateError(
            f"semantic patch rejected: {rel_path!r} is a dependency manifest"
        )


# ---------------------------------------------------------------------------
# Diff helpers
# ---------------------------------------------------------------------------

def _line_diff_stats(before: str, after: str):
    """Return (lines_added, lines_removed) between two strings."""
    import difflib

    diff = difflib.ndiff(before.splitlines(), after.splitlines())
    added = 0
    removed = 0
    for line in diff:
        if line.startswith("+ "):
            added += 1
        elif line.startswith("- "):
            removed += 1
    return added, removed


def _diff_preview(before: str, after: str, max_chars: int = 2000) -> str:
    """Return a simple unified-style preview (truncated to *max_chars*)."""
    import difflib
    diff = list(difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile="before",
        tofile="after",
        n=2,
    ))
    preview = "".join(diff)
    if len(preview) > max_chars:
        preview = preview[:max_chars] + "\n... (truncated)"
    return preview


# ---------------------------------------------------------------------------
# Core patch function (operates on strings, no I/O)
# ---------------------------------------------------------------------------

def apply_semantic_patch_to_content(
    content: str,
    search: str,
    replace: str,
) -> str:
    """Apply an exact SEARCH/REPLACE to *content*.

    Raises
    ------
    ``NoOpPatchError``     if search == replace.
    ``PatchConflictError`` if search is empty, not found, or ambiguous.
    """
    if not search:
        raise PatchConflictError("search text must not be empty")
    if search == replace:
        raise NoOpPatchError("search and replace are identical — no change would occur")
    count = content.count(search)
    if count == 0:
        raise PatchConflictError(
            "search text not found in file content"
        )
    if count > 1:
        raise PatchConflictError(
            f"search text found {count} times (ambiguous); patch rejected"
        )
    return content.replace(search, replace, 1)


# ---------------------------------------------------------------------------
# File-level entry point (used by executor)
# ---------------------------------------------------------------------------

def apply_semantic_patch_to_file(
    workspace_root: str,
    path: str,
    search: str,
    replace: str,
    *,
    skip_patch_gate: bool = False,
) -> PatchResult:
    """Read *path* (relative to *workspace_root*), apply the patch, write back.

    Parameters
    ----------
    workspace_root:
        Absolute path to the workspace / repo root.  Used for containment
        checks.
    path:
        Repo-root-relative path to the file to patch.
    search:
        Exact text to find.  Must be non-empty and unique in the file.
    replace:
        Replacement text.  Must differ from *search*.
    skip_patch_gate:
        Set to ``True`` only in unit tests that deliberately test gated paths.
        Production code must never set this.

    Returns
    -------
    ``PatchResult`` describing what changed.

    Raises
    ------
    ``PatchPathError``     — bad path.
    ``PatchGateError``     — protected file class.
    ``PatchConflictError`` — search not found or ambiguous.
    ``NoOpPatchError``     — no change would occur.
    ``FileNotFoundError``  — file does not exist.
    ``OSError``            — I/O error.
    """
    if not skip_patch_gate:
        _check_patch_gate(path)

    full_path = _safe_path(workspace_root, path)
    if not os.path.isfile(full_path):
        raise FileNotFoundError(f"file not found: {path!r}")

    with open(full_path, "r", encoding="utf-8") as fh:
        before = fh.read()

    after = apply_semantic_patch_to_content(before, search, replace)

    before_hash = hashlib.sha256(before.encode("utf-8")).hexdigest()
    after_hash = hashlib.sha256(after.encode("utf-8")).hexdigest()

    with open(full_path, "w", encoding="utf-8") as fh:
        fh.write(after)

    added, removed = _line_diff_stats(before, after)
    preview = _diff_preview(before, after)

    return PatchResult(
        files_changed=[path],
        lines_added=added,
        lines_removed=removed,
        diff_preview=preview,
        before_hash=before_hash,
        after_hash=after_hash,
    )


# ---------------------------------------------------------------------------
# Legacy block-format helpers (kept for backward compat; not part of API)
# ---------------------------------------------------------------------------

def apply_unified_diff(
    diff_text: str,
    workdir: str,
    strict: bool = False,
) -> None:
    """Apply a standard unified diff to the workdir via the ``patch`` command.

    Note: this function uses ``subprocess.run`` with a structured argument list
    (no ``shell=True``).  The patch content is written to a temp file; no
    agent-controlled content reaches the shell.
    """
    import subprocess
    import tempfile

    if not diff_text.strip():
        return

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".patch", delete=False
    ) as f:
        f.write(diff_text)
        patch_path = f.name

    try:
        cmd = ["patch", "-p1", "--batch", "--forward", "-i", patch_path]
        if not strict:
            cmd.extend(["--ignore-whitespace", "--fuzz=2"])

        p = subprocess.run(
            cmd,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        if p.returncode != 0:
            if strict:
                raise RuntimeError(f"Patch failed: {p.stderr}\n{p.stdout}")

            cmd0 = [
                "patch", "-p0", "--batch", "--forward", "-i", patch_path,
                "--ignore-whitespace",
            ]
            p0 = subprocess.run(
                cmd0,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if p0.returncode != 0:
                raise RuntimeError(
                    f"Patch failed (-p1 and -p0): {p.stderr}\n{p0.stderr}"
                )
    finally:
        try:
            os.remove(patch_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Backward-compatibility shim
# (SemanticPatcher was the legacy API; new code should use
# apply_semantic_patch_to_file() / apply_semantic_patch_to_content().)
# ---------------------------------------------------------------------------

class SemanticPatcher:
    """Backward-compatibility wrapper around the new exact-match patcher.

    Parses the legacy ``<<<<<<< SEARCH / ======= / REPLACE >>>>>>>`` block
    format and delegates to :func:`apply_semantic_patch_to_content`.

    New code should use :func:`apply_semantic_patch_to_content` directly.
    """

    def __init__(self, file_content: str) -> None:
        self._content = file_content

    def apply_patches(self, patch_text: str) -> str:
        """Apply a ``<<<<<<< SEARCH / ======= / >>>>>>> REPLACE`` block to the content.

        Applies each block in order.  Raises :class:`PatchConflictError` if
        any block's search text is not found, or :class:`NoOpPatchError` if
        a block makes no change.
        """
        import re

        # Parse all SEARCH/REPLACE blocks.
        pattern = re.compile(
            r"<<<<<<< SEARCH\n(.*?)\n=======\n(.*?)\n>>>>>>> REPLACE",
            re.DOTALL,
        )
        blocks = pattern.findall(patch_text)
        if not blocks:
            raise PatchConflictError(
                "No SEARCH/REPLACE blocks found in patch_text. "
                "Expected format: <<<<<<< SEARCH ... ======= ... >>>>>>> REPLACE"
            )

        content = self._content
        for search, replace in blocks:
            content = apply_semantic_patch_to_content(content, search, replace)
        self._content = content
        return content


# ---------------------------------------------------------------------------
# Module-level compatibility function
# (The executor service injects patcher.py into a sandbox script and calls
# this function by name.  It delegates to SemanticPatcher so the legacy
# <<<<<<< SEARCH / ======= / >>>>>>> REPLACE block format continues to work.)
# ---------------------------------------------------------------------------

def apply_semantic_patch(content: str, patch_text: str) -> str:
    """Apply *patch_text* to *content* and return the modified content.

    *patch_text* must contain one or more SEARCH/REPLACE blocks in the format::

        <<<<<<< SEARCH
        old text
        =======
        new text
        >>>>>>> REPLACE

    Raises :class:`PatchConflictError` if any search text is not found and
    :class:`NoOpPatchError` if a block would make no change.

    .. deprecated::
        Prefer :func:`apply_semantic_patch_to_content` for new code.
    """
    return SemanticPatcher(content).apply_patches(patch_text)

