import hashlib, os


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_tree(root, ignore=None, max_file_bytes=50_000_000):
    ignore = set(ignore or [])
    h = hashlib.sha256()
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ignore]
        for f in sorted(files):
            p = os.path.join(base, f)
            try:
                if os.path.getsize(p) > max_file_bytes:
                    continue
                h.update(f.encode())
                with open(p, "rb") as fh:
                    for chunk in iter(lambda: fh.read(65536), b""):
                        h.update(chunk)
            except Exception:
                pass
    return h.hexdigest()
