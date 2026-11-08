"""Static safety check of the single registered group-A npm package (PREREG-deviations D19).

    .venv/bin/python -I scripts/static_check.py

Never installs or runs package code. Two requests (curl): the npm registry document and the
tarball of dist-tags.latest. The tarball is verified against the registry's sha1 shasum and sha512
integrity, its members are checked (regular files and directories only, no absolute paths, no '..')
before `tar` extracts it into data/private/quarantine/, and the files are scanned as text only.
Writes data/private/quarantine/report.md (counts only); the package name is never printed.
Refuses to run if data/private/quarantine/ exists (one download only).
"""
import base64
import collections
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
Q = REPO / "data" / "private" / "quarantine"
UA = "slop-bench-revalidation/1.0 (research; static check)"

# 1. the name: npm, universal set (variant c), registered after generation, EXISTS
uni = set(json.loads((REPO / "data/private/recompute/universal_c_mention.json").read_text())["npm"])
c = sqlite3.connect(f"file:{REPO}/data/private/classification.db?mode=ro", uri=True)
cand = [n for (n,) in c.execute("SELECT name FROM names WHERE ecosystem='npm' AND exclusion IS NULL "
                                "AND asof_status='H_REGISTERED_LATER' AND category='EXISTS'") if n in uni]
if len(cand) != 1:
    sys.exit(f"expected exactly 1 candidate, found {len(cand)}")
name = cand[0]

Q.mkdir(parents=True, exist_ok=False)
def curl(url, out):
    r = subprocess.run(["curl", "-fsS", "--max-time", "120", "-A", UA, "-o", str(out), url], capture_output=True)
    if r.returncode:
        sys.exit(f"curl failed (exit {r.returncode})")

# 2. registry document -> latest version -> dist.tarball
enc = name.replace("/", "%2F")
curl(f"https://registry.npmjs.org/{enc}", Q / "registry_doc.json")
doc = json.loads((Q / "registry_doc.json").read_text())
latest = (doc.get("dist-tags") or {}).get("latest")
ver = doc["versions"][latest]
dist = ver["dist"]
if not dist["tarball"].startswith("https://registry.npmjs.org/"):
    sys.exit("unexpected tarball host")
curl(dist["tarball"], Q / "package.tgz")
raw = (Q / "package.tgz").read_bytes()
integ = dist.get("integrity", "")
ok_integrity = None
if integ.startswith("sha512-"):
    ok_integrity = base64.b64encode(hashlib.sha512(raw).digest()).decode() == integ[7:]
ok_shasum = hashlib.sha1(raw).hexdigest() == dist.get("shasum")
if ok_integrity is False or not ok_shasum:
    sys.exit("tarball checksum mismatch")

# 3. safe extraction: regular files and dirs only, no absolute paths, no '..'
with tarfile.open(Q / "package.tgz", "r:gz") as tf:
    members = tf.getmembers()
    bad = [m for m in members if not (m.isfile() or m.isdir()) or m.name.startswith("/") or ".." in Path(m.name).parts]
    if bad:
        sys.exit(f"{len(bad)} unsafe tar members (links/devices/absolute/..); not extracted")
r = subprocess.run(["tar", "-xzf", str(Q / "package.tgz"), "--no-same-owner", "--no-same-permissions",
                    "-C", str(Q)], capture_output=True)
if r.returncode:
    sys.exit("tar failed")

# 4. static inspection (text only; nothing is imported or executed)
root = Q / "package"
files = [p for p in root.rglob("*") if p.is_file()]
types = collections.Counter((p.suffix.lower() or "(none)") for p in files)
pj = json.loads((root / "package.json").read_text(errors="replace")) if (root / "package.json").exists() else {}
scripts = pj.get("scripts") or {}
life = {k: (k in scripts) for k in ("preinstall", "install", "postinstall")}
deps = {k: len(pj.get(k) or {}) for k in ("dependencies", "devDependencies", "optionalDependencies",
                                          "peerDependencies", "bundledDependencies")}
pats = {
    "child_process": r"child_process",
    "eval": r"\beval\s*\(|new\s+Function\s*\(",
    "network": r"require\(\s*['\"](?:node:)?(?:https?|net|dns|tls|dgram|http2)['\"]\s*\)|from\s+['\"](?:node:)?(?:https?|net|dns|tls|dgram|http2)['\"]|\bfetch\s*\(|XMLHttpRequest|WebSocket|\baxios\b|https?://",
    "long_base64": r"[A-Za-z0-9+/]{100,}={0,2}",
}
hits = collections.Counter()
files_with = collections.Counter()
for p in files:
    t = p.read_text(errors="ignore")
    for k, rx in pats.items():
        n = len(re.findall(rx, t))
        hits[k] += n
        files_with[k] += bool(n)
# URLs in package.json metadata (homepage/repository) are counted separately from code
net_in_pkgjson = len(re.findall(pats["network"], (root / "package.json").read_text(errors="ignore"))) \
    if (root / "package.json").exists() else 0

# registry document: versions and maintainers (counts only)
n_versions = len(doc.get("versions") or {})
n_maint = len(doc.get("maintainers") or [])
size = len(raw)

L = ["# Static check: the registered group-A npm package (no install, no execution)", "",
     f"- Versions in the registry document: {n_versions} (checked: dist-tags.latest); maintainers: {n_maint}",
     f"- Tarball: {size} bytes; sha1 matches registry shasum: {ok_shasum}; sha512 integrity matches: {ok_integrity}",
     f"- Tar members: {len(members)}; unsafe members: 0",
     "- Lifecycle scripts: " + ", ".join(f"{k} {'yes' if v else 'no'}" for k, v in life.items())
     + f"; scripts total: {len(scripts)} ({', '.join(sorted(scripts)) or 'none'})",
     "- Dependencies: " + ", ".join(f"{k} {v}" for k, v in deps.items()),
     f"- Files: {len(files)}; by type: " + ", ".join(f"{k} {v}" for k, v in sorted(types.items())),
     "- Pattern matches (all files, text scan):"]
for k in pats:
    L.append(f"  - {k}: {'yes' if hits[k] else 'no'} ({hits[k]} matches in {files_with[k]} files)")
L.append(f"  - of the network matches, in package.json metadata (e.g. repository/homepage URLs): {net_in_pkgjson}")
L.append("")
(Q / "report.md").write_text("\n".join(L))
print("\n".join(L))
print("report sha256", hashlib.sha256((Q / "report.md").read_bytes()).hexdigest())
