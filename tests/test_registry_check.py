"""Offline tests for scripts/registry_check.py (httpx.MockTransport; no network)."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from collections import Counter
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "registry_check.py"
spec = importlib.util.spec_from_file_location("registry_check", SCRIPT)
rc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rc)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(rc, "MIN_INTERVAL", 0.0)
    monkeypatch.setattr(rc.time, "sleep", lambda s: None)


def run_one(tmp_path, handler, method="pypi_json", eco="pypi", name="probe-pkg"):
    store = rc.Store(tmp_path / "c.db")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    stats = Counter()
    rc.check_one(rc.Host(client), store, 1, name, eco, method, stats)
    rows = store.conn.execute("SELECT url, final_url, http_status, state, attempt, is_final, meta_json, body_sha256 "
                              "FROM registry_checks ORDER BY id").fetchall()
    return store, rows


def test_urls():
    assert rc.build_request("pypi_json", "Foo_Bar.baz", "pypi")[1] == "https://pypi.org/pypi/foo-bar-baz/json"
    v, u, h, _ = rc.build_request("pypi_simple", "X", "pypi")
    assert u == "https://pypi.org/simple/x/" and h["Accept"] == "application/vnd.pypi.simple.v1+json"
    assert rc.build_request("npm_registry", "@scope/name", "npm")[1] == "https://registry.npmjs.org/@scope%2Fname"
    assert rc.build_request("depsdev", "@scope/name", "npm")[1] == \
        "https://api.deps.dev/v3/systems/npm/packages/%40scope%2Fname"
    v, u, h, body = rc.build_request("osv", "x", "pypi")
    assert v == "POST" and json.loads(body)["package"]["ecosystem"] == "PyPI"


def test_absent(tmp_path):
    _, rows = run_one(tmp_path, lambda r: httpx.Response(404, text="nope"))
    assert [(r[2], r[3], r[5]) for r in rows] == [(404, "ABSENT", 1)]


def test_pypi_exists_metadata(tmp_path):
    doc = {"info": {"name": "probe"}, "releases": {
        "1.0": [{"upload_time_iso_8601": "2026-05-02T10:00:00Z"}],
        "0.1": [{"upload_time_iso_8601": "2026-05-01T09:00:00Z"}], "0.0": []}, "urls": []}
    _, rows = run_one(tmp_path, lambda r: httpx.Response(200, json=doc))
    meta = json.loads(rows[0][6])
    assert rows[0][3] == "EXISTS" and meta["earliest_upload"] == "2026-05-01T09:00:00Z"
    assert meta["flag_release_without_files"] and meta["releases_without_files"] == ["0.0"]


def test_npm_security_holding_and_unpublished(tmp_path):
    doc = {"name": "probe", "time": {"created": "2026-06-01T00:00:00Z"}, "versions": {"0.0.1-security": {}}}
    _, rows = run_one(tmp_path, lambda r: httpx.Response(200, json=doc), method="npm_registry", eco="npm")
    meta = json.loads(rows[0][6])
    assert meta["security_holding"] and meta["time_created"] == "2026-06-01T00:00:00Z"
    doc2 = {"name": "probe", "time": {"created": "x", "unpublished": {"time": "y"}}}
    state, meta2 = rc.classify("npm_registry", 200, json.dumps(doc2).encode())
    assert state == "EXISTS" and meta2["time_unpublished"] == {"time": "y"}


def test_unparseable_200_is_unknown():
    assert rc.classify("pypi_json", 200, b"<html>")[0] == "UNKNOWN"
    assert rc.classify("depsdev", 200, b"{}")[0] == "UNKNOWN"
    assert rc.classify("pypi_json", 403, b"")[0] == "UNKNOWN"


def test_retry_then_success(tmp_path):
    calls = {"n": 0}

    def h(r):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        if calls["n"] == 2:
            return httpx.Response(503)
        return httpx.Response(404)
    _, rows = run_one(tmp_path, h)
    assert [(r[2], r[3], r[4], r[5]) for r in rows] == [(429, "RETRY", 1, 0), (503, "RETRY", 2, 0), (404, "ABSENT", 3, 1)]


def test_gives_up_after_five(tmp_path):
    def h(r):
        raise httpx.ConnectTimeout("t", request=r)
    _, rows = run_one(tmp_path, h)
    assert len(rows) == 5 and rows[-1][3] == "UNKNOWN" and rows[-1][5] == 1 and rows[-1][2] is None


def test_redirect_recorded(tmp_path):
    def h(r):
        if r.url.path == "/pypi/probe-pkg/json":
            return httpx.Response(301, headers={"Location": "https://pypi.org/pypi/canonical/json"})
        return httpx.Response(200, json={"info": {"name": "canonical"}, "releases": {}})
    _, rows = run_one(tmp_path, h)
    assert rows[0][0].endswith("/probe-pkg/json") and rows[0][1].endswith("/canonical/json")
    assert json.loads(rows[0][6])["redirect_chain"]


def test_osv_mal_and_pagination(tmp_path):
    pages = iter([{"vulns": [{"id": "GHSA-1"}], "next_page_token": "p2"}, {"vulns": [{"id": "MAL-2026-1"}]}])
    _, rows = run_one(tmp_path, lambda r: httpx.Response(200, json=next(pages)), method="osv")
    assert [(r[3], r[5]) for r in rows] == [("RETRY", 0), ("MAL", 1)]
    _, rows = run_one(tmp_path / "b", lambda r: httpx.Response(200, json={}), method="osv")
    assert rows[0][3] == "NO_MAL"


def test_append_only(tmp_path):
    store, _ = run_one(tmp_path, lambda r: httpx.Response(404))
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("UPDATE registry_checks SET state='EXISTS'")
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("DELETE FROM registry_checks")


def test_resume_skips_final(tmp_path):
    store, _ = run_one(tmp_path, lambda r: httpx.Response(404))
    assert ("probe-pkg", "pypi", "pypi_json") in store.done(1)
