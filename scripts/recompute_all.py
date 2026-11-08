"""Recompute every paper metric from runs.db, deterministically (seed 42).

    python scripts/recompute_all.py                                   # v3 method (native SQL rule)
    python scripts/recompute_all.py --classification data/private/classification.db --variant c
    python scripts/recompute_all.py ... --level generation            # PREREG §3 unit

Hallucination labels come from ONE place per run:
  * native (no --classification): the v3 rule of src/slop_bench/noise_filter.py
    (exists_in_master = 0 AND live_status <> 200 AND not noise), on the raw key h.package_norm;
  * --classification PATH --variant {a,b,c}: per-mention labels from the table `mentions`
    written by scripts/task1_analysis.py (a = v3 method, b = v3 minus stdlib/built-ins,
    c = live revalidation, PREREG §2 + deviations D1-D15).
Either way the labels go into a TEMP table `labels(hit_id, keep, halluc, unknown, name)` and
every metric below reads only that table, so the variants are computed by identical code.

Units (--level):
  * mention    — one heuristic_hits row (the unit of the published v3 numbers);
  * generation — one generations row; it counts if >= 1 of its mentions is a hallucination
                 (PREREG §3.1). Denominator = all stored generations of the model.

Metrics: per-model rates + 95% Wilson CI, pairwise chi2 + Holm, permutation test
(10 000, seed 42), Jaccard of hallucinated name sets, per-language / per-dataset /
model x language breakdowns, universal set (all 5 models), top-10 names, refusals.

Names never go to the public outputs: reports get counts and SHA-256 of sorted lists;
name lists go only to --private-out (default data/private/recompute/).
runs.db is opened read-only (sqlite URI mode=ro); nothing is written to it.
"""
from __future__ import annotations

import argparse
import hashlib
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
LANGUAGES = ["python", "javascript"]
SEED = 42
N_PERM = 10_000
Z = 1.959963984540054  # 95%
VARIANTS = ("a", "b", "c")
LEVELS = ("mention", "generation")

KEEP_V3 = sql_keep_clause("h.package_norm")
HALLUC_V3 = sql_hallucination_clause("h.package_norm", "v.exists_in_master", "v.live_status")
JOIN = """
    FROM generations g
    JOIN heuristic_hits h ON h.generation_id = g.id
    JOIN validations v ON v.package_norm = h.package_norm
         AND v.registry = (CASE WHEN g.language='python' THEN 'pypi' ELSE 'npm' END)
"""
JOIN_L = JOIN + "    JOIN temp.labels L ON L.hit_id = h.id\n"


def wilson(h: int, t: int) -> tuple[float, float, float]:
    if t == 0:
        return 0.0, 0.0, 0.0
    p = h / t
    d = 1 + Z * Z / t
    c = (p + Z * Z / (2 * t)) / d
    m = Z * math.sqrt((p * (1 - p) + Z * Z / (4 * t)) / t) / d
    return p * 100, max(0.0, (c - m)) * 100, min(1.0, (c + m)) * 100


def list_sha256(names) -> str:
    s = sorted(names)
    return hashlib.sha256(("\n".join(s) + "\n" if s else "").encode()).hexdigest()


# ── labels ────────────────────────────────────────────────────────────────────
def prepare_labels(conn: sqlite3.Connection, classification: str | None = None, variant: str | None = None) -> str:
    """Fill temp.labels; return a short description of the label source."""
    conn.execute("DROP TABLE IF EXISTS temp.labels")
    conn.execute("CREATE TEMP TABLE labels (hit_id INTEGER PRIMARY KEY, keep INTEGER NOT NULL, "
                 "halluc INTEGER NOT NULL, unknown INTEGER NOT NULL, name TEXT NOT NULL)")
    if classification is None:
        conn.execute(f"INSERT INTO temp.labels SELECT h.id, CASE WHEN {KEEP_V3} THEN 1 ELSE 0 END, "
                     f"CASE WHEN {HALLUC_V3} THEN 1 ELSE 0 END, 0, h.package_norm {JOIN}")
        return "native v3 SQL rule (noise_filter.sql_hallucination_clause)"
    if variant not in VARIANTS:
        raise SystemExit(f"--variant must be one of {VARIANTS}")
    attached = {r[1] for r in conn.execute("PRAGMA database_list")}
    if "cls" not in attached:
        conn.execute("ATTACH DATABASE ? AS cls", (f"file:{classification}?mode=ro",))
    conn.execute(f"INSERT INTO temp.labels SELECT hit_id, keep_{variant}, halluc_{variant}, "
                 f"unknown_{variant}, name_{variant} FROM cls.mentions")
    return f"classification {Path(classification).name}, variant {variant}"


# ── rates ─────────────────────────────────────────────────────────────────────
def rate(cur, level: str, cond: str = "1=1", params: tuple = ()) -> dict:
    """Rate over units matching `cond` (a condition on g.*). Also reports units whose only
    reason to count would be an UNKNOWN label (upper bound if all of them were hallucinations)."""
    if level == "mention":
        total, halluc, unk = cur.execute(
            f"SELECT COUNT(*), SUM(L.halluc), SUM(CASE WHEN L.halluc = 0 AND L.unknown = 1 THEN 1 ELSE 0 END) "
            f"{JOIN_L} WHERE L.keep = 1 AND {cond}", params).fetchone()
    else:
        total = cur.execute(f"SELECT COUNT(*) FROM generations g WHERE {cond}", params).fetchone()[0]
        halluc, unk = cur.execute(
            f"""SELECT SUM(hh), SUM(CASE WHEN hh = 0 AND uu = 1 THEN 1 ELSE 0 END) FROM (
                    SELECT g.id, MAX(L.halluc) AS hh, MAX(L.unknown) AS uu {JOIN_L}
                    WHERE L.keep = 1 AND {cond} GROUP BY g.id)""", params).fetchone()
    total, halluc, unk = int(total or 0), int(halluc or 0), int(unk or 0)
    r, lo, hi = wilson(halluc, total)
    return {"halluc": halluc, "total": total, "rate": r, "ci95": [lo, hi],
            "unknown_only": unk, "rate_upper_if_unknown_counted": 100 * (halluc + unk) / total if total else 0.0}


def per_model(cur, level: str) -> dict:
    return {m: rate(cur, level, "g.model_key = ?", (m,)) for m in MODELS}


def grouped(cur, level: str, col: str, values) -> dict:
    return {v: rate(cur, level, f"{col} = ?", (v,)) for v in values}


def model_by_language(cur, level: str) -> dict:
    return {m: {lang: rate(cur, level, "g.model_key = ? AND g.language = ?", (m, lang)) for lang in LANGUAGES}
            for m in MODELS}


def model_by_dataset(cur, level: str) -> dict:
    return {m: {ds: rate(cur, level, "g.model_key = ? AND g.dataset = ?", (m, ds)) for ds in DATASETS}
            for m in MODELS}


# ── pairwise chi2 + Holm ──────────────────────────────────────────────────────
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


# ── permutation test ──────────────────────────────────────────────────────────
def unit_vectors(cur, level: str) -> dict:
    """0/1 vector per model in a fixed order (h.id or g.id)."""
    data = {}
    for m in MODELS:
        if level == "mention":
            rows = cur.execute(f"SELECT L.halluc {JOIN_L} WHERE L.keep = 1 AND g.model_key = ? ORDER BY h.id",
                               (m,)).fetchall()
            data[m] = np.array([r[0] for r in rows], dtype=np.int8)
        else:
            hit = {r[0] for r in cur.execute(
                f"SELECT DISTINCT g.id {JOIN_L} WHERE L.keep = 1 AND L.halluc = 1 AND g.model_key = ?", (m,))}
            ids = [r[0] for r in cur.execute("SELECT id FROM generations WHERE model_key = ? ORDER BY id", (m,))]
            data[m] = np.array([1 if i in hit else 0 for i in ids], dtype=np.int8)
    return data


def permutation(cur, level: str) -> dict:
    data = unit_vectors(cur, level)
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
        rows.append({"a": a, "b": b, "delta_pp": float(100 * obs), "p_perm": exceed / N_PERM})
    return {"seed": SEED, "n_permutations": N_PERM, "pairs": rows,
            "n_significant": sum(1 for r in rows if r["p_perm"] < 0.05),
            "max_p": max(r["p_perm"] for r in rows)}


# ── name sets (Jaccard, universal, top names) ─────────────────────────────────
def model_name_sets(cur) -> dict:
    return {m: {r[0] for r in cur.execute(
        f"SELECT DISTINCT L.name {JOIN_L} WHERE L.halluc = 1 AND g.model_key = ?", (m,))} for m in MODELS}


def jaccard(sets: dict) -> dict:
    rows, vals = [], []
    for a, b in combinations(MODELS, 2):
        u = sets[a] | sets[b]
        j = len(sets[a] & sets[b]) / len(u) if u else 0.0
        vals.append(j)
        rows.append({"a": a, "b": b, "jaccard": j, "inter": len(sets[a] & sets[b]), "union": len(u)})
    return {"pairs": rows, "mean": sum(vals) / len(vals), "min": min(vals), "max": max(vals),
            "unique_per_model": {m: len(s) for m, s in sets.items()}}


def universal(cur) -> dict:
    rows = cur.execute(f"""
        WITH mh AS (SELECT DISTINCT g.model_key, L.name, g.language {JOIN_L} WHERE L.halluc = 1)
        SELECT name, language FROM mh GROUP BY name, language HAVING COUNT(DISTINCT model_key) = 5
        ORDER BY language, name""").fetchall()
    return {"pypi": sorted(p for p, lang in rows if lang == "python"),
            "npm": sorted(p for p, lang in rows if lang == "javascript")}


def top_names(cur, n: int = 10) -> dict:
    out = {}
    for reg, lang in (("pypi", "python"), ("npm", "javascript")):
        out[reg] = [{"name": nm, "count": int(c)} for nm, c in cur.execute(
            f"SELECT L.name, COUNT(*) c {JOIN_L} WHERE g.language = ? AND L.halluc = 1 "
            f"GROUP BY L.name ORDER BY c DESC, L.name LIMIT ?", (lang, n))]
    return out


# ── refusals ──────────────────────────────────────────────────────────────────
def refusals(cur) -> dict:
    out = {}
    for m in MODELS:
        total, ref = cur.execute(
            "SELECT COUNT(*), SUM(CASE WHEN generated_code IS NULL "
            "OR LOWER(TRIM(generated_code)) IN ('none','') THEN 1 ELSE 0 END) "
            "FROM generations WHERE model_key=?", (m,)).fetchone()
        total, ref = int(total or 0), int(ref or 0)
        out[m] = {"refusals": ref, "generations": total, "rate": 100 * ref / total if total else 0.0}
    return out


# ── driver ────────────────────────────────────────────────────────────────────
def compute(conn: sqlite3.Connection, level: str, classification: str | None = None,
            variant: str | None = None, permutations: bool = True) -> dict:
    """All metrics for one (label source, level). The result CONTAINS NAMES under
    'universal' and 'top_names'; use public() before writing anything outside data/private/."""
    if level not in LEVELS:
        raise SystemExit(f"--level must be one of {LEVELS}")
    source = prepare_labels(conn, classification, variant)
    cur = conn.cursor()
    counts = per_model(cur, level)
    sets = model_name_sets(cur)
    res = {
        "labels": source, "level": level,
        "per_model": counts,
        "chi2": pairwise_chi2(counts),
        "permutation": permutation(cur, level) if permutations else None,
        "jaccard": jaccard(sets),
        "by_language": grouped(cur, level, "g.language", LANGUAGES),
        "by_dataset": grouped(cur, level, "g.dataset", DATASETS),
        "by_model_language": model_by_language(cur, level),
        "by_model_dataset": model_by_dataset(cur, level),
        "universal": universal(cur),
        "top_names": top_names(cur),
        "refusals": refusals(cur),
    }
    return res


def public(res: dict) -> dict:
    """Name-free copy: lists become counts + SHA-256 of the sorted list."""
    out = {k: v for k, v in res.items() if k not in ("universal", "top_names")}
    u = res["universal"]
    out["universal"] = {"n_pypi": len(u["pypi"]), "n_npm": len(u["npm"]), "total": len(u["pypi"]) + len(u["npm"]),
                        "sha256_pypi": list_sha256(u["pypi"]), "sha256_npm": list_sha256(u["npm"])}
    out["top_names"] = {reg: {"counts": [x["count"] for x in lst], "sha256": list_sha256(x["name"] for x in lst)}
                        for reg, lst in res["top_names"].items()}
    return out


def write_private(res: dict, private_dir: Path, tag: str) -> None:
    private_dir.mkdir(parents=True, exist_ok=True)
    (private_dir / f"universal_{tag}.json").write_text(json.dumps(res["universal"], ensure_ascii=False) + "\n")
    (private_dir / f"top_{tag}.json").write_text(json.dumps(res["top_names"], ensure_ascii=False) + "\n")


def fmt_pct(d):
    return f"{d['rate']:.2f}% [{d['ci95'][0]:.2f}–{d['ci95'][1]:.2f}]  ({d['halluc']}/{d['total']})"


def write_md(res: dict, path: Path) -> None:
    unit = "Mentions" if res["level"] == "mention" else "Generations"
    L = [f"# Recomputed numbers ({res['labels']}; unit: {res['level']})", "",
         "## Per-model hallucination rate (95% Wilson CI)", "",
         f"| Model | Rate | Halluc | {unit} |", "|---|---|---:|---:|"]
    for m in MODELS:
        d = res["per_model"][m]
        L.append(f"| `{m}` | {d['rate']:.2f}% [{d['ci95'][0]:.2f}–{d['ci95'][1]:.2f}] | {d['halluc']} | {d['total']} |")
    L += ["", "## Pairwise χ² (Holm-corrected)", "",
          f"Significant pairs: **{res['chi2']['n_significant']}/{res['chi2']['n_pairs']}**", "",
          "| Pair | Δpp | χ² | p_raw | p_holm | sig |", "|---|---:|---:|---:|---:|:--:|"]
    for r in res["chi2"]["pairs"]:
        L.append(f"| {r['a']} vs {r['b']} | {r['delta_pp']:.2f} | {r['chi2']:.2f} | "
                 f"{r['p_raw']:.2e} | {r['p_holm']:.2e} | {'***' if r['significant'] else 'ns'} |")
    if res["permutation"]:
        L += ["", "## Permutation test (10 000, seed 42)", "",
              f"Pairs with p<0.05: **{res['permutation']['n_significant']}/{len(res['permutation']['pairs'])}**, "
              f"max p = {res['permutation']['max_p']:.4f}"]
    L += ["", "## Jaccard of hallucinated sets", "",
          f"mean={res['jaccard']['mean']:.3f}, min={res['jaccard']['min']:.3f}, max={res['jaccard']['max']:.3f}", "",
          "## By language", "", "| Language | Rate |", "|---|---|"]
    for k, d in res["by_language"].items():
        L.append(f"| {k} | {fmt_pct(d)} |")
    L += ["", "## By dataset", "", "| Dataset | Rate |", "|---|---|"]
    for k, d in res["by_dataset"].items():
        L.append(f"| {k} | {fmt_pct(d)} |")
    u = res["universal"]
    L += ["", "## Universal set (all 5 models)", "",
          f"**PyPI: {u['n_pypi']}  •  npm: {u['n_npm']}  •  total: {u['total']}** (names: private only)", "",
          "## Refusal rates", "", "| Model | Refusals | Gens | Rate |", "|---|---:|---:|---:|"]
    for m in MODELS:
        d = res["refusals"][m]
        L.append(f"| `{m}` | {d['refusals']} | {d['generations']} | {d['rate']:.3f}% |")
    path.write_text("\n".join(L) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(REPO / "data" / "runs.db"))
    ap.add_argument("--classification", default=None, help="classification.db from scripts/task1_analysis.py")
    ap.add_argument("--variant", choices=VARIANTS, default=None)
    ap.add_argument("--level", choices=LEVELS, default="mention")
    ap.add_argument("--out", default=str(REPO / "reports"))
    ap.add_argument("--private-out", default=str(REPO / "data" / "private" / "recompute"))
    args = ap.parse_args()
    if (args.classification is None) != (args.variant is None):
        raise SystemExit("--classification and --variant go together")

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    res = compute(conn, args.level, args.classification, args.variant)
    conn.close()
    tag = f"{args.variant or 'native'}_{args.level}"
    write_private(res, Path(args.private_out), tag)
    pub = public(res)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"recompute_{tag}.json").write_text(json.dumps(pub, indent=2, sort_keys=True) + "\n")
    write_md(pub, out_dir / f"recompute_{tag}.md")
    print(f"wrote {out_dir}/recompute_{tag}.json/.md; universal pypi={pub['universal']['n_pypi']} "
          f"npm={pub['universal']['n_npm']} (names only in {args.private_out})")


if __name__ == "__main__":
    main()
