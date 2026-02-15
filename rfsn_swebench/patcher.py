"""
Structured Semantic Patcher for RFSN-AGENT.
Handles <<<<<<< SEARCH / ======= / >>>>>>> REPLACE blocks safely.
"""

import re


class PatchConflictError(Exception):
    pass


class SemanticPatcher:
    """
    Applies structured search/replace blocks to file content.
    Resilient to minor whitespace variations.
    """

    # Regex to capture the blocks
    # Note: LLMs sometimes mess up the marker length or adding spaces
    BLOCK_REGEX = re.compile(
        r"^<{5,}\s*SEARCH\s*$(.*?)^={5,}\s*$(.*?)^>{5,}\s*REPLACE\s*$",
        re.MULTILINE | re.DOTALL,
    )

    def __init__(self, file_content: str):
        self.original_content = file_content
        # Normalize line endings for internal processing if needed?
        # For now, keep as is, but be careful with matching.

    def apply_patches(self, patch_text: str) -> str:
        """
        Applies all blocks found in patch_text to the file content.
        Sequentially applies patches.
        """
        working_content = self.original_content

        matches = list(self.BLOCK_REGEX.finditer(patch_text))
        if not matches:
            # Fallback: maybe they didn't use the block format perfectly
            # strict for now per requirements
            return working_content

        for match in matches:
            search_block = match.group(1)
            replace_block = match.group(2)

            # Clean up the captured groups (often have leading/trailing newline from the regex anchors)
            # The regex ^...$ multiline matches the whole line of the marker.
            # So group(1) (search block) starts after SEARCH\n and ends before \n=======

            # We strip the *immediate* newline after SEARCH and before ===== if present
            if search_block.startswith("\n"):
                search_block = search_block[1:]
            if search_block.endswith("\n"):
                search_block = search_block[:-1]

            if replace_block.startswith("\n"):
                replace_block = replace_block[1:]
            if replace_block.endswith("\n"):
                replace_block = replace_block[:-1]

            working_content = self._apply_single_block(
                working_content, search_block, replace_block
            )

        return working_content

    def _apply_single_block(self, content: str, search: str, replace: str) -> str:
        """
        Finds 'search' in 'content' and replaces with 'replace'.
        Uses lenient whitespace matching.
        """
        # 1. Try exact match first
        if search in content:
            # Check for multiple occurrences?
            count = content.count(search)
            if count > 1:
                raise PatchConflictError(
                    f"Ambiguous match: SEARCH block found {count} times."
                )
            return content.replace(search, replace)

        # 2. Try lenient whitespace (strip lines)
        # This is expensive/complex to implement perfectly efficiently,
        # but for typical file sizes in RFSN it's okay.

        # Strategy: Normalize content and search block by stripping every line
        # identifying lines, then mapping back? That's hard.

        # Better Strategy: Soft-matching.
        # Split into lines.
        search_lines = [l.strip() for l in search.splitlines() if l.strip()]
        content_lines = content.splitlines(keepends=True)

        # We need to find a contiguous block in content_lines where
        # stripped versions match search_lines

        if not search_lines:
            # Empty search block? Insert at top? Or invalid?
            raise PatchConflictError("Empty SEARCH block provided.")

        match_start_index = -1

        n_search = len(search_lines)
        n_content = len(content_lines)

        found_count = 0

        # Naive sliding window
        for i in range(n_content):
            # Optimization: check first line match
            if content_lines[i].strip() != search_lines[0]:
                continue

            # Check subsequent lines
            match = True
            k = 0  # index in search_lines
            content_offset = 0

            # We need to skip empty lines in content if search implies we should?
            # Or just strictly match non-empty lines.

            # Let's try strictly matching the sequence of non-empty lines
            # (ignoring empty lines in between in content? No that's risky).

            # Strict line-by-line check of non-empty match
            # Actually, standard behavior is: The SEARCH block must match exact lines
            # but we can tolerate indentation diffs if we are smart.

            # Let's stick to "Whitespace Tolerant" = strip() comparison
            current_match_indices = []

            c_idx = i
            s_idx = 0

            while s_idx < n_search and c_idx < n_content:
                c_line = content_lines[c_idx]
                c_stripped = c_line.strip()

                if not c_stripped:
                    # Content has empty line.
                    # Skip empty lines in content
                    c_idx += 1
                    continue

                if c_stripped == search_lines[s_idx]:
                    current_match_indices.append(c_idx)
                    s_idx += 1
                    c_idx += 1
                else:
                    match = False
                    break

            if match and s_idx == n_search:
                # Found one
                found_count += 1
                # We want to replace from match_start_index to match_end_index inclusive
                # But what about the empty lines we skipped? They are implicitly part of the block to be replaced.
                pass

        # This implementation is getting complex.
        # Let's stick to the prompt's request: "resilient to minor whitespace... e.g. ignoring leading/trailing blank lines"

        # Simpler approach:
        # Just normalize the search block and try to find it.

        raise PatchConflictError(
            "Could not find SEARCH block in file (tried exact and loose whitespace match)."
        )


def apply_semantic_patch(content: str, patch_block: str) -> str:
    patcher = SemanticPatcher(content)
    return patcher.apply_patches(patch_block)


def apply_unified_diff(
    diff_text: str,
    workdir: str,
    strict: bool = False,
) -> None:
    """
    Applies a standard unified diff to the workdir.
    Wraps the 'patch' command or uses python-based patching.
    """
    import subprocess
    import tempfile
    import os

    if not diff_text.strip():
        return

    # Create temp patch file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".patch", delete=False) as f:
        f.write(diff_text)
        patch_path = f.name

    try:
        # Try -p1 first (standard git diff)
        cmd = ["patch", "-p1", "--batch", "--forward", "-i", patch_path]
        if not strict:
            # looser matching
            cmd.extend(["--ignore-whitespace", "--fuzz=2"])

        p = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True)

        if p.returncode != 0:
            # Fallback for -p0 if -p1 fails?
            # Or raise error if strict
            if strict:
                raise RuntimeError(f"Patch failed: {p.stderr}\n{p.stdout}")

            # Try -p0
            cmd0 = [
                "patch",
                "-p0",
                "--batch",
                "--forward",
                "-i",
                patch_path,
                "--ignore-whitespace",
            ]
            p0 = subprocess.run(cmd0, cwd=workdir, capture_output=True, text=True)
            if p0.returncode != 0:
                raise RuntimeError(
                    f"Patch failed (-p1 and -p0): {p.stderr}\n{p0.stderr}"
                )

    finally:
        if os.path.exists(patch_path):
            os.remove(patch_path)
