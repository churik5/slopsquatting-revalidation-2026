"""Recompute EVERY paper number from the rebuilt database, deterministically.

Single command (revalidation task, item 4). Reads the snapshot copy
(/tmp/runs-snap.db) whose `validations` table has fresh exists_in_master + live
status. Uses the ONE noise filter (src/slop_bench/noise_filter.py) and the
live-aware hallucination rule (exists=0 AND live<>200 AND not-noise).

Outputs (deterministic, seed=42):
  * reports/recompute.json  — machine-readable, every number
  * reports/recompute.md    — human-readable tables

Metrics: per-model rates + 95% Wilson CI, pairwise chi2 + Holm, permutation test
(10000, seed 42), Jaccard, per-language & per-dataset breakdown, universal set
(intersection of all 5 models), top-10 names per ecosystem, refusal rates.
`generations`/`heuristic_hits` are read-only here; nothing is written to the DB.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from itertools import combinations
from pathlib import Path

import numpy as np
from scipy.stats import chi2_contingency
from statsmodels.stats.multitest import multipletests

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from slop_bench.noise_filter import sql_hallucination_clause, sql_keep_clause  # noqa: E402

MODELS = ["haiku-4-5", "deepseek-v3.2", "sonnet-4-6", "gpt-5.4-mini", "gemini-2.5-pro"]
DATASETS = ["SO_AT", "SO_LY", "LLM_AT", "LLM_LY"]
SEED = 42
N_PERM = 10_000
Z = 1.959963984540054  # 95%

KEEP = sql_keep_clause("h.package_norm")
HALLUC = sql_hallucination_clause("h.package_norm", "v.exists_in_master", "v.live_status")
JOIN = """
    FROM generations g
    JOIN heuristic_hits h ON h.generation_id = g.id
    JOIN validations v ON v.package_norm = h.package_norm
         AND v.registry = (CASE WHEN g.language='python' THEN 'pypi' ELSE 'npm' END)
"""


def wilson(h: int, t: int) -> tuple[float, float, float]:
    if t == 0:
        return 0.0, 0.0, 0.0
    p = h / t
    d = 1 + Z * Z / t
    c = (p + Z * Z / (2 * t)) / d
    m = Z * math.sqrt((p * (1 - p) + Z * Z / (4 * t)) / t) / d
    return p * 100, max(0.0, (c - m)) * 100, min(1.0, (c + m)) * 100


# ── 1. per-model rates ────────────────────────────────────────────────────────
def per_model(cur) -> dict:
    out = {}
    for m in MODELS:
        cur.execute(
            f"SELECT COUNT(*), SUM(CASE WHEN {HALLUC} THEN 1 ELSE 0 END) "
            f"{JOIN} WHERE g.model_key=? AND ({KEEP})", (m,))
        total, halluc = cur.fetchone()
        total, halluc = int(total or 0), int(halluc or 0)
        rate, lo, hi = wilson(halluc, total)
        out[m] = {"halluc": halluc, "total": total, "rate": rate, "ci95": [lo, hi]}
    return out


# ── 2. pairwise chi2 + Holm ───────────────────────────────────────────────────
def pairwise_chi2(counts: dict) -> dict:
    pairs = list(combinations(MODELS, 2))
    pvals, rows = [], []
    for a, b in pairs:
        ha, ta = counts[a]["halluc"], counts[a]["total"]
        hb, tb = counts[b]["halluc"], counts[b]["total"]
        chi2, p, _, _ = chi2_contingency([[ha, ta - ha], [hb, tb - hb]])
        pvals.append(p)
        rows.append({"a": a, "b": b, "chi2": float(chi2), "p_raw": float(p),
                     "delta_pp": abs(counts[a]["rate"] - counts[b]["rate"])})
    reject, p_adj, _, _ = multipletests(pvals, alpha=0.05, method="holm")
    for r, pa, rej in zip(rows, p_adj, reject):
        r["p_holm"] = float(pa)
        r["significant"] = bool(rej)
    return {"pairs": rows, "n_significant": int(sum(reject)), "n_pairs": len(pairs)}


# ── 3. permutation test ───────────────────────────────────────────────────────
def permutation(cur) -> dict:
    data = {}
    for m in MODELS:
        cur.execute(f"SELECT CASE WHEN {HALLUC} THEN 1 ELSE 0 END {JOIN} "
                    f"WHERE g.model_key=? AND ({KEEP})", (m,))
        data[m] = np.array([r[0] for r in cur.fetchall()], dtype=np.int8)
    rng = np.random.default_rng(SEED)
    rows = []
    for a, b in combinations(MODELS, 2):
        va, vb = data[a], data[b]
        obs = abs(va.mean() - vb.mean())
        combined = np.concatenate([va, vb])
        na = len(va)
        exceed = 0
        for _ in range(N_PERM):
            rng.shuffle(combined)
            if abs(combined[:na].mean() - combined[na:].mean()) >= obs:
                exceed += 1
        rows.append({"a": a, "b": b, "delta_pp": float(100 * obs),
                     "p_perm": exceed / N_PERM})
    return {"seed": SEED, "n_permutations": N_PERM, "pairs": rows,
            "n_significant": sum(1 for r in rows if r["p_perm"] < 0.05),
            "max_p": max(r["p_perm"] for r in rows)}


# ── 4. Jaccard ────────────────────────────────────────────────────────────────
def jaccard(cur) -> dict:
    sets = {}
    for m in MODELS:
        cur.execute(f"SELECT DISTINCT h.package_norm {JOIN} WHERE g.model_key=? AND {HALLUC}", (m,))
        sets[m] = {r[0] for r in cur.fetchall()}
    rows, vals = [], []
    for a, b in combinations(MODELS, 2):
        u = sets[a] | sets[b]
        j = len(sets[a] & sets[b]) / len(u) if u else 0.0
        vals.append(j)
        rows.append({"a": a, "b": b, "jaccard": j,
                     "inter": len(sets[a] & sets[b]), "union": len(u)})
    return {"pairs": rows, "mean": sum(vals) / len(vals),
            "min": min(vals), "max": max(vals),
            "unique_per_model": {m: len(s) for m, s in sets.items()}}


# ── 5. breakdowns ─────────────────────────────────────────────────────────────
def grouped(cur, col: str) -> dict:
    cur.execute(
        f"SELECT {col} AS grp, COUNT(*), SUM(CASE WHEN {HALLUC} THEN 1 ELSE 0 END) "
        f"{JOIN} WHERE ({KEEP}) GROUP BY grp ORDER BY grp")
    out = {}
    for grp, total, halluc in cur.fetchall():
        total, halluc = int(total or 0), int(halluc or 0)
        rate, lo, hi = wilson(halluc, total)
        out[grp] = {"halluc": halluc, "total": total, "rate": rate, "ci95": [lo, hi]}
    return out


def model_by_language(cur) -> dict:
    out = {}
    for m in MODELS:
        out[m] = {}
        for lang in ("python", "javascript"):
            cur.execute(
                f"SELECT COUNT(*), SUM(CASE WHEN {HALLUC} THEN 1 ELSE 0 END) "
                f"{JOIN} WHERE g.model_key=? AND g.language=? AND ({KEEP})", (m, lang))
            total, halluc = cur.fetchone()
            total, halluc = int(total or 0), int(halluc or 0)
            rate, lo, hi = wilson(halluc, total)
            out[m][lang] = {"halluc": halluc, "total": total, "rate": rate, "ci95": [lo, hi]}
    return out


# ── 6. universal set ──────────────────────────────────────────────────────────
def universal(cur) -> dict:
    cur.execute(f"""
        WITH mh AS (
            SELECT DISTINCT g.model_key, h.package_norm, g.language {JOIN} WHERE {HALLUC}
        )
        SELECT package_norm, language FROM mh
        GROUP BY package_norm, language HAVING COUNT(DISTINCT model_key) = 5
        ORDER BY language, package_norm""")
    rows = cur.fetchall()
    py = sorted(p for p, l in rows if l == "python")
    js = sorted(p for p, l in rows if l == "javascript")
    # readiness check: none may have live_status == 200
    still_200 = []
    for name, lang in rows:
        reg = "pypi" if lang == "python" else "npm"
        r = cur.execute("SELECT live_status FROM validations WHERE package_norm=? AND registry=?",
                        (name, reg)).fetchone()
        if r and r[0] == 200:
            still_200.append((reg, name))
    return {"pypi": py, "npm": js, "n_pypi": len(py), "n_npm": len(js),
            "total": len(py) + len(js), "any_live_200": still_200}


# ── 7. top names ──────────────────────────────────────────────────────────────
def top_names(cur, n: int = 10) -> dict:
    out = {}
    for reg, lang in (("pypi", "python"), ("npm", "javascript")):
        cur.execute(
            f"SELECT h.package_norm, COUNT(*) c {JOIN} "
            f"WHERE g.language=? AND {HALLUC} "
            f"GROUP BY h.package_norm ORDER BY c DESC, h.package_norm LIMIT ?", (lang, n))
        out[reg] = [{"name": nm, "count": int(c)} for nm, c in cur.fetchall()]
    return out


# ── 8. refusals ───────────────────────────────────────────────────────────────
def refusals(cur) -> dict:
    out = {}
    for m in MODELS:
        cur.execute(
            "SELECT COUNT(*), SUM(CASE WHEN generated_code IS NULL "
            "OR LOWER(TRIM(generated_code)) IN ('none','') THEN 1 ELSE 0 END) "
            "FROM generations WHERE model_key=?", (m,))
        total, ref = cur.fetchone()
        total, ref = int(total or 0), int(ref or 0)
        out[m] = {"refusals": ref, "generations": total,
                  "rate": 100 * ref / total if total else 0.0}
    return out


# ── live status distribution over the hallucination candidate set ─────────────
def live_summary(cur) -> dict:
    cur.execute("SELECT registry, live_status, COUNT(*) FROM validations GROUP BY registry, live_status")
    dist = {}
    for reg, st, c in cur.fetchall():
        dist.setdefault(reg, {})[str(st)] = int(c)
    return dist


def fmt_pct(d):
    return f"{d['rate']:.2f}% [{d['ci95'][0]:.2f}–{d['ci95'][1]:.2f}]  ({d['halluc']}/{d['total']})"


def write_md(res: dict, path: Path) -> None:
    L = ["# Recomputed numbers — slopsquatting-replication-2026 (v2)", "",
         f"DB: `{res['db']}`  •  master lists: `{res['lists_note']}`  •  seed={SEED}", "",
         "## Per-model hallucination rate (95% Wilson CI)", "",
         "| Model | Rate | Halluc | Mentions |", "|---|---|---:|---:|"]
    for m in MODELS:
        d = res["per_model"][m]
        L.append(f"| `{m}` | {d['rate']:.2f}% [{d['ci95'][0]:.2f}–{d['ci95'][1]:.2f}] | {d['halluc']} | {d['total']} |")
    L += ["", "## Pairwise χ² (Holm-corrected)", "",
          f"Significant pairs: **{res['chi2']['n_significant']}/{res['chi2']['n_pairs']}**", "",
          "| Pair | Δpp | χ² | p_raw | p_holm | sig |", "|---|---:|---:|---:|---:|:--:|"]
    for r in res["chi2"]["pairs"]:
        L.append(f"| {r['a']} vs {r['b']} | {r['delta_pp']:.2f} | {r['chi2']:.2f} | "
                 f"{r['p_raw']:.2e} | {r['p_holm']:.2e} | {'***' if r['significant'] else 'ns'} |")
    L += ["", "## Permutation test (10 000, seed 42)", "",
          f"Pairs with p<0.05: **{res['permutation']['n_significant']}/{len(res['permutation']['pairs'])}**, "
          f"max p = {res['permutation']['max_p']:.4f}", "",
          "## Jaccard of hallucinated sets", "",
          f"mean={res['jaccard']['mean']:.3f}, min={res['jaccard']['min']:.3f}, max={res['jaccard']['max']:.3f}", "",
          "## By language", "", "| Language | Rate |", "|---|---|"]
    for k, d in res["by_language"].items():
        L.append(f"| {k} | {fmt_pct(d)} |")
    L += ["", "## By dataset", "", "| Dataset | Rate |", "|---|---|"]
    for k, d in res["by_dataset"].items():
        L.append(f"| {k} | {fmt_pct(d)} |")
    u = res["universal"]
    L += ["", "## Universal set (invented by all 5 models)", "",
          f"**PyPI: {u['n_pypi']}  •  npm: {u['n_npm']}  •  total: {u['total']}**",
          f"  \nReadiness — universal names still returning HTTP 200: **{len(u['any_live_200'])}** "
          f"(must be 0){'' if not u['any_live_200'] else ' — ' + str(u['any_live_200'])}", ""]
    L += ["## Refusal rates", "", "| Model | Refusals | Gens | Rate |", "|---|---:|---:|---:|"]
    for m in MODELS:
        d = res["refusals"][m]
        L.append(f"| `{m}` | {d['refusals']} | {d['generations']} | {d['rate']:.3f}% |")
    L += ["", "## Top-10 hallucinated names", "", "**PyPI:** " +
          ", ".join(f"{x['name']} ({x['count']})" for x in res["top_names"]["pypi"]),
          "", "**npm:** " +
          ", ".join(f"{x['name']} ({x['count']})" for x in res["top_names"]["npm"]), ""]
    path.write_text("\n".join(L) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/tmp/runs-snap.db")
    ap.add_argument("--out", default=str(REPO / "reports"))
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    cur = conn.cursor()

    counts = per_model(cur)
    res = {
        "db": args.db,
        "lists_note": "data/master_lists/<latest>/ (fresh + stdlib-augmented)",
        "per_model": counts,
        "chi2": pairwise_chi2(counts),
        "permutation": permutation(cur),
        "jaccard": jaccard(cur),
        "by_language": grouped(cur, "g.language"),
        "by_dataset": grouped(cur, "g.dataset"),
        "by_model_language": model_by_language(cur),
        "universal": universal(cur),
        "top_names": top_names(cur),
        "refusals": refusals(cur),
        "live_status_distribution": live_summary(cur),
    }
    conn.close()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "recompute.json").write_text(json.dumps(res, indent=2, sort_keys=True))
    write_md(res, out_dir / "recompute.md")
    print(f"wrote {out_dir/'recompute.json'} and {out_dir/'recompute.md'}")
    print(f"universal: pypi={res['universal']['n_pypi']} npm={res['universal']['n_npm']} "
          f"total={res['universal']['total']}  (200-in-final={len(res['universal']['any_live_200'])})")


if __name__ == "__main__":
    main()
