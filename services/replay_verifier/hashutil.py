import hashlib, os


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_tree(root, ignore=None, max_file_bytes=50_000_000, previous_snapshot=None):
    """Recursively hash a directory tree.

    If 'previous_snapshot' (dict of path -> {hash, size, mtime}) is provided,
    files with matching size and mtime will skip re-hashing.
    """
    ignore = set(ignore or [])
    h = hashlib.sha256()

    # Sort for determinism
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted([d for d in dirs if d not in ignore])
        for f in sorted(files):
            p = os.path.join(base, f)
            rel_path = os.path.relpath(p, root)

            try:
                stat = os.stat(p)
                size = stat.st_size
                mtime = stat.st_mtime

                if size > max_file_bytes:
                    continue

                h.update(f.encode())

                # Incremental check
                if previous_snapshot:
                    prev = previous_snapshot.get(rel_path)
                    if (
                        prev
                        and prev.get("size") == size
                        and abs(prev.get("mtime", 0) - mtime) < 0.001
                    ):
                        # Cache hit
                        if prev.get("hash"):
                            h.update(bytes.fromhex(prev["hash"]))
                            continue

                # Cache miss - read file
                file_hash = hashlib.sha256()
                with open(p, "rb") as fh:
                    for chunk in iter(lambda: fh.read(65536), b""):
                        file_hash.update(chunk)

                digest = file_hash.digest()
                h.update(digest)  # Accumulate into tree hash

            except Exception:
                pass

    return h.hexdigest()
