"""Fetch FRESH PyPI + npm package-name master lists (revalidation task, item 1).

Writes to data/master_lists/<YYYY-MM-DD>/ :
  * pypi_package_names.csv  — normalize_python(project_name), unique, sorted, one/line
  * npm_package_names.csv   — verbatim _all_docs ids (excl. _design/), unique, sorted
  * SNAPSHOT.md             — date, source URLs, row counts (new + old), sha256, provenance

Sources (per task):
  * PyPI : https://pypi.org/simple/   (Accept: application/vnd.pypi.simple.v1+json)
  * npm  : https://replicate.npmjs.com/_all_docs   (registry replica, paginated)

Polite client: custom UA, HTTP/2, retry+backoff on 429/5xx, small inter-page sleep,
one network round-trip per page. npm is resumable via a .state sidecar.

Old lists are NOT touched — this only ADDS a dated snapshot dir for as-was/as-is diff.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from slop_bench.heuristics import normalize_python  # noqa: E402
from slop_bench.extractors.python_extract import _stdlib_names  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
ML = REPO / "data" / "master_lists"

CONTACT = os.getenv("SLOP_BENCH_CONTACT_EMAIL", "churik0509@gmail.com")
REPO_URL = os.getenv("SLOP_BENCH_REPO_URL", "https://github.com/local/slop-bench")
UA = f"slop-bench-revalidation/0.2 (+{REPO_URL}; {CONTACT})"

PYPI_SIMPLE = "https://pypi.org/simple/"
NPM_ALLDOCS = "https://replicate.npmjs.com/_all_docs"
NPM_PAGE = 10_000  # replica rejects limit>10000 with HTTP 400 (CouchDB view cap)
RETRY_STATUS = {429, 500, 502, 503, 504}


def _client() -> httpx.Client:
    # HTTP/1.1 on purpose: httpx's HTTP/2 flow-control stalls on the large
    # (42 MB PyPI / ~380 MB npm) responses; plain 1.1 streams these fine.
    return httpx.Client(
        http2=False,
        headers={"User-Agent": UA},
        timeout=httpx.Timeout(30.0, read=300.0),
        follow_redirects=True,
    )


def _get(client: httpx.Client, url: str, **kw) -> httpx.Response:
    """GET with retry + exponential backoff on transport errors and 429/5xx."""
    delay = 2.0
    last_exc = None
    for attempt in range(7):
        try:
            r = client.get(url, **kw)
            if r.status_code in RETRY_STATUS:
                wait = float(r.headers.get("retry-after", delay))
                print(f"  {r.status_code} on {url} — retry in {wait:.0f}s "
                      f"(attempt {attempt + 1})", flush=True)
                time.sleep(wait)
                delay = min(delay * 2, 60)
                continue
            r.raise_for_status()
            return r
        except httpx.HTTPError as e:  # transport / read / status errors
            last_exc = e
            if attempt == 6:
                break
            print(f"  {type(e).__name__} on {url} — retry in {delay:.0f}s", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 60)
    raise RuntimeError(f"GET failed after retries: {url}") from last_exc


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sort_unique(src: Path, dst: Path) -> int:
    """Byte-deterministic dedup+sort (LC_ALL=C) — memory-safe for millions of lines."""
    env = {**os.environ, "LC_ALL": "C"}
    with dst.open("w") as out:
        subprocess.run(["sort", "-u", str(src)], check=True, stdout=out, env=env)
    with dst.open("rb") as f:
        return sum(1 for _ in f)


# ── PyPI ──────────────────────────────────────────────────────────────────────
def fetch_pypi(client: httpx.Client, out_dir: Path) -> dict:
    print("PyPI: fetching simple index (v1 JSON, ~42 MB)…", flush=True)
    r = _get(client, PYPI_SIMPLE,
             headers={"Accept": "application/vnd.pypi.simple.v1+json"})
    serial = r.headers.get("x-pypi-last-serial", "?")
    data = r.json()
    projects = data.get("projects", [])
    print(f"PyPI: {len(projects)} raw projects; normalising…", flush=True)
    names = set()
    for p in projects:
        n = normalize_python(p["name"])
        if n:
            names.add(n)
    n_registry = len(names)
    # Augment with the Python 3.11 stdlib module set — mirrors Spracklen's own
    # pypi_package_names.csv (which shipped ~192 stdlib names) AND the extractor's
    # _stdlib_names(): stdlib modules are never PyPI packages, so without this a
    # bare `pip install tkinter` (H1) would be miscounted as a hallucination.
    stdlib = {normalize_python(m) for m in _stdlib_names()}
    stdlib.discard("")
    names |= stdlib
    n_stdlib_added = len(names) - n_registry
    final = out_dir / "pypi_package_names.csv"
    with final.open("w", encoding="utf-8") as f:
        for n in sorted(names):
            f.write(n + "\n")
    print(f"PyPI: wrote {len(names)} unique names "
          f"({n_registry} registry + {n_stdlib_added} new stdlib) → {final}", flush=True)
    return {"count": len(names), "raw_projects": len(projects),
            "n_stdlib_added": n_stdlib_added,
            "serial": serial, "sha256": _sha256(final), "path": final}


# ── npm ───────────────────────────────────────────────────────────────────────
def fetch_npm(client: httpx.Client, out_dir: Path) -> dict:
    raw = out_dir / "npm_package_names.raw"
    state_path = out_dir / ".npm_state.json"
    last = None
    total = None
    if state_path.exists() and raw.exists():
        st = json.loads(state_path.read_text())
        last = st.get("last")
        print(f"npm: resuming after {last!r} (already fetched ~{st.get('rows')})",
              flush=True)
    mode = "a" if last else "w"
    f = raw.open(mode, encoding="utf-8")
    rows_written = 0
    try:
        while True:
            # NOTE: the replica rejects `skip` (HTTP 400), so we page with an
            # INCLUSIVE `startkey` only. The boundary row (== previous page's last
            # id) is re-returned as row 0 of each page; the final `sort -u` dedups
            # it. Forward progress is guaranteed because startkey advances by page.
            params = {"limit": NPM_PAGE}
            if last is not None:
                params["startkey"] = json.dumps(last)
            r = _get(client, NPM_ALLDOCS, params=params)
            j = r.json()
            total = j.get("total_rows")
            rows = j.get("rows", [])
            if not rows:
                break
            page_last = None
            for row in rows:
                _id = row["id"]
                page_last = _id
                if _id.startswith("_design/"):
                    continue
                f.write(_id + "\n")
                rows_written += 1
            f.flush()
            last = page_last
            state_path.write_text(json.dumps({"last": last, "rows": rows_written}))
            print(f"npm: ~{rows_written} written / {total} total (last={last!r})",
                  flush=True)
            if len(rows) < NPM_PAGE:
                break
            time.sleep(0.3)
    finally:
        f.close()

    final = out_dir / "npm_package_names.csv"
    count = _sort_unique(raw, final)
    raw.unlink(missing_ok=True)
    state_path.unlink(missing_ok=True)
    print(f"npm: wrote {count} unique names → {final}", flush=True)
    return {"count": count, "total_rows": total,
            "sha256": _sha256(final), "path": final}


def _old_count(name: str) -> str:
    p = ML / name
    if not p.exists():
        return "n/a"
    with p.open("rb") as f:
        return f"{sum(1 for _ in f)}"


def _meta_from_disk(out_dir: Path, filename: str) -> dict | None:
    """Rebuild a list's snapshot metadata from the CSV already on disk.

    Used so that running the fetcher for only ONE registry still produces a
    complete SNAPSHOT.md (the other registry's row is filled from its file)."""
    p = out_dir / filename
    if not p.exists():
        return None
    with p.open("rb") as f:
        count = sum(1 for _ in f)
    return {"count": count, "sha256": _sha256(p), "path": p, "from_disk": True}


def write_snapshot(out_dir: Path, pypi: dict | None, npm: dict | None) -> None:
    d = out_dir.name
    lines = [
        f"# Master-list snapshot — {d}",
        "",
        "Fresh PyPI + npm package-name lists fetched for the revalidation of",
        "slopsquatting-replication-2026. Old flat lists in `data/master_lists/` are",
        "kept untouched for as-was/as-is comparison.",
        "",
        "| List | Source | Fetched (this snapshot) | Old (Spracklen, 2026-04-22) |",
        "|---|---|---:|---:|",
    ]
    if pypi:
        lines.append(
            f"| PyPI | `{PYPI_SIMPLE}` (v1 JSON) | {pypi['count']} | {_old_count('pypi_package_names.csv')} |"
        )
    if npm:
        lines.append(
            f"| npm | `{NPM_ALLDOCS}` (replica) | {npm['count']} | {_old_count('npm_package_names.csv')} |"
        )
    lines += ["", "## Provenance", ""]
    if pypi:
        rp = pypi.get("raw_projects", "n/a (recomputed from disk)")
        sr = pypi.get("serial", "n/a (recomputed from disk)")
        std = pypi.get("n_stdlib_added")
        lines += [
            f"- **PyPI** raw projects returned: {rp}; "
            f"stored as `normalize_python()`-normalised unique names, "
            f"augmented with the Python-3.11 stdlib module set"
            + (f" (+{std} names not already present)" if std is not None else "")
            + " to match Spracklen's list composition.",
            f"  - `X-PyPI-Last-Serial`: `{sr}`",
            f"  - `pypi_package_names.csv` sha256: `{pypi['sha256']}`",
        ]
    if npm:
        tr = npm.get("total_rows", "n/a (recomputed from disk)")
        lines += [
            f"- **npm** `_all_docs` total_rows at fetch: {tr}; "
            f"stored verbatim ids (excl. `_design/`), unique+sorted (LC_ALL=C).",
            f"  - `npm_package_names.csv` sha256: `{npm['sha256']}`",
        ]
    lines += [
        "",
        f"User-Agent: `{UA}`",
        "",
        "Normalisation notes:",
        "- PyPI names normalised with `slop_bench.heuristics.normalize_python` so the",
        "  stored set equals the membership set the validator tests against.",
        "- npm names stored verbatim (validator loads them raw; `normalize_javascript`",
        "  only strips backticks).",
        "",
    ]
    (out_dir / "SNAPSHOT.md").write_text("\n".join(lines))
    print(f"Wrote {out_dir / 'SNAPSHOT.md'}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--skip-pypi", action="store_true")
    ap.add_argument("--skip-npm", action="store_true")
    args = ap.parse_args()

    out_dir = ML / args.date
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output dir: {out_dir}", flush=True)

    pypi = npm = None
    with _client() as client:
        if not args.skip_pypi:
            pypi = fetch_pypi(client, out_dir)
        if not args.skip_npm:
            npm = fetch_npm(client, out_dir)
    # Fill any skipped list from its on-disk CSV so SNAPSHOT.md is always complete.
    if pypi is None:
        pypi = _meta_from_disk(out_dir, "pypi_package_names.csv")
    if npm is None:
        npm = _meta_from_disk(out_dir, "npm_package_names.csv")
    write_snapshot(out_dir, pypi, npm)
    print("Done (item 1).", flush=True)


if __name__ == "__main__":
    main()
