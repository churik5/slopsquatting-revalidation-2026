"""Tests for scripts/prereg_baseline.py: normalization, network block, no names in reports."""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "prereg_baseline.py"
PRIVATE = REPO / "data" / "private" / "prereg"


def _load():
    spec = importlib.util.spec_from_file_location("prereg_baseline", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # importing does not block the network (only __main__ does)
    return mod


pb = _load()


# ── normalization ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ("Django", "django"),
    ("zope.interface", "zope-interface"),
    ("typing_extensions", "typing-extensions"),
    ("Foo__Bar", "foo-bar"),
    ("foo-_.bar", "foo-bar"),          # a run of mixed separators collapses to one '-'
    ("A.B-C_D", "a-b-c-d"),
    ("PIL", "pil"),
    ("already-normal", "already-normal"),
])
def test_pep503(raw, expected):
    assert pb.norm_pypi(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("React", "react"),
    ("@Babel/Core", "@babel/core"),        # scope kept, lowercased
    ("@types/node", "@types/node"),
    ("@scope/My_Pkg.js", "@scope/my_pkg.js"),  # npm does not collapse separators
    ("lodash.debounce", "lodash.debounce"),
])
def test_npm_norm(raw, expected):
    assert pb.norm_npm(raw) == expected


def test_npm_scope_not_merged():
    assert pb.norm_npm("@a/b") != pb.norm_npm("a/b")
    assert pb.norm_npm("@a/b").startswith("@a/")


def test_list_hash_format():
    import hashlib
    assert pb.list_sha256(["b", "a"]) == hashlib.sha256(b"a\nb\n").hexdigest()


# ── network block ─────────────────────────────────────────────────────────────
_NET_PROBE = r"""
import importlib.util, socket, sys, urllib.request
spec = importlib.util.spec_from_file_location("pb", sys.argv[1])
pb = importlib.util.module_from_spec(spec); spec.loader.exec_module(pb)
pb.block_network()
failures = 0
attempts = [
    lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("127.0.0.1", 9)),
    lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect_ex(("127.0.0.1", 9)),
    lambda: socket.create_connection(("pypi.org", 443), timeout=1),
    lambda: socket.getaddrinfo("registry.npmjs.org", 443),
    lambda: urllib.request.urlopen("https://pypi.org/simple/", timeout=1),
]
for f in attempts:
    try:
        f()
    except Exception as e:
        chain = [e, getattr(e, "reason", None), e.__cause__, e.__context__]
        if any(isinstance(x, pb.NetworkBlockedError) for x in chain):
            failures += 1
print(failures, len(attempts))
"""


def test_network_blocked():
    out = subprocess.run([sys.executable, "-c", _NET_PROBE, str(SCRIPT)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    blocked, total = map(int, out.stdout.split())
    assert blocked == total


def test_script_blocks_network_before_heavy_imports():
    src = SCRIPT.read_text()
    assert src.index("block_network()\n") < src.index("import sqlite3")


# ── no names from A in the committed reports ──────────────────────────────────
def _group_a():
    files = sorted(PRIVATE.glob("group_A_*.txt"))
    if not files:
        pytest.skip("private group A lists not present (run scripts/prereg_baseline.py)")
    names = set()
    for f in files:
        names |= {x for x in f.read_text().splitlines() if x}
    assert names
    return names


@pytest.mark.parametrize("report", ["prereg_baseline.json", "prereg_baseline.md"])
def test_no_group_a_name_in_report(report):
    path = REPO / "reports" / report
    if not path.exists():
        pytest.skip("report not generated")
    names = _group_a()
    text = path.read_text().lower()
    tokens = set(re.findall(r"[a-z0-9@][a-z0-9_.@/\-]*", text))
    leaked = names & tokens
    if report.endswith(".json"):
        data = json.loads(path.read_text())

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    yield str(k).lower()
                    yield from walk(v)
            elif isinstance(o, list):
                for v in o:
                    yield from walk(v)
            elif isinstance(o, str):
                yield o.lower()
        leaked |= names & set(walk(data))
    n_leaked = len(leaked)
    del leaked  # keep pytest's assertion introspection from printing any name
    assert n_leaked == 0, f"{n_leaked} group-A name(s) found in {report}"
