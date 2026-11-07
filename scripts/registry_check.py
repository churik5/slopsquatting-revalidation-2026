"""Live registry checks for Task 1 (PREREG.md §2, PREREG-deviations.md D0-D12).

    PY=~/.local/share/slop-bench/venv-check/bin/python
    caffeinate -i $PY scripts/registry_check.py --round 1
    caffeinate -i $PY scripts/registry_check.py --round 2   # >= 12 h after round 1 ended
    $PY scripts/registry_check.py --summary 1               # rewrite reports/round1_summary.json

The interpreter is a plain venv outside iCloud Drive (python3.11 -m venv; pip install httpx==0.28.1):
the project .venv lives in an iCloud-synced folder, its files get evicted, and imports can block.

What is checked (lists stay in data/private/; nothing name-bearing is printed):
  * role "study"   — every name of groups A and B after exclusions (offline baseline files);
  * role "mapped"  — for PyPI study names that are pipreqs-mapping keys, the mapped distribution(s);
  * role "control" — 200 random names per ecosystem that v3 counted as existing (seed 42).
    Quality check only; never part of the main numbers.

Methods (state per PREREG §2: EXISTS = 200 + valid document, ABSENT = 404, else UNKNOWN):
  pypi_json    GET https://pypi.org/pypi/<pep503>/json
  pypi_simple  GET https://pypi.org/simple/<pep503>/  (Accept: application/vnd.pypi.simple.v1+json)
  npm_registry GET https://registry.npmjs.org/<name>  ('/' in scoped names as %2F)
  depsdev      GET https://api.deps.dev/v3/systems/npm/packages/<encodeURIComponent(name)>
  osv          POST https://api.osv.dev/v1/query {"package": {"name", "ecosystem"}}  -> MAL / NO_MAL / UNKNOWN

Every HTTP response (and every failed attempt) is a new row of the append-only table
registry_checks in data/private/registry_checks.db; triggers forbid UPDATE and DELETE.
At most 1 request per second per host (pypi_json and pypi_simple share pypi.org).
429 / 5xx / timeout / network error: retry with pause (Retry-After respected), at most 5
attempts, then UNKNOWN. Redirects are followed; original and final URL are recorded.
Resumable: (round, name, ecosystem, method) with a final row is never requested again.
"""
from __future__ import annotations

import argparse
import email.utils
import hashlib
import json
import queue
import random
import re
import sqlite3
import sys
import threading
import time
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

REPO = Path(__file__).resolve().parents[1]
PRIVATE = REPO / "data" / "private"
GROUPS = PRIVATE / "prereg"
OUT_DB = PRIVATE / "registry_checks.db"
LOG = PRIVATE / "registry_check.log"
RUNS_DB = REPO / "data" / "runs.db"
PIPREQS = REPO / "data" / "reference" / "pipreqs_mapping.txt"
REPORTS = REPO / "reports"

USER_AGENT = "slop-bench-revalidation/1.0 (research; contact: churik0509@gmail.com)"
MIN_INTERVAL = 1.05          # seconds between request starts on one host (limit: 1/s)
MAX_ATTEMPTS = 5
TIMEOUT = httpx.Timeout(60.0, connect=20.0)
ROUND2_DELAY = timedelta(hours=12)
CONTROL_N = 200
SEED = 42

ENDPOINTS = {
    "pypi_json": "GET https://pypi.org/pypi/{pep503_name}/json",
    "pypi_simple": "GET https://pypi.org/simple/{pep503_name}/ with Accept: application/vnd.pypi.simple.v1+json",
    "npm_registry": "GET https://registry.npmjs.org/{name with '/' as %2F}",
    "depsdev": "GET https://api.deps.dev/v3/systems/npm/packages/{name, fully percent-encoded ('@'->%40, '/'->%2F)} "
               "(deps.dev API v3 GetPackage, https://docs.deps.dev/api/v3/; 404 = not found, checked 2026-11-07)",
    "osv": "POST https://api.osv.dev/v1/query {\"package\": {\"name\": name, \"ecosystem\": \"PyPI\"|\"npm\"}} "
           "(no version = all records; follows next_page_token); MAL = any id starting with 'MAL-'",
}
METHODS = {"pypi": ("pypi_json", "pypi_simple", "osv"), "npm": ("npm_registry", "depsdev", "osv")}
REGISTRY_METHODS = {"pypi": ("pypi_json", "pypi_simple"), "npm": ("npm_registry", "depsdev")}
HOST_OF = {"pypi_json": "pypi.org", "pypi_simple": "pypi.org", "npm_registry": "registry.npmjs.org",
           "depsdev": "api.deps.dev", "osv": "api.osv.dev"}

_PEP503 = re.compile(r"[-_.]+")


def norm_pypi(n: str) -> str:
    return _PEP503.sub("-", n).lower()


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def log(msg: str) -> None:
    """Counts only — callers never pass names."""
    line = f"{datetime.now(timezone.utc).strftime('%H:%M:%S')}Z {msg}"
    print(line, flush=True)


def private_log(msg: str) -> None:
    with open(LOG, "a") as f:
        f.write(f"{now_utc()} {msg}\n")


# ── storage ───────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS registry_checks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    round           INTEGER NOT NULL,
    name            TEXT NOT NULL,
    ecosystem       TEXT NOT NULL,
    method          TEXT NOT NULL,
    url             TEXT NOT NULL,
    final_url       TEXT,
    http_status     INTEGER,
    state           TEXT NOT NULL,       -- EXISTS | ABSENT | UNKNOWN | MAL | NO_MAL | RETRY
    body_sha256     TEXT,
    checked_at_utc  TEXT NOT NULL,
    attempt         INTEGER NOT NULL,
    is_final        INTEGER NOT NULL,    -- 1 = last row for (round, name, ecosystem, method)
    error           TEXT,
    meta_json       TEXT
);
CREATE INDEX IF NOT EXISTS idx_rc_key ON registry_checks(round, ecosystem, method, name, is_final);
CREATE TRIGGER IF NOT EXISTS registry_checks_no_update BEFORE UPDATE ON registry_checks
BEGIN SELECT RAISE(ABORT, 'registry_checks is append-only'); END;
CREATE TRIGGER IF NOT EXISTS registry_checks_no_delete BEFORE DELETE ON registry_checks
BEGIN SELECT RAISE(ABORT, 'registry_checks is append-only'); END;

CREATE TABLE IF NOT EXISTS targets (
    round     INTEGER NOT NULL,
    name      TEXT NOT NULL,
    ecosystem TEXT NOT NULL,
    role      TEXT NOT NULL,              -- study | mapped | control
    UNIQUE(round, name, ecosystem, role)
);
CREATE TRIGGER IF NOT EXISTS targets_no_update BEFORE UPDATE ON targets
BEGIN SELECT RAISE(ABORT, 'targets is append-only'); END;
CREATE TRIGGER IF NOT EXISTS targets_no_delete BEFORE DELETE ON targets
BEGIN SELECT RAISE(ABORT, 'targets is append-only'); END;

CREATE TABLE IF NOT EXISTS run_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    round   INTEGER NOT NULL,
    event   TEXT NOT NULL,                -- start | end
    at_utc  TEXT NOT NULL,
    info    TEXT
);
CREATE TRIGGER IF NOT EXISTS run_events_no_update BEFORE UPDATE ON run_events
BEGIN SELECT RAISE(ABORT, 'run_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS run_events_no_delete BEFORE DELETE ON run_events
BEGIN SELECT RAISE(ABORT, 'run_events is append-only'); END;
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()

    def add_check(self, row: dict) -> None:
        cols = ("round", "name", "ecosystem", "method", "url", "final_url", "http_status", "state",
                "body_sha256", "checked_at_utc", "attempt", "is_final", "error", "meta_json")
        with self.lock:
            self.conn.execute(f"INSERT INTO registry_checks ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                              [row.get(c) for c in cols])

    def add_targets(self, rnd: int, rows) -> None:
        with self.lock:
            self.conn.executemany("INSERT OR IGNORE INTO targets (round, name, ecosystem, role) VALUES (?,?,?,?)",
                                  [(rnd, n, e, r) for n, e, r in rows])

    def event(self, rnd: int, ev: str, info: dict | None = None) -> None:
        with self.lock:
            self.conn.execute("INSERT INTO run_events (round, event, at_utc, info) VALUES (?,?,?,?)",
                              (rnd, ev, now_utc(), json.dumps(info or {}, sort_keys=True)))

    def done(self, rnd: int) -> set[tuple[str, str, str]]:
        return {tuple(r) for r in self.conn.execute(
            "SELECT name, ecosystem, method FROM registry_checks WHERE round=? AND is_final=1", (rnd,))}


# ── targets ───────────────────────────────────────────────────────────────────
def list_sha256(names) -> str:
    s = sorted(names)
    return hashlib.sha256(("\n".join(s) + "\n" if s else "").encode()).hexdigest()


def read_group(g: str, eco: str) -> list[str]:
    """Exact group names from the JSON written by prereg_baseline.py, verified against the
    committed group hash in reports/prereg_baseline.json (some names contain '\n')."""
    names = json.loads((GROUPS / f"group_{g}_{eco}.json").read_text())
    want = json.loads((REPORTS / "prereg_baseline.json").read_text())["groups"][eco]
    if len(names) != want[g] or list_sha256(names) != want["sha256"][g]:
        raise SystemExit(f"group {g}/{eco} does not match the committed baseline")
    return names


def load_pipreqs() -> dict[str, set[str]]:
    m: dict[str, set[str]] = defaultdict(set)
    for line in PIPREQS.read_text().splitlines():
        if ":" in line:
            imp, dist = line.split(":", 1)
            m[norm_pypi(imp.strip())].add(norm_pypi(dist.strip()))
    return m


def control_sample() -> list[tuple[str, str, str]]:
    conn = sqlite3.connect(f"file:{RUNS_DB}?mode=ro", uri=True)
    rows = conn.execute("SELECT package_norm, registry FROM validations WHERE exists_in_master = 1").fetchall()
    conn.close()
    out = []
    for eco, norm in (("pypi", norm_pypi), ("npm", str.lower)):
        pool = sorted({norm(n) for n, r in rows if r == eco})
        out += [(n, eco, "control") for n in random.Random(SEED).sample(pool, CONTROL_N)]
    return out


def round1_targets() -> list[tuple[str, str, str]]:
    t = []
    pmap = load_pipreqs()
    for eco in ("pypi", "npm"):
        for g in ("A", "B"):
            t += [(n, eco, "study") for n in read_group(g, eco)]
    study_pypi = {n for n, e, _ in t if e == "pypi"}
    t += [(d, "pypi", "mapped") for n in sorted(study_pypi) if n in pmap for d in sorted(pmap[n])]
    t += control_sample()
    return t


def round1_end(store: Store) -> datetime | None:
    r = store.conn.execute("SELECT MAX(at_utc) FROM run_events WHERE round=1 AND event='end'").fetchone()[0]
    return datetime.strptime(r, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc) if r else None


def round2_targets(store: Store) -> list[tuple[str, str, str]]:
    """(name, eco) with ABSENT or UNKNOWN in at least one registry method in round 1; roles copied."""
    rows = store.conn.execute(
        "SELECT name, ecosystem, method, state FROM registry_checks WHERE round=1 AND is_final=1").fetchall()
    flag = set()
    for n, e, m, s in rows:
        if m in REGISTRY_METHODS[e] and s in ("ABSENT", "UNKNOWN"):
            flag.add((n, e))
    roles = store.conn.execute("SELECT name, ecosystem, role FROM targets WHERE round=1").fetchall()
    return [(n, e, r) for n, e, r in roles if (n, e) in flag]


# ── requests and classification ───────────────────────────────────────────────
def build_request(method: str, name: str, eco: str) -> tuple[str, str, dict, bytes | None]:
    if method == "pypi_json":
        return "GET", f"https://pypi.org/pypi/{quote(norm_pypi(name), safe='')}/json", {}, None
    if method == "pypi_simple":
        return ("GET", f"https://pypi.org/simple/{quote(norm_pypi(name), safe='')}/",
                {"Accept": "application/vnd.pypi.simple.v1+json"}, None)
    if method == "npm_registry":
        return "GET", f"https://registry.npmjs.org/{quote(name, safe='@')}", {}, None
    if method == "depsdev":
        return "GET", f"https://api.deps.dev/v3/systems/npm/packages/{quote(name, safe='')}", {}, None
    if method == "osv":
        body = {"package": {"name": name, "ecosystem": "PyPI" if eco == "pypi" else "npm"}}
        return "POST", "https://api.osv.dev/v1/query", {"Content-Type": "application/json"}, json.dumps(body).encode()
    raise ValueError(method)


def _min_time(values) -> str | None:
    vals = sorted(v for v in values if v)
    return vals[0] if vals else None


def classify(method: str, status: int, body: bytes) -> tuple[str, dict]:
    """Return (state, metadata) for one non-retryable response."""
    if method == "osv":
        if status != 200:
            return "UNKNOWN", {}
        try:
            d = json.loads(body)
        except ValueError:
            return "UNKNOWN", {"parse_error": True}
        if not isinstance(d, dict):
            return "UNKNOWN", {"parse_error": True}
        ids = [v.get("id") for v in d.get("vulns", []) if isinstance(v, dict)]
        return ("MAL" if any(str(i).startswith("MAL-") for i in ids) else "NO_MAL"), \
            {"vuln_ids": ids, "next_page_token": d.get("next_page_token")}
    if status == 404:
        return "ABSENT", {}
    if status != 200:
        return "UNKNOWN", {}
    try:
        d = json.loads(body)
    except ValueError:
        return "UNKNOWN", {"parse_error": True}
    if not isinstance(d, dict):
        return "UNKNOWN", {"parse_error": True}
    if method == "pypi_json":
        info = d.get("info")
        if not isinstance(info, dict) or not info.get("name"):
            return "UNKNOWN", {"invalid_document": True}
        releases = d.get("releases") or {}
        uploads = [f.get("upload_time_iso_8601") for files in releases.values() for f in (files or [])]
        uploads += [f.get("upload_time_iso_8601") for f in (d.get("urls") or [])]
        empty = sorted(v for v, files in releases.items() if not files)
        return "EXISTS", {"project_name": info.get("name"), "n_releases": len(releases),
                          "earliest_upload": _min_time(uploads), "n_files": len(uploads),
                          "releases_without_files": empty, "flag_release_without_files": bool(empty)}
    if method == "pypi_simple":
        if not d.get("name") or not isinstance(d.get("files"), list):
            return "UNKNOWN", {"invalid_document": True}
        files = d["files"]
        return "EXISTS", {"project_name": d.get("name"), "api_version": (d.get("meta") or {}).get("api-version"),
                          "n_files": len(files), "earliest_upload": _min_time(f.get("upload-time") for f in files),
                          "versions": d.get("versions"), "flag_no_files": not files}
    if method == "npm_registry":
        if not d.get("name"):
            return "UNKNOWN", {"invalid_document": True}
        t = d.get("time") or {}
        versions = sorted((d.get("versions") or {}).keys())
        return "EXISTS", {"package_name": d.get("name"), "time_created": t.get("created"),
                          "time_unpublished": t.get("unpublished"), "versions": versions,
                          "security_holding": versions == ["0.0.1-security"],
                          "dist_tags": d.get("dist-tags")}
    if method == "depsdev":
        vs = d.get("versions")
        if not isinstance(vs, list) or not d.get("packageKey"):
            return "UNKNOWN", {"invalid_document": True}
        return "EXISTS", {"package_key": d.get("packageKey"), "n_versions": len(vs),
                          "earliest_published": _min_time(v.get("publishedAt") for v in vs),
                          "versions": [(v.get("versionKey") or {}).get("version") for v in vs]}
    raise ValueError(method)


def retry_after(resp: httpx.Response | None, attempt: int) -> float:
    base = min(600.0, 5.0 * 2 ** (attempt - 1))
    if resp is not None and resp.headers.get("Retry-After"):
        v = resp.headers["Retry-After"].strip()
        try:
            return max(base, float(v))
        except ValueError:
            dt = email.utils.parsedate_to_datetime(v)
            if dt is not None:
                return max(base, (dt - datetime.now(timezone.utc)).total_seconds())
    return base


class Host:
    """Serializes requests to one host with >= MIN_INTERVAL between starts."""

    def __init__(self, client: httpx.Client):
        self.client = client
        self.last = 0.0

    def send(self, verb, url, headers, content):
        wait = self.last + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()
        return self.client.request(verb, url, headers=headers, content=content)


def check_one(host: Host, store: Store, rnd: int, name: str, eco: str, method: str, stats: Counter) -> None:
    verb, url, headers, content = build_request(method, name, eco)
    page_ids: list = []
    page_token = None
    attempt = 0
    while True:
        attempt += 1
        req_content = content
        if method == "osv" and page_token:
            body = json.loads(content)
            body["page_token"] = page_token
            req_content = json.dumps(body).encode()
        row = {"round": rnd, "name": name, "ecosystem": eco, "method": method, "url": url,
               "attempt": attempt, "checked_at_utc": now_utc()}
        resp = None
        try:
            resp = host.send(verb, url, headers, req_content)
            raw = resp.content
            row.update(final_url=str(resp.url), http_status=resp.status_code,
                       body_sha256=hashlib.sha256(raw).hexdigest(),
                       checked_at_utc=now_utc())
            if resp.history:
                row["meta_redirects"] = [str(h.url) for h in resp.history]
            transient = resp.status_code == 429 or resp.status_code >= 500
        except httpx.TooManyRedirects as e:
            row.update(state="UNKNOWN", is_final=1, error=f"TooManyRedirects: {e}")
            store.add_check(row)
            stats[f"{method}:UNKNOWN"] += 1
            return
        except (httpx.TimeoutException, httpx.TransportError) as e:
            row.update(error=f"{type(e).__name__}: {e}")
            transient = True
        if transient:
            stats["transient"] += 1
            if attempt >= MAX_ATTEMPTS:
                row.update(state="UNKNOWN", is_final=1)
                store.add_check(row)
                stats[f"{method}:UNKNOWN"] += 1
                return
            row.update(state="RETRY", is_final=0)
            store.add_check(row)
            time.sleep(retry_after(resp, attempt))
            continue
        state, meta = classify(method, resp.status_code, raw)
        if "meta_redirects" in row:
            meta["redirect_chain"] = row.pop("meta_redirects")
        if method == "osv" and state in ("MAL", "NO_MAL"):
            page_ids += meta.get("vuln_ids", [])
            meta["all_page_vuln_ids"] = list(page_ids)
            if meta.get("next_page_token"):
                page_token = meta["next_page_token"]
                row.update(state="RETRY", is_final=0, meta_json=json.dumps(meta, sort_keys=True),
                           error="next_page")
                store.add_check(row)
                attempt = 0
                continue
            state = "MAL" if any(str(i).startswith("MAL-") for i in page_ids) else "NO_MAL"
        row.update(state=state, is_final=1, meta_json=json.dumps(meta, sort_keys=True))
        store.add_check(row)
        stats[f"{method}:{state}"] += 1
        return


# ── run ───────────────────────────────────────────────────────────────────────
def run(rnd: int) -> None:
    store = Store(OUT_DB)
    if rnd == 1:
        targets = round1_targets()
        methods_for = METHODS
    else:
        end1 = round1_end(store)
        if end1 is None:
            raise SystemExit("round 1 has no 'end' event; finish round 1 first")
        earliest = end1 + ROUND2_DELAY
        if datetime.now(timezone.utc) < earliest:
            raise SystemExit(f"round 2 may start at {earliest.strftime('%Y-%m-%dT%H:%M:%SZ')} or later")
        targets = round2_targets(store)
        methods_for = REGISTRY_METHODS
    store.add_targets(rnd, targets)
    pairs = sorted({(n, e) for n, e, _ in targets})
    done = store.done(rnd)
    work: dict[str, list] = defaultdict(list)
    for n, e in pairs:
        for m in methods_for[e]:
            if (n, e, m) not in done:
                work[HOST_OF[m]].append((n, e, m))
    total = sum(len(v) for v in work.values())
    log(f"round {rnd}: {len(pairs)} (name, ecosystem) pairs; {len(done)} checks already final; "
        f"{total} to do: " + ", ".join(f"{h}={len(v)}" for h, v in sorted(work.items())))
    store.event(rnd, "start", {"pairs": len(pairs), "todo": total, "already_final": len(done)})

    stats: Counter = Counter()
    progress: Counter = Counter()
    errors: list = []
    headers = {"User-Agent": USER_AGENT}

    def worker(hostname: str, items: list):
        with httpx.Client(headers=headers, timeout=TIMEOUT, follow_redirects=True, max_redirects=10,
                          http2=False) as client:
            host = Host(client)
            for i, (n, e, m) in enumerate(items, 1):
                try:
                    check_one(host, store, rnd, n, e, m, stats)
                except Exception:  # never let one name stop the host; details only in the private log
                    errors.append(hostname)
                    private_log(f"round {rnd} {hostname} {e} {m} {n}\n{traceback.format_exc()}")
                progress[hostname] = i
                if i % 250 == 0 or i == len(items):
                    log(f"{hostname}: {i}/{len(items)}")

    threads = [threading.Thread(target=worker, args=(h, items), daemon=True) for h, items in sorted(work.items())]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    remaining = sum(1 for n, e in pairs for m in methods_for[e] if (n, e, m) not in store.done(rnd))
    log(f"round {rnd} finished: {sum(progress.values())} checks processed, {len(errors)} unexpected errors, "
        f"{remaining} without a final row")
    if remaining == 0:
        store.event(rnd, "end", {"unexpected_errors": len(errors)})
        write_summary(store, rnd)
        if rnd == 1:
            log(f"round 2 may start at {(round1_end(store) + ROUND2_DELAY).strftime('%Y-%m-%dT%H:%M:%SZ')} or later")
    else:
        log("not complete: rerun the same command to resume")


# ── summary (aggregates only; no A/B split) ───────────────────────────────────
def write_summary(store: Store, rnd: int) -> dict:
    c = store.conn
    rows = c.execute("SELECT name, ecosystem, method, state, http_status, error, is_final FROM registry_checks "
                     "WHERE round=?", (rnd,)).fetchall()
    roles = defaultdict(set)
    for n, e, r in c.execute("SELECT name, ecosystem, role FROM targets WHERE round=?", (rnd,)):
        roles[(n, e)].add(r)

    def role_bucket(n, e):  # study wins over mapped wins over control
        rs = roles[(n, e)]
        return "study" if "study" in rs else ("mapped" if "mapped" in rs else "control")

    requests = {"total_rows": len(rows), "http_requests_without_response": sum(1 for r in rows if r[4] is None),
                "by_method": dict(Counter(f"{r[1]}.{r[2]}" for r in rows)),
                "by_http_status": {str(k): v for k, v in sorted(Counter(r[4] for r in rows).items(),
                                                                 key=lambda x: (x[0] is None, x[0] or 0))}}
    final = {}
    states = defaultdict(Counter)
    per_name = defaultdict(dict)
    for n, e, m, s, st, err, fin in rows:
        if fin:
            b = role_bucket(n, e)
            states[f"{b}.{e}.{m}"][s] += 1
            final[(n, e, m)] = s
            per_name[(b, n, e)][m] = s
    disagreements = {}
    for b in ("study", "mapped", "control"):
        for e in ("pypi", "npm"):
            m1, m2 = REGISTRY_METHODS[e]
            pairs = [v for (bb, n, ee), v in per_name.items() if bb == b and ee == e]
            if not pairs:
                continue
            both = [(v.get(m1), v.get(m2)) for v in pairs]
            disagreements[f"{b}.{e}"] = {
                "names": len(pairs),
                "both_EXISTS": sum(1 for a, z in both if a == z == "EXISTS"),
                "both_ABSENT": sum(1 for a, z in both if a == z == "ABSENT"),
                "EXISTS_vs_ABSENT": sum(1 for a, z in both if {a, z} == {"EXISTS", "ABSENT"}),
                "one_or_both_UNKNOWN": sum(1 for a, z in both if "UNKNOWN" in (a, z)),
                "missing_final_row": sum(1 for a, z in both if a is None or z is None)}
    errors = {"retried_responses_or_failures": sum(1 for r in rows if r[3] == "RETRY" and r[5] != "next_page"),
              "http_429": sum(1 for r in rows if r[4] == 429),
              "http_5xx": sum(1 for r in rows if r[4] and r[4] >= 500),
              "timeouts_or_network_errors": sum(1 for r in rows if r[4] is None),
              "final_UNKNOWN": sum(1 for r in rows if r[6] and r[3] == "UNKNOWN"),
              "other_non_200_non_404_final": sum(1 for r in rows if r[6] and r[4] not in (None, 200, 404))}
    ev = c.execute("SELECT event, MIN(at_utc), MAX(at_utc) FROM run_events WHERE round=? GROUP BY event", (rnd,)).fetchall()
    evd = {e: (a, z) for e, a, z in ev}
    t0 = json.loads((REPORTS / "t0.json").read_text())
    end = round1_end(store) if rnd == 1 else None
    out = {
        "round": rnd, "t0_utc": t0["t0_utc"],
        "first_start_utc": evd.get("start", (None,))[0], "end_utc": evd.get("end", (None, None))[1],
        "first_check_utc": c.execute("SELECT MIN(checked_at_utc) FROM registry_checks WHERE round=?", (rnd,)).fetchone()[0],
        "last_check_utc": c.execute("SELECT MAX(checked_at_utc) FROM registry_checks WHERE round=?", (rnd,)).fetchone()[0],
        "targets": {f"{r}.{e}": v for (r, e), v in sorted(Counter(
            (r, e) for n, e, r in c.execute("SELECT name, ecosystem, role FROM targets WHERE round=?", (rnd,))).items())},
        "role_note": "study = groups A and B together (no split); mapped = pipreqs-mapped distributions of "
                     "PyPI study names; control = 200 random v3-existing names per ecosystem (quality check only). "
                     "A name with several roles is counted once: study > mapped > control.",
        "requests": requests,
        "final_states": {k: dict(sorted(v.items())) for k, v in sorted(states.items())},
        "method_disagreements": disagreements,
        "errors": errors,
        "endpoints": ENDPOINTS, "user_agent": USER_AGENT,
        "rate_limit": f"<= 1 request/s per host (min interval {MIN_INTERVAL}s; pypi_json + pypi_simple share pypi.org)",
        "retries": f"429/5xx/timeout/network: up to {MAX_ATTEMPTS} attempts, pause max(Retry-After, 5*2^(n-1) s)",
        "round2": {"eligible_pairs": len({(n, e) for n, e, _ in round2_targets(store)}) if rnd == 1 else None,
                   "earliest_start_utc": (end + ROUND2_DELAY).strftime("%Y-%m-%dT%H:%M:%SZ") if end else None,
                   "command": "caffeinate -i ~/.local/share/slop-bench/venv-check/bin/python scripts/registry_check.py --round 2"},
        "storage": "data/private/registry_checks.db (append-only; not committed)",
    }
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / f"round{rnd}_summary.json").write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    log(f"wrote reports/round{rnd}_summary.json")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--round", type=int, choices=(1, 2))
    g.add_argument("--summary", type=int, choices=(1, 2))
    a = ap.parse_args()
    if a.summary:
        write_summary(Store(OUT_DB), a.summary)
    else:
        run(a.round)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        private_log(traceback.format_exc())
        print(f"failed: {sys.exc_info()[0].__name__} (details in data/private/registry_check.log)", file=sys.stderr)
        sys.exit(1)
