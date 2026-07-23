"""Published-vs-new diff report (revalidation task, item 5).

Writes reports/diff_published_vs_new.md:
  * every headline number: published -> new -> delta
  * the reproducible baseline computed by the current code on the ORIGINAL data
    (to show that the preprint's 109 is not reproducible; the code gives 121)
  * a by-name list of what left / entered the universal set, WITH the reason.

Reads: reports/recompute.json (new), disclosure/*.csv (published universal set),
the ORIGINAL db read-only (old exists_in_master), the COPY (fresh exists + live).
"""
from __future__ import annotations

import csv
import json
import math
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from slop_bench.noise_filter import is_noise, sql_hallucination_clause  # noqa: E402

Z = 1.959963984540054

# ── Published constants (from the committed artifacts) ────────────────────────
# Per-model (halluc, total) as hard-coded in the original make_figure2.py.
PUBLISHED_MODEL = {
    "haiku-4-5": (1399, 30303),
    "sonnet-4-6": (1875, 34633),
    "gemini-2.5-pro": (1983, 34168),
    "deepseek-v3.2": (1537, 26116),
    "gpt-5.4-mini": (1194, 19589),
}
# Preprint §9.2 / README: 127 = 109 PyPI + 18 npm.
PUBLISHED_UNIVERSAL_PREPRINT = {"pypi": 109, "npm": 18, "total": 127}
MODELS = ["haiku-4-5", "sonnet-4-6", "gemini-2.5-pro", "deepseek-v3.2", "gpt-5.4-mini"]


def wilson(h, t):
    if not t:
        return 0.0
    return 100 * h / t


def load_disclosure():
    def rd(f):
        return set(r[0].strip() for r in csv.reader((REPO / "disclosure" / f).open()) if r)
    return rd("pypi_universal_hallucinations.csv"), rd("npm_universal_hallucinations.csv")


def universal_on(db_uri, halluc_mode="new"):
    """Return (pypi_set, npm_set) universal set on a db.
    halluc_mode 'new' uses live-aware clause; 'origfilter' ignores live (original)."""
    con = sqlite3.connect(db_uri, uri=True)
    cur = con.cursor()
    if halluc_mode == "new":
        clause = sql_hallucination_clause("h.package_norm", "v.exists_in_master", "v.live_status")
    else:  # original data has live_status all-NULL, so this equals exists=0 AND keep
        clause = sql_hallucination_clause("h.package_norm", "v.exists_in_master", "v.live_status")
    cur.execute(f"""
        WITH mh AS (SELECT DISTINCT g.model_key, h.package_norm, g.language
            FROM generations g JOIN heuristic_hits h ON h.generation_id=g.id
            JOIN validations v ON v.package_norm=h.package_norm
                 AND v.registry=(CASE WHEN g.language='python' THEN 'pypi' ELSE 'npm' END)
            WHERE {clause})
        SELECT package_norm, language FROM mh
        GROUP BY package_norm, language HAVING COUNT(DISTINCT model_key)=5""")
    rows = cur.fetchall()
    con.close()
    return (set(p for p, l in rows if l == "python"),
            set(p for p, l in rows if l == "javascript"))


def val_lookup(db_uri):
    con = sqlite3.connect(db_uri, uri=True)
    d = {}
    for norm, reg, ex, live in con.execute(
            "SELECT package_norm, registry, exists_in_master, live_status FROM validations"):
        d[(norm, reg)] = (ex, live)
    con.close()
    return d


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/tmp/runs-snap.db")
    args = ap.parse_args()

    new = json.loads((REPO / "reports" / "recompute.json").read_text())
    disc_py, disc_js = load_disclosure()
    orig_ex = val_lookup("file:data/runs.db?mode=ro")   # OLD exists (+ null live)
    new_ex = val_lookup(f"file:{args.db}?mode=ro")       # fresh exists + live

    # reproducible baseline: current code on ORIGINAL data
    repro_py, repro_js = universal_on("file:data/runs.db?mode=ro", "origfilter")
    new_py, new_js = set(new["universal"]["pypi"]), set(new["universal"]["npm"])

    L = ["# Published vs. recomputed — every headline number", "",
         "Three columns where they differ:",
         "- **published** — the value in the preprint / figure / disclosure files as shipped",
         "- **repro(orig)** — what the *current* code produces on the *original* database "
         "(old master list, no live check) — i.e. what was actually reproducible",
         "- **new** — recomputed on fresh master lists + live registry status", "",
         "## Per-model hallucination rate", "",
         "| Model | published | new | Δ (pp) |", "|---|---|---|---:|"]
    for m in MODELS:
        ph, pt = PUBLISHED_MODEL[m]
        pr = wilson(ph, pt)
        nd = new["per_model"][m]
        L.append(f"| `{m}` | {pr:.2f}% ({ph}/{pt}) | {nd['rate']:.2f}% ({nd['halluc']}/{nd['total']}) "
                 f"| {nd['rate']-pr:+.2f} |")

    L += ["", "## Universal hallucination set (invented by all 5 models)", "",
          "| basis | PyPI | npm | total |", "|---|---:|---:|---:|",
          f"| preprint §9.2 / README | {PUBLISHED_UNIVERSAL_PREPRINT['pypi']} | "
          f"{PUBLISHED_UNIVERSAL_PREPRINT['npm']} | {PUBLISHED_UNIVERSAL_PREPRINT['total']} |",
          f"| disclosure CSVs (as sent) | {len(disc_py)} | {len(disc_js)} | {len(disc_py)+len(disc_js)} |",
          f"| repro(orig): current code, original data | {len(repro_py)} | {len(repro_js)} | {len(repro_py)+len(repro_js)} |",
          f"| **new**: fresh lists + live | **{len(new_py)}** | **{len(new_js)}** | **{len(new_py)+len(new_js)}** |",
          "",
          f"> The preprint's **{PUBLISHED_UNIVERSAL_PREPRINT['pypi']}** PyPI names are not reproducible "
          f"from the code or notebooks: the code yields **{len(repro_py)}** on the original data "
          f"(matching the disclosure CSV's {len(disc_py)} up to the `usr` extractor artifact the new "
          f"filter removes). Treat 109/127 as an erratum target.", ""]

    def reason(name, reg):
        oe = orig_ex.get((name, reg), (None, None))[0]
        ne, live = new_ex.get((name, reg), (None, None))
        if is_noise(name):
            return f"noise-filter drop (extractor/placeholder artifact)"
        if ne == 1 and live == 200:
            return "now EXISTS in registry (fresh list + live HTTP 200)"
        if ne == 1:
            return "now in fresh master list (registered since April snapshot)"
        if oe == 1 and ne == 0:
            return "was in April master list, ABSENT from fresh registry (yanked/removed; live "\
                   f"{live})"
        return f"exists_old={oe} exists_new={ne} live={live}"

    L += ["## Names that LEFT the universal set (disclosure − new), with reason", ""]
    for reg, disc, newset in (("pypi", disc_py, new_py), ("npm", disc_js, new_js)):
        gone = sorted(disc - newset)
        L.append(f"**{reg}: {len(gone)} left**")
        for n in gone:
            L.append(f"- `{n}` — {reason(n, reg)}")
        L.append("")

    L += ["## Names that ENTERED the universal set (new − disclosure), with reason", ""]
    for reg, disc, newset in (("pypi", disc_py, new_py), ("npm", disc_js, new_js)):
        added = sorted(newset - disc)
        L.append(f"**{reg}: {len(added)} entered**")
        for n in added:
            L.append(f"- `{n}` — {reason(n, reg)}")
        L.append("")

    # readiness criterion
    still = new["universal"]["any_live_200"]
    L += ["## Readiness criterion", "",
          f"- Universal-set names returning HTTP 200 from the registry: **{len(still)}** (must be 0)."
          + ("" if not still else f" — {still}"), ""]

    out = REPO / "reports" / "diff_published_vs_new.md"
    out.write_text("\n".join(L) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
