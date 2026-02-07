#!/usr/bin/env bash
set -euo pipefail
ROOT="$(pwd)"
OUT="${ROOT}/requirements.in"

if [[ -f "$OUT" ]]; then
  echo "[*] requirements.in already exists"
  exit 0
fi

echo "[*] Creating requirements.in (best effort)"

if [[ -f "pyproject.toml" ]]; then
  python3 - <<'PY'
import re, pathlib
p = pathlib.Path("pyproject.toml").read_text(encoding="utf-8", errors="replace")
m = re.search(r'(?s)\[project\].*?dependencies\s*=\s*\[(.*?)\]', p)
deps=[]
if m:
    block=m.group(1)
    for line in block.splitlines():
        s=line.strip().strip(",").strip().strip('"').strip("'")
        if not s or s.startswith("#"): 
            continue
        deps.append(s)
out = pathlib.Path("requirements.in")
out.write_text("\n".join(deps) + ("\n" if deps else "# TODO: add dependencies here\n"), encoding="utf-8")
print("[*] wrote requirements.in")
PY
  exit 0
fi

if [[ -f "requirements.txt" ]]; then
  python3 - <<'PY'
import pathlib
inp = pathlib.Path("requirements.txt").read_text(encoding="utf-8", errors="replace").splitlines()
out=[]
for ln in inp:
    s=ln.strip()
    if not s or s.startswith("#"): 
        continue
    s = s.split(" --hash=",1)[0].strip()
    if s.startswith("--hash=") or s.startswith("\\"):
        continue
    out.append(s)
pathlib.Path("requirements.in").write_text("\n".join(out)+"\n", encoding="utf-8")
print("[*] bootstrapped requirements.in from requirements.txt (review it)")
PY
  exit 0
fi

echo "# TODO: add dependencies here" > requirements.in
echo "[!] No deps source found; wrote TODO stub"
