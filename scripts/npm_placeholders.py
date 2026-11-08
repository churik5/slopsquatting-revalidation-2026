"""Descriptive check of npm SECURITY_HOLDING / UNPUBLISHED names (task 1 follow-up; no A/B split).

    ~/.local/share/slop-bench/venv-check/bin/python scripts/npm_placeholders.py

1. Creation time vs generation time: from data/private/classification.db (time.created of the
   npm document; the name's earliest generation, as in task1_analysis.py).
2. SECURITY_HOLDING placeholders: type of the description / README text. The round 1-2 rows of
   registry_checks keep no description or README, so each such name gets ONE extra request
   GET https://registry.npmjs.org/<name> (<= 1 request/s, same client settings as
   scripts/registry_check.py), stored append-only in registry_checks with round = 'meta'.
   A name that already has a final 'meta' row is not requested again.

Writes reports/npm_placeholders.md (counts only, no names).
"""
from __future__ import annotations

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

CLS_DB = REPO / "data" / "private" / "classification.db"
OUT = REPO / "reports" / "npm_placeholders.md"
ROUND = "meta"
TYPES = ["npm_standard_malicious_code_text", "npm_standard_description_only", "other_text_publisher_placeholder",
         "no_text", "request_failed_no_response"]


def text_type(m: dict) -> str:
    """Type of the placeholder text, from the stored 'meta' row (description + README)."""
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


def fetch_texts(names: list[str]) -> dict[str, dict]:
    store = RC.Store(RC.OUT_DB)
    done = {}
    for n, mj in store.conn.execute("SELECT name, meta_json FROM registry_checks WHERE round = ? AND "
                                    "ecosystem = 'npm' AND method = 'npm_registry' AND is_final = 1", (ROUND,)):
        done[n] = json.loads(mj or "{}")
    todo = [n for n in names if n not in done]
    store.event(ROUND, "start", {"todo": len(todo), "already_final": len(names) - len(todo)})
    with httpx.Client(headers={"User-Agent": RC.USER_AGENT}, timeout=RC.TIMEOUT, follow_redirects=True) as client:
        host = RC.Host(client)
        for n in todo:
            verb, url, headers, content = RC.build_request("npm_registry", n, "npm")
            row = {"round": ROUND, "name": n, "ecosystem": "npm", "method": "npm_registry", "url": url,
                   "attempt": 1, "checked_at_utc": RC.now_utc(), "is_final": 1}
            try:
                resp = host.send(verb, url, headers, content)
                raw = resp.content
                row.update(final_url=str(resp.url), http_status=resp.status_code,
                           body_sha256=hashlib.sha256(raw).hexdigest(), checked_at_utc=RC.now_utc())
                state, meta = RC.classify("npm_registry", resp.status_code, raw)
                if state == "EXISTS":
                    d = json.loads(raw)
                    v = (d.get("versions") or {}).get("0.0.1-security") or {}
                    desc = d.get("description") or v.get("description") or ""
                    readme = d.get("readme") or v.get("readme") or ""
                    meta.update(description=desc, readme=readme[:2000],
                                readme_sha256=hashlib.sha256(readme.encode()).hexdigest())
                row.update(state=state, meta_json=json.dumps(meta, sort_keys=True))
            except (httpx.TimeoutException, httpx.TransportError) as e:
                row.update(state="UNKNOWN", error=f"{type(e).__name__}")
            store.add_check(row)
            done[n] = json.loads(row.get("meta_json") or "{}")
    store.event(ROUND, "end", {})
    return done


def main() -> None:
    c = sqlite3.connect(f"file:{CLS_DB}?mode=ro", uri=True)
    rows = c.execute("SELECT name, category, asof_status, created_utc FROM names WHERE ecosystem = 'npm' "
                     "AND category IN ('SECURITY_HOLDING', 'UNPUBLISHED') AND exclusion IS NULL "
                     "ORDER BY name").fetchall()
    c.close()
    timing = {cat: Counter() for cat in ("SECURITY_HOLDING", "UNPUBLISHED")}
    for _n, cat, asof, created in rows:
        timing[cat]["before" if asof == "FALSE_POSITIVE" else "after" if asof == "H_REGISTERED_LATER"
                    else "unknown"] += 1
    holding = [n for n, cat, *_ in rows if cat == "SECURITY_HOLDING"]
    meta = fetch_texts(holding)
    rc = sqlite3.connect(f"file:{RC.OUT_DB}?mode=ro", uri=True)
    mal = {n for (n,) in rc.execute("SELECT name FROM registry_checks WHERE round = 1 AND ecosystem = 'npm' "
                                    "AND method = 'osv' AND is_final = 1 AND state = 'MAL'")}
    types = Counter((text_type(meta[n]), n in mal) for n in holding)
    errs = rc.execute("SELECT GROUP_CONCAT(DISTINCT error) FROM registry_checks WHERE round = ? AND error IS NOT NULL",
                      (ROUND,)).fetchone()[0]
    req = rc.execute("SELECT COUNT(*), GROUP_CONCAT(DISTINCT http_status) FROM registry_checks WHERE round = ?",
                     (ROUND,)).fetchone()
    rc.close()

    L = ["# npm placeholders: SECURITY_HOLDING and UNPUBLISHED (descriptive)", "",
         "Generated by `scripts/npm_placeholders.py`. Retained npm study names (groups A and B together, no split); "
         "counts only.", "",
         "## Creation time (npm `time.created`) vs generation time", "",
         "Generation time = the name's earliest generation (sync: own timestamp; batch: D2 interval), "
         "as in `scripts/task1_analysis.py`.", "",
         "| Status | Names | Created before generation | Created after generation | Unknown |",
         "|---|---:|---:|---:|---:|"]
    for cat, t in timing.items():
        L.append(f"| {cat} | {sum(t.values())} | {t['before']} | {t['after']} | {t['unknown']} |")
    L += ["", "## SECURITY_HOLDING: type of the placeholder text (description + README)", "",
          "| Text type | Names | …with an OSV MAL record | …without |", "|---|---:|---:|---:|"]
    for k in TYPES:
        a, b = types[(k, True)], types[(k, False)]
        L.append(f"| {k} | {a + b} | {a} | {b} |")
    L += ["", "- `npm_standard_malicious_code_text`: npm's standard README (the package contained malicious code and "
          "was removed by the npm security team).",
          "- `npm_standard_description_only`: npm's standard description `security holding package` with an empty "
          "README; the text itself does not give the reason, the OSV column does.",
          "- `other_text_publisher_placeholder`: version `0.0.1-security` but the publisher's own text (a name "
          "reservation), not npm's.",
          "- `request_failed_no_response`: the single allowed request got no response; not retried.", "",
          "Rounds 1–2 stored no description or README, so the text comes from one extra request per name, recorded "
          f"in `registry_checks` as round `meta` ({req[0]} rows; HTTP statuses: {req[1]}; errors: {errs or 'none'}). "
          "OSV = round 1 query.", ""]
    OUT.write_text("\n".join(L))
    print(f"wrote {OUT.relative_to(REPO)}")


if __name__ == "__main__":
    main()
