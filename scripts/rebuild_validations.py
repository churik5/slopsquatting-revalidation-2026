"""Rebuild the `validations` table (revalidation task, item 2).

Writes ONLY to a COPY of the database (default /tmp/runs-snap.db). It refuses to
run against the original data/runs.db. `generations` and `heuristic_hits` are
never written — the name universe is taken from the existing `validations` keyset
(the exact set the paper's numbers were computed from), and we only recompute the
two data-quality columns:

  * exists_in_master : membership in the FRESH master list (data/master_lists/<date>/)
  * live_status      : real HTTP status from the registry JSON API

Live client is polite: custom UA, HTTP/1.1, on-disk cache (one network hit per
name, ever), per-registry rate limit, retry+backoff on 429/5xx honouring
Retry-After. PyPI and npm run on two threads so each host sees only its own
polite rate. Fully resumable via the cache.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from urllib.parse import quote

import diskcache
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from slop_bench.config import CACHE_DIR, MASTER_LISTS_DIR, USER_AGENT  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
ORIGINAL_DB = (REPO / "data" / "runs.db").resolve()

PYPI_URL = "https://pypi.org/pypi/{name}/json"
NPM_URL = "https://registry.npmjs.org/{name}"
RETRY_STATUS = {429, 500, 502, 503, 504}
RATE = {"pypi": 0.10, "npm": 0.20}  # min seconds between requests, per registry


# ── fresh master lists ────────────────────────────────────────────────────────
def latest_lists_dir() -> Path:
    dated = sorted(p for p in MASTER_LISTS_DIR.iterdir()
                   if p.is_dir() and len(p.name) == 10 and p.name[4] == "-")
    if not dated:
        sys.exit("no dated master_lists/<YYYY-MM-DD>/ dir found — run fetch_master_lists.py first")
    return dated[-1]


def load_fresh(lists_dir: Path) -> dict[str, frozenset[str]]:
    out = {}
    for reg, fname in (("pypi", "pypi_package_names.csv"), ("npm", "npm_package_names.csv")):
        p = lists_dir / fname
        print(f"loading {p.name} …", flush=True)
        with p.open(encoding="utf-8") as f:
            out[reg] = frozenset(line.strip() for line in f if line.strip())
        print(f"  {reg}: {len(out[reg])} names", flush=True)
    return out


# ── exists_in_master recompute (fast, no network) ─────────────────────────────
def recompute_exists(conn: sqlite3.Connection, fresh: dict[str, frozenset[str]]) -> dict:
    rows = conn.execute(
        "SELECT package_norm, registry, exists_in_master FROM validations"
    ).fetchall()
    flip_0to1, flip_1to0, updates = [], [], []
    for norm, reg, old in rows:
        new = 1 if norm in fresh[reg] else 0
        if new != old:
            (flip_0to1 if new == 1 else flip_1to0).append((reg, norm))
            updates.append((new, norm, reg))
    conn.executemany(
        "UPDATE validations SET exists_in_master=? WHERE package_norm=? AND registry=?",
        updates,
    )
    conn.commit()
    print(f"exists_in_master: {len(rows)} rows, {len(updates)} changed "
          f"({len(flip_0to1)} 0→1 now-exist, {len(flip_1to0)} 1→0 vanished)", flush=True)
    return {"n_rows": len(rows), "flip_0to1": flip_0to1, "flip_1to0": flip_1to0}


# ── live_status (network, threaded, cached) ───────────────────────────────────
def _encode(name: str) -> str:
    # keep '@' literal (npm scopes), encode everything else incl. the scope slash
    return quote(name, safe="@")


def _live_worker(registry: str, names: list[str], cache, results: dict, lock) -> None:
    url_tmpl = PYPI_URL if registry == "pypi" else NPM_URL
    min_int = RATE[registry]
    done = 0
    with httpx.Client(http2=False, headers={"User-Agent": USER_AGENT},
                      timeout=httpx.Timeout(15.0, read=30.0), follow_redirects=True) as client:
        for name in names:
            ckey = f"{registry}:{name}"
            if ckey in cache:
                status = cache[ckey]
            else:
                status = _fetch_status(client, url_tmpl.format(name=_encode(name)), min_int)
                cache[ckey] = status
            with lock:
                results[(name, registry)] = status
            done += 1
            if done % 250 == 0:
                print(f"  live[{registry}]: {done}/{len(names)}", flush=True)
    print(f"  live[{registry}]: done {done}/{len(names)}", flush=True)


def _fetch_status(client: httpx.Client, url: str, min_int: float) -> int:
    delay = 2.0
    for attempt in range(6):
        try:
            r = client.get(url)
            if r.status_code in RETRY_STATUS:
                wait = float(r.headers.get("retry-after", delay))
                time.sleep(min(wait, 60))
                delay = min(delay * 2, 60)
                continue
            time.sleep(min_int)
            return r.status_code
        except httpx.HTTPError:
            if attempt == 5:
                return -1
            time.sleep(delay)
            delay = min(delay * 2, 60)
    return -1


def live_check(conn: sqlite3.Connection, cache_dir: Path) -> dict:
    rows = conn.execute("SELECT package_norm, registry FROM validations").fetchall()
    by_reg: dict[str, list[str]] = {"pypi": [], "npm": []}
    for norm, reg in rows:
        by_reg[reg].append(norm)
    print(f"live_status: {len(by_reg['pypi'])} pypi + {len(by_reg['npm'])} npm names", flush=True)

    cache = diskcache.Cache(str(cache_dir))
    results: dict = {}
    lock = threading.Lock()
    threads = [
        threading.Thread(target=_live_worker, args=(reg, by_reg[reg], cache, results, lock))
        for reg in ("pypi", "npm")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    cache.close()

    conn.executemany(
        "UPDATE validations SET live_status=? WHERE package_norm=? AND registry=?",
        [(st, n, r) for (n, r), st in results.items()],
    )
    conn.commit()
    dist = Counter(results.values())
    print("live_status distribution:", dict(sorted(dist.items(), key=lambda x: -x[1])), flush=True)
    return {"n": len(results), "dist": dict(dist)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/tmp/runs-snap.db")
    ap.add_argument("--lists-dir", default=None)
    ap.add_argument("--no-live", action="store_true", help="only recompute exists_in_master")
    args = ap.parse_args()

    db = Path(args.db).resolve()
    if db == ORIGINAL_DB or db.name == "runs.db" and db.parent == ORIGINAL_DB.parent:
        sys.exit(f"REFUSING to write to the original database: {db}")
    if not db.exists():
        sys.exit(f"snapshot DB not found: {db} (create it with sqlite3 .backup first)")
    lists_dir = Path(args.lists_dir) if args.lists_dir else latest_lists_dir()
    print(f"DB (copy):   {db}\nlists:       {lists_dir}\n", flush=True)

    fresh = load_fresh(lists_dir)
    conn = sqlite3.connect(db)
    recompute_exists(conn, fresh)
    if not args.no_live:
        cache_dir = CACHE_DIR / "registry_live"
        cache_dir.mkdir(parents=True, exist_ok=True)
        live_check(conn, cache_dir)
    conn.close()
    print("\nDone (item 2).", flush=True)


if __name__ == "__main__":
    main()
