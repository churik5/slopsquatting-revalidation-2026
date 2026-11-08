"""Descriptive check of npm SECURITY_HOLDING / UNPUBLISHED names (task 1 follow-up; no A/B split).

    .venv/bin/python scripts/npm_placeholders.py                  # render; requests only what is missing
    .venv/bin/python scripts/npm_placeholders.py --retry-failed   # one more request for a failed npm request

1. Creation time vs generation time: data/private/classification.db (npm `time.created`; the
   name's earliest generation, as in task1_analysis.py).
2. SECURITY_HOLDING placeholder text (description + README). Rounds 1-2 kept no text, so each such
   name gets ONE request GET https://registry.npmjs.org/<name>.
3. For SECURITY_HOLDING names created after generation: publication date of the OSV MAL record
   vs generation time and T0. Round 1 kept only OSV ids, so each such name gets ONE request
   POST https://api.osv.dev/v1/query (full records include `published`).
All extra requests: <= 1 request/s per host, client settings of scripts/registry_check.py, stored
append-only in registry_checks with round = 'meta'. A name with a 'meta' row that has a response is
never requested again; --retry-failed re-requests (once per run) names whose latest row has none.

Writes reports/npm_placeholders.md (counts only, no names) and records the post-stage-A state of
registry_checks.db in reports/numbers.json -> provenance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import registry_check as RC  # noqa: E402
import task1_analysis as T  # noqa: E402

CLS_DB = REPO / "data" / "private" / "classification.db"
RUNS_DB = REPO / "data" / "runs.db"
OUT = REPO / "reports" / "npm_placeholders.md"
NUMBERS = REPO / "reports" / "numbers.json"
ROUND = "meta"
TYPES = ["npm_standard_malicious_code_text", "npm_standard_description_only", "other_text_publisher_placeholder",
         "no_text", "request_failed_no_response"]


def text_type(m: dict) -> str:
    """Type of the placeholder text, from the latest stored 'meta' row (description + README)."""
    if "description" not in m and "readme" not in m:
        return "request_failed_no_response"
    desc, readme = (m.get("description") or "").strip(), (m.get("readme") or "").strip()
    t = f"{desc}\n{readme}".lower()
    if not t.strip():
        return "no_text"
    if "malicious code" in t and "removed" in t:
        return "npm_standard_malicious_code_text"       # npm security team's standard README
    if desc.lower() == "security holding package" and not readme:
        return "npm_standard_description_only"          # npm's standard description, no README
    return "other_text_publisher_placeholder"


def latest_meta(conn, method: str) -> dict[str, tuple]:
    """name -> (http_status, meta) of the latest 'meta' row for this method."""
    out = {}
    for n, st, mj in conn.execute("SELECT name, http_status, meta_json FROM registry_checks WHERE round = ? AND "
                                  "ecosystem = 'npm' AND method = ? AND is_final = 1 ORDER BY id", (ROUND, method)):
        out[n] = (st, json.loads(mj or "{}"))
    return out


def request(host, store, method: str, n: str) -> None:
    verb, url, headers, content = RC.build_request(method, n, "npm")
    row = {"round": ROUND, "name": n, "ecosystem": "npm", "method": method, "url": url,
           "attempt": 1, "checked_at_utc": RC.now_utc(), "is_final": 1}
    try:
        resp = host.send(verb, url, headers, content)
        raw = resp.content
        row.update(final_url=str(resp.url), http_status=resp.status_code,
                   body_sha256=hashlib.sha256(raw).hexdigest(), checked_at_utc=RC.now_utc())
        state, meta = RC.classify(method, resp.status_code, raw)
        if method == "npm_registry" and state == "EXISTS":
            d = json.loads(raw)
            v = (d.get("versions") or {}).get("0.0.1-security") or {}
            desc = d.get("description") or v.get("description") or ""
            readme = d.get("readme") or v.get("readme") or ""
            meta.update(description=desc, readme=readme[:2000],
                        readme_sha256=hashlib.sha256(readme.encode()).hexdigest())
        if method == "osv" and state in ("MAL", "NO_MAL"):
            vulns = [v for v in json.loads(raw).get("vulns", []) if isinstance(v, dict)]
            meta["mal_records"] = [{"id": v.get("id"), "published": v.get("published"), "modified": v.get("modified")}
                                   for v in vulns if str(v.get("id", "")).startswith("MAL-")]
        row.update(state=state, meta_json=json.dumps(meta, sort_keys=True))
    except (httpx.TimeoutException, httpx.TransportError) as e:
        row.update(state="UNKNOWN", error=type(e).__name__)
    store.add_check(row)


def fetch(holding: list[str], after: list[str], retry_failed: bool) -> None:
    store = RC.Store(RC.OUT_DB)
    npm_meta, osv_meta = latest_meta(store.conn, "npm_registry"), latest_meta(store.conn, "osv")
    todo_npm = [n for n in holding if n not in npm_meta or (retry_failed and npm_meta[n][0] is None)]
    todo_osv = [n for n in after if n not in osv_meta or (retry_failed and osv_meta[n][0] is None)]
    if not todo_npm and not todo_osv:
        return
    store.event(ROUND, "start", {"npm_registry": len(todo_npm), "osv": len(todo_osv), "retry_failed": retry_failed})
    with httpx.Client(headers={"User-Agent": RC.USER_AGENT}, timeout=RC.TIMEOUT, follow_redirects=True) as client:
        hosts = {"npm_registry": RC.Host(client), "osv": RC.Host(client)}
        for method, todo in (("npm_registry", todo_npm), ("osv", todo_osv)):
            for n in todo:
                request(hosts[method], store, method, n)
    store.event(ROUND, "end", {})
    store.conn.close()                     # last connection: WAL is checkpointed into the main file


def earliest_generation(names: set[str]) -> dict[str, tuple]:
    """name -> (lo, hi) of its earliest generation (smallest upper bound), as in task1_analysis.py."""
    c = sqlite3.connect(f"file:{RUNS_DB}?mode=ro", uri=True)
    gens = T.load_generations(c.cursor())
    c.close()
    k = sqlite3.connect(f"file:{CLS_DB}?mode=ro", uri=True)
    out: dict = {}
    for n, gid in k.execute("SELECT name_c, generation_id FROM mentions WHERE ecosystem = 'npm'"):
        if n in names:
            lo, hi = gens[gid][3], gens[gid][4]
            if hi is not None and (n not in out or (hi, lo) < (out[n][1], out[n][0])):
                out[n] = (lo, hi)
    k.close()
    return out


def rel(t, lo, hi) -> str:
    if t is None:
        return "no_date"
    return "before" if t < lo else "after" if t > hi else "inside_batch_interval"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--retry-failed", action="store_true")
    args = ap.parse_args()
    c = sqlite3.connect(f"file:{CLS_DB}?mode=ro", uri=True)
    rows = c.execute("SELECT name, category, asof_status FROM names WHERE ecosystem = 'npm' "
                     "AND category IN ('SECURITY_HOLDING', 'UNPUBLISHED') AND exclusion IS NULL ORDER BY name").fetchall()
    c.close()
    when = {n: ("before" if a == "FALSE_POSITIVE" else "after" if a == "H_REGISTERED_LATER" else "unknown")
            for n, _c, a in rows}
    cat = {n: c_ for n, c_, _a in rows}
    holding = [n for n in cat if cat[n] == "SECURITY_HOLDING"]
    after = [n for n in holding if when[n] == "after"]
    fetch(holding, after, args.retry_failed)

    rc = sqlite3.connect(f"file:{RC.OUT_DB}?mode=ro", uri=True)
    npm_meta, osv_meta = latest_meta(rc, "npm_registry"), latest_meta(rc, "osv")
    mal = {n for (n,) in rc.execute("SELECT name FROM registry_checks WHERE round = 1 AND ecosystem = 'npm' "
                                    "AND method = 'osv' AND is_final = 1 AND state = 'MAL'")}
    meta_rows = rc.execute("SELECT method, COUNT(*), GROUP_CONCAT(DISTINCT http_status), "
                           "GROUP_CONCAT(DISTINCT error) FROM registry_checks WHERE round = ? GROUP BY method",
                           (ROUND,)).fetchall()
    n_meta = sum(r[1] for r in meta_rows)
    rounds = dict(rc.execute("SELECT CAST(round AS TEXT), COUNT(*) FROM registry_checks GROUP BY round").fetchall())
    rc.close()

    timing = {k: Counter() for k in ("SECURITY_HOLDING", "UNPUBLISHED")}
    for n in cat:
        timing[cat[n]][when[n]] += 1
    cross = Counter((cat[n], when[n], text_type(npm_meta.get(n, (None, {}))[1]) if cat[n] == "SECURITY_HOLDING"
                     else "not_requested (UNPUBLISHED)", n in mal) for n in cat)

    t0 = T.ts(json.loads((REPO / "reports" / "t0.json").read_text())["t0_utc"])
    unpub = [n for n in cat if cat[n] == "UNPUBLISHED"]
    gen = earliest_generation(set(after) | set(unpub))
    # time.unpublished.time from the npm document of the latest round (1-2) that returned it
    rc = sqlite3.connect(f"file:{RC.OUT_DB}?mode=ro", uri=True)
    unpub_time = {}
    for n, mj in rc.execute("SELECT name, meta_json FROM registry_checks WHERE round IN (1, 2) AND "
                            "ecosystem = 'npm' AND method = 'npm_registry' AND is_final = 1 AND state = 'EXISTS' "
                            "ORDER BY id"):
        tu = json.loads(mj or "{}").get("time_unpublished")
        if n in cat and tu:
            unpub_time[n] = T.ts(tu.get("time")) if isinstance(tu, dict) else None
    rc.close()
    unpub_rel = Counter(rel(unpub_time.get(n), *gen[n]) if n in gen else "no_generation_time" for n in unpub)
    osv_rel = Counter()
    for n in after:
        st, m = osv_meta.get(n, (None, {}))
        recs = m.get("mal_records") or []
        pub = min((T.ts(r["published"]) for r in recs if r.get("published")), default=None)
        if st is None:
            osv_rel[("request_failed", "", "")] += 1
            continue
        lo, hi = gen[n]
        osv_rel[(f"{len(recs)} MAL record(s)", rel(pub, lo, hi),
                 "no_date" if pub is None else ("before" if pub < t0 else "after"))] += 1

    L = ["# npm placeholders: SECURITY_HOLDING and UNPUBLISHED (descriptive)", "",
         "Generated by `scripts/npm_placeholders.py`. Retained npm study names (groups A and B together, no split); "
         "counts only.", "",
         "## 1. Creation time (npm `time.created`) vs generation time", "",
         "Generation time = the name's earliest generation (sync: own timestamp; batch: D2 interval), "
         "as in `scripts/task1_analysis.py`.", "",
         "| Status | Names | Created before generation | Created after generation | Unknown |",
         "|---|---:|---:|---:|---:|"]
    for k, t in timing.items():
        L.append(f"| {k} | {sum(t.values())} | {t['before']} | {t['after']} | {t['unknown']} |")
    L += ["", "## 2. SECURITY_HOLDING: type of the placeholder text (description + README)", "",
          "| Text type | Names | …with an OSV MAL record | …without |", "|---|---:|---:|---:|"]
    for k in TYPES:
        a = sum(v for (c_, _w, tt, m), v in cross.items() if c_ == "SECURITY_HOLDING" and tt == k and m)
        b = sum(v for (c_, _w, tt, m), v in cross.items() if c_ == "SECURITY_HOLDING" and tt == k and not m)
        L.append(f"| {k} | {a + b} | {a} | {b} |")
    L += ["", "## 3. Creation time × text type × OSV MAL record", "",
          "| Status | Created | Text type | OSV MAL record | Names |", "|---|---|---|---|---:|"]
    for (c_, w, tt, m), v in sorted(cross.items()):
        L.append(f"| {c_} | {w} generation | {tt} | {'yes' if m else 'no'} | {v} |")
    L += ["", "## 4. SECURITY_HOLDING created after generation: OSV MAL record publication date", "",
          "Earliest `published` among the name's MAL records (OSV query in round `meta`).", "",
          "| MAL records | Published vs generation time | Published vs T0 | Names |", "|---|---|---|---:|"]
    for (k, g, t), v in sorted(osv_rel.items()):
        L.append(f"| {k} | {g} | {t} | {v} |")
    L += ["", f"T0 = {T.iso(t0)[:19]}Z (`reports/t0.json`).", "",
          "## 5. UNPUBLISHED: unpublication time (`time.unpublished.time`) vs generation time", "",
          "From the npm documents of rounds 1–2 (no new requests). See PREREG-deviations D17.", "",
          "| Unpublished | Names |", "|---|---:|"]
    for k in ("before", "after", "inside_batch_interval", "no_date", "no_generation_time"):
        if unpub_rel[k] or k in ("before", "after"):
            L.append(f"| {k} generation |" if k in ("before", "after") else f"| {k} |")
            L[-1] += f" {unpub_rel[k]} |"
    L += [f"| **total** | {len(unpub)} |", "",
          "## Notes", "",
          "- `npm_standard_malicious_code_text`: npm's standard README (the package contained malicious code and "
          "was removed by the npm security team).",
          "- `npm_standard_description_only`: npm's standard description `security holding package` with an empty "
          "README; the text itself does not give the reason, the OSV column does.",
          "- `other_text_publisher_placeholder`: version `0.0.1-security` but the publisher's own text (a name "
          "reservation), not npm's.",
          "- `request_failed_no_response`: no response to the allowed request(s).",
          "- Extra requests are stored in `registry_checks` as round `meta`: " + "; ".join(
              f"{m} {k} rows (HTTP {s or '—'}; errors: {e or 'none'})" for m, k, s, e in meta_rows)
          + ". The latest row per name is used.", ""]
    OUT.write_text("\n".join(L))

    wal = RC.OUT_DB.with_name(RC.OUT_DB.name + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise SystemExit("registry_checks.db has a non-empty WAL; its file hash would not be final")
    nums = json.loads(NUMBERS.read_text())
    nums["provenance"]["registry_checks_after_stage_A"] = {
        "note": "after stage A (task1_analysis.py), rows of round 'meta' were appended to registry_checks by "
                "scripts/npm_placeholders.py; rounds 1 and 2 are unchanged and the task 1 classification reads "
                "only rounds 1 and 2. registry_checks_db_sha256 above is the file at stage A.",
        "meta_rows": n_meta, "meta_rows_by_method": {m: k for m, k, _s, _e in meta_rows},
        "rows_by_round": rounds,
        "registry_checks_db_sha256_now": T.sha256_file(RC.OUT_DB)}
    NUMBERS.write_text(json.dumps(nums, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    print(f"wrote {OUT.relative_to(REPO)} and updated reports/numbers.json provenance")


if __name__ == "__main__":
    main()
