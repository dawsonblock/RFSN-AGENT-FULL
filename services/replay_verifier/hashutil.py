import hashlib, os


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_tree(root, ignore=None):
    ignore = set(ignore or [])
    h = hashlib.sha256()
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ignore]
        for f in sorted(files):
            p = os.path.join(base, f)
            try:
                h.update(f.encode())
                h.update(str(os.path.getsize(p)).encode())
                with open(p, "rb") as f_obj:
                    # Read 4k chunks to avoid memory issues with large files
                    while True:
                        chunk = f_obj.read(4096)
                        if not chunk:
                            break
                        h.update(chunk)
            except Exception:
                pass
    return h.hexdigest()
