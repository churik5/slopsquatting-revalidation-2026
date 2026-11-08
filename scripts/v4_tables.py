"""Tables and figures for paper v4 (offline). Aggregates and model names only.

    .venv/bin/python scripts/v4_tables.py

Inputs (read-only): reports/numbers.json, data/private/classification.db, data/runs.db,
data/private/recompute/universal_c_mention.json, data/master_lists/2026-07-23/*.csv,
ext/spracklen/Plots/Data/figure_14.csv, src/slop_bench/{config,orchestrator,batch}.py.
Outputs: reports/v4/tables.md, reports/v4/figure2_rates.png, reports/v4/figure3_jaccard.png,
reports/v4/v4_numbers.json; data/private/v4_examples.md (private, never committed).
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "src"))

import prereg_baseline as PB  # noqa: E402

if __name__ == "__main__":
    PB.block_network()

import csv  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import random  # noqa: E402
import re  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
from collections import Counter, defaultdict  # noqa: E402

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402
from rapidfuzz import process  # noqa: E402
from rapidfuzz.distance import Levenshtein  # noqa: E402

import make_diff_report as DIFF  # noqa: E402
import task1_analysis as T  # noqa: E402
import task2_analysis as T2  # noqa: E402

NUMBERS = REPO / "reports" / "numbers.json"
OUT = REPO / "reports" / "v4"
CLS_DB = REPO / "data" / "private" / "classification.db"
RUNS_DB = REPO / "data" / "runs.db"
UNIVERSAL = REPO / "data" / "private" / "recompute" / "universal_c_mention.json"
MASTER = REPO / "data" / "master_lists" / "2026-07-23"
SPRACKLEN = REPO / "ext" / "spracklen" / "Plots" / "Data" / "figure_14.csv"
EXAMPLES = REPO / "data" / "private" / "v4_examples.md"
MODELS = ["haiku-4-5", "sonnet-4-6", "gemini-2.5-pro", "deepseek-v3.2", "gpt-5.4-mini"]   # paper order
LABEL = DIFF.LABEL
LANGS = ["python", "javascript"]
DATASETS = ["SO_AT", "SO_LY", "LLM_AT", "LLM_LY"]
PROVIDER = {"haiku-4-5": "Anthropic", "sonnet-4-6": "Anthropic", "gemini-2.5-pro": "Google",
            "deepseek-v3.2": "DeepSeek", "gpt-5.4-mini": "OpenAI"}
REFUSAL_SQL = T.REFUSAL_SQL.replace("g.", "")
SEED = 42

# palette (dataviz reference instance): one series -> slot 1; sequential -> blue ramp; text tokens
BLUE = "#2a78d6"
TEXT1, TEXT2, GRID = "#0b0b0b", "#52514e", "#e4e3df"
SEQ = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6", "#256abf",
       "#1c5cab", "#184f95", "#104281", "#0d366b"]


# ── helpers ───────────────────────────────────────────────────────────────────
def pct(x, d=2):
    return "—" if x is None else f"{x:.{d}f}"


def rate_ci(r: dict) -> str:
    """recompute_all rate dict (percent)."""
    return f"{r['rate']:.2f} [{r['ci95'][0]:.2f}–{r['ci95'][1]:.2f}]"


def frac(r: dict) -> dict:
    """recompute_all rate dict -> task2 Wilson dict (fractions)."""
    return {"k": r["halluc"], "n": r["total"], "share": r["rate"] / 100, "ci95": [r["ci95"][0] / 100, r["ci95"][1] / 100]}


def diff_ci(a: dict, b: dict) -> str:
    d = T2.newcombe(frac(a), frac(b))
    return f"{100 * d['diff']:+.2f} [{100 * d['ci95'][0]:+.2f}, {100 * d['ci95'][1]:+.2f}]"


def pval(p: float) -> str:
    return "<0.0001" if p < 1e-4 else f"{p:.4f}" if p < 0.01 else f"{p:.3f}"


def table(head: list[str], rows: list[list], align: str | None = None) -> list[str]:
    al = align or ("l" + "r" * (len(head) - 1))
    sep = ["---:" if a == "r" else "---" for a in al]
    return ["| " + " | ".join(head) + " |", "| " + " | ".join(sep) + " |"] + \
        ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]


# ── 1. models ─────────────────────────────────────────────────────────────────
def models_table(n: dict) -> tuple[list[str], dict]:
    c = sqlite3.connect(f"file:{RUNS_DB}?mode=ro", uri=True)
    q = lambda s, *a: c.execute(s, a).fetchall()  # noqa: E731
    gens = dict(q("SELECT model_key, COUNT(*) FROM generations GROUP BY model_key"))
    refus = dict(q(f"SELECT model_key, SUM(CASE WHEN {REFUSAL_SQL} THEN 1 ELSE 0 END) FROM generations "
                   "GROUP BY model_key"))
    dups = dict(q("SELECT model_key, COUNT(*) FROM (SELECT model_key, prompt_id, n_sample, COUNT(*) k FROM generations "
                  "GROUP BY 1, 2, 3 HAVING k > 1) GROUP BY model_key"))
    calls = {m: (k, orphan, first, last) for m, k, orphan, first, last in q(
        "SELECT a.model_key, COUNT(*), SUM(g.id IS NULL), MIN(CASE WHEN g.id IS NULL THEN a.created_at END), "
        "MAX(CASE WHEN g.id IS NULL THEN a.created_at END) FROM api_calls a "
        "LEFT JOIN generations g ON g.id = a.generation_id GROUP BY a.model_key")}
    seq = dict(q("SELECT name, seq FROM sqlite_sequence"))["generations"]
    prompts = q("SELECT COUNT(DISTINCT prompt_id) FROM generations")[0][0]
    c.close()
    appx = DIFF.V3["appx_b"][0]
    rows, out = [], {}
    for m in MODELS:
        w = T2.wilson(refus[m], gens[m])
        k, orphan, first, last = calls[m]
        rows.append([LABEL[m], f"{gens[m]:,}", f"{refus[m]:,}",
                     f"{100 * w['share']:.2f} [{100 * w['ci95'][0]:.2f}–{100 * w['ci95'][1]:.2f}]",
                     f"{k:,}", f"{orphan:,}" + (f" ({first[:10]} – {last[:10]})" if orphan else ""), dups.get(m, 0)])
        out[m] = {"generations": gens[m], "refusals": refus[m], "refusal_share": w, "api_calls": k,
                  "api_calls_without_generation_row": orphan, "duplicate_prompt_sample_rows": dups.get(m, 0)}
    db_prov = {"anthropic": gens["haiku-4-5"] + gens["sonnet-4-6"], "openai": gens["gpt-5.4-mini"],
               "google": gens["gemini-2.5-pro"], "deepseek": gens["deepseek-v3.2"]}
    L = ["## 1. Models: generations, refusals, repeats", "",
         "Refusal = `generated_code` NULL, 'none' or empty (the stage A rule). Repeats: rows with the same "
         "(prompt, model, sample) in `generations`; API calls whose generation row is no longer in the table.", ""]
    L += table(["Model", "Generations", "Refusals", "Refusal % [95% CI]", "API calls", "Calls without a generation row",
                "Duplicate rows"], rows)
    L += ["", f"Distinct prompts: {prompts:,}; × 5 = {5 * prompts:,}; rows: {sum(gens.values()):,}. "
          f"`sqlite_sequence` for generations: {seq:,} (= rows + {seq - sum(gens.values()):,} deleted).", "",
          "Appendix B (v3, per provider) vs the database:", ""]
    L += table(["Provider", "v3 Appendix B", "Database", "Difference"],
               [[{"anthropic": "Anthropic", "openai": "OpenAI", "google": "Google", "deepseek": "DeepSeek"}[p], f"{appx[p]:,}", f"{db_prov[p]:,}",
                 f"{appx[p] - db_prov[p]:+,}"] for p in ("anthropic", "openai", "google", "deepseek")])
    dd = appx["deepseek"] - db_prov["deepseek"]
    L += ["", f"The database shows repeats only for GPT-5.4-mini: {calls['gpt-5.4-mini'][1]:,} API calls of an earlier "
          f"partial run ({calls['gpt-5.4-mini'][2][:10]} – {calls['gpt-5.4-mini'][3][:10]}) whose generation rows were "
          f"deleted before the full run. For DeepSeek the database has exactly {calls['deepseek-v3.2'][0]:,} API calls "
          f"and {gens['deepseek-v3.2']:,} generations, no duplicates; the v3 figure ({appx['deepseek']:,}, "
          f"{dd:+,}) is not explained by the database. The same holds for Google ({appx['google'] - db_prov['google']:+,})."]
    return L, {"per_model": out, "appendix_b_v3": appx, "database_per_provider": db_prov}


def costs_table() -> tuple[list[str], dict]:
    """API spend recorded by the pipeline (tokens x list price, batch discount applied), per provider."""
    c = sqlite3.connect(f"file:{RUNS_DB}?mode=ro", uri=True)
    rows = c.execute("""SELECT a.provider, a.batch, g.id IS NULL, COUNT(*), SUM(a.cost_usd), SUM(a.input_tokens),
                        SUM(a.output_tokens), MIN(a.created_at), MAX(a.created_at)
                        FROM api_calls a LEFT JOIN generations g ON g.id = a.generation_id GROUP BY 1, 2, 3""").fetchall()
    c.close()
    paper = {"anthropic": 375.26, "openai": 93.54, "google": 380.99, "deepseek": 11.11}   # checked below
    quotes = {"anthropic": "79,933 $375.26", "openai": "39,969 $93.54", "google": "39,968 $380.99",
              "deepseek": "39,975 $11.11"}
    txt = DIFF._norm(subprocess.run(["pdftotext", "-enc", "UTF-8", "-layout", str(DIFF.PDF), "-"],
                                    capture_output=True, text=True, check=True).stdout)
    if any(DIFF._norm(q) not in txt for q in quotes.values()):
        raise SystemExit("Appendix B cost quote not found in v3.pdf")
    prov = {"anthropic": "anthropic", "openai": "openai", "gemini": "google", "deepseek": "deepseek"}
    agg = defaultdict(lambda: [0, 0.0, 0, 0])
    early = None
    for p_, batch, orphan, k, cost, tin, tout, first, last in rows:
        a = agg[prov[p_]]
        a[0] += k
        a[1] += cost
        a[2] += tin
        a[3] += tout
        if orphan:
            early = {"provider": prov[p_], "calls": k, "cost_usd": cost, "input_tokens": tin, "output_tokens": tout,
                     "first": first, "last": last}
    trows = [[{"anthropic": "Anthropic", "openai": "OpenAI", "google": "Google", "deepseek": "DeepSeek"}[k],
              f"{v[0]:,}", f"{v[2]:,}", f"{v[3]:,}", f"${v[1]:,.2f}", f"${paper[k]:,.2f}", f"{round(v[1] - paper[k], 2) + 0.0:+,.2f}"]
             for k, v in agg.items()]
    tot = [sum(v[i] for v in agg.values()) for i in range(4)]
    trows.append(["**Total**", f"{tot[0]:,}", f"{tot[2]:,}", f"{tot[3]:,}", f"${tot[1]:,.2f}",
                  f"${sum(paper.values()):,.2f}", f"{tot[1] - sum(paper.values()):+,.2f}"])
    L = ["## 1b. Appendix B: API spend recorded in `api_calls`", "",
         "`api_calls` has cost and token counts for every call (none missing). Cost = the pipeline's own estimate "
         "(tokens × list price, batch discount applied), not provider invoices. v3 values: Appendix B of v3.pdf "
         "(quotes checked against the PDF).", ""]
    L += table(["Provider", "API calls", "Input tokens", "Output tokens", "Cost (DB)", "v3 Appendix B", "DB − v3"], trows)
    if early:
        L += ["", f"Early partial run of GPT-5.4-mini ({early['first'][:10]} – {early['last'][:10]}; generation rows "
              f"deleted): {early['calls']:,} calls, {early['input_tokens']:,} input and {early['output_tokens']:,} "
              f"output tokens, ${early['cost_usd']:,.2f} — included in the OpenAI total above."]
    return L + [""], {"per_provider": {k: {"calls": v[0], "cost_usd": round(v[1], 2), "input_tokens": v[2],
                                          "output_tokens": v[3]} for k, v in agg.items()},
                      "early_gpt_run": early, "v3_appendix_b_cost": paper}


# ── 2. language ───────────────────────────────────────────────────────────────
def language_tables(n: dict) -> list[str]:
    L = ["## 2. Rates by language (v4 = live revalidation), % with 95% Wilson CI; Δ with 95% Newcombe CI", ""]
    for lvl in ("mention", "generation"):
        r = n["recompute"][f"c_{lvl}"]
        rows = []
        for m in MODELS:
            py, js = r["by_model_language"][m]["python"], r["by_model_language"][m]["javascript"]
            rows.append([LABEL[m], rate_ci(py), rate_ci(js), diff_ci(py, js), rate_ci(r["per_model"][m])])
        L += [f"**{lvl.capitalize()} level**", ""]
        L += table(["Model", "Python", "JavaScript", "Δ Py − JS (pp)", "Overall"], rows)
        L.append("")
    return L


# ── 3. pairs ──────────────────────────────────────────────────────────────────
def pairs_table(n: dict) -> list[str]:
    rm, rg = n["recompute"]["c_mention"], n["recompute"]["c_generation"]
    perm = {frozenset((x["a"], x["b"])): x["p_perm"] for x in rm["permutation"]["pairs"]}
    gen = {frozenset((x["a"], x["b"])): x for x in rg["chi2"]["pairs"]}
    rows = []
    for x in sorted(rm["chi2"]["pairs"], key=lambda x: -x["delta_pp"]):
        k = frozenset((x["a"], x["b"]))
        a, b = sorted((x["a"], x["b"]), key=lambda m: rm["per_model"][m]["rate"])
        g = gen[k]
        rows.append([f"{LABEL[a]} vs {LABEL[b]}", f"{x['delta_pp']:.2f}", f"{x['chi2']:.2f}", pval(x["p_holm"]),
                     pval(perm[k]), "***" if x["significant"] else "ns",
                     ("***" if g["significant"] else "ns") + f" (Δ {g['delta_pp']:.2f}, p_Holm {pval(g['p_holm'])})"])
    L = ["## 3. Pairwise differences (mention level, v4)", "",
         f"χ² on 2×2 tables, Holm correction over 10 pairs; permutation test 10 000 resamples, seed 42. "
         f"Significant pairs: mention level {rm['chi2']['n_significant']}/10, generation level "
         f"{rg['chi2']['n_significant']}/10.", ""]
    L += table(["Pair", "Δ (pp)", "χ²", "p_Holm", "p_perm", "Sig.", "Generation level"], rows, "lrrrrll")
    return L + [""]


# ── 4. figure 2 ───────────────────────────────────────────────────────────────
def spracklen_gpt4t() -> float:
    with open(SPRACKLEN, newline="") as f:
        for r in csv.reader(f):
            if r and r[0].strip().lower() == "gpt 4 turbo":
                return float(r[1].strip().rstrip("%"))
    raise SystemExit("GPT 4 Turbo not found in figure_14.csv")


def figure2(n: dict, ref: float) -> list[str]:
    rm, rg = n["recompute"]["c_mention"], n["recompute"]["c_generation"]
    order = sorted(MODELS, key=lambda m: rm["per_model"][m]["rate"])
    fig, axes = plt.subplots(1, 2, figsize=(9.2, 3.4), sharey=True, sharex=True, dpi=300)
    for ax, r, title in ((axes[0], rm, "(a) Mention level"), (axes[1], rg, "(b) Generation level (PREREG §3.1)")):
        for i, m in enumerate(order):
            d = r["per_model"][m]
            ax.plot([d["ci95"][0], d["ci95"][1]], [i, i], color=BLUE, lw=2, solid_capstyle="round", zorder=2)
            ax.plot(d["rate"], i, "o", ms=8, color=BLUE, markeredgecolor="white", markeredgewidth=2, zorder=3)
        ax.set_title(title, loc="left", fontsize=10, color=TEXT1)
        ax.grid(axis="x", color=GRID, lw=0.8)
        ax.set_axisbelow(True)
        for s in ("top", "right", "left"):
            ax.spines[s].set_visible(False)
        ax.spines["bottom"].set_color(TEXT2)
        ax.tick_params(colors=TEXT2, labelsize=9, length=0)
        ax.set_xlabel("Hallucination rate, % (95% Wilson CI)", fontsize=9, color=TEXT2)
    axes[0].axvline(ref, color=TEXT2, ls=(0, (4, 3)), lw=1.2, zorder=1)
    axes[0].text(ref, len(order) - 0.45, f"Spracklen et al., GPT-4 Turbo: {ref:.2f}% ", fontsize=8, color=TEXT2,
                 va="bottom", ha="right")
    axes[0].set_yticks(range(len(order)), [LABEL[m] for m in order])
    axes[0].set_ylim(-0.6, len(order) - 0.1)
    xmax = max(r["per_model"][m]["ci95"][1] for r in (rm, rg) for m in MODELS)
    axes[0].set_xlim(0, max(xmax, ref) * 1.12)
    fig.tight_layout()
    fig.savefig(OUT / "figure2_rates.png", facecolor="white")
    plt.close(fig)
    return ["## 4. Figure 2", "", "![Figure 2](figure2_rates.png)", "",
            f"Per-model hallucination rate after live revalidation with 95% Wilson CI: (a) mention level (the unit of "
            f"the v3 figure), (b) generation level (≥ 1 hallucinated name; denominator = all generations). Dashed line: "
            f"{ref:.2f}% (Spracklen et al., GPT-4 Turbo, `ext/spracklen/Plots/Data/figure_14.csv`), shown on panel (a) "
            f"only, because Spracklen's unit is the package mention, not the generation. Models are ordered by the "
            f"mention-level rate in both panels; the x-axis is shared.", ""]


# ── 5. figure 3 ───────────────────────────────────────────────────────────────
def figure3(n: dict) -> list[str]:
    j = n["recompute"]["c_mention"]["jaccard"]
    val = {frozenset((x["a"], x["b"])): x["jaccard"] for x in j["pairs"]}
    k = len(MODELS)
    cmap = LinearSegmentedColormap.from_list("seq_blue", SEQ)
    vmin = math.floor(min(val.values()) * 20) / 20       # colour scale spans the data range, rounded to 0.05
    vmax = math.ceil(max(val.values()) * 20) / 20
    fig, ax = plt.subplots(figsize=(5.6, 4.6), dpi=300)
    for i in range(k):
        for jj in range(k):
            if i == jj:
                continue
            v = val[frozenset((MODELS[i], MODELS[jj]))]
            ax.add_patch(plt.Rectangle((jj - 0.5 + 0.02, i - 0.5 + 0.02), 0.96, 0.96,
                                       color=cmap((v - vmin) / (vmax - vmin)), lw=0))
            ax.text(jj, i, f"{v:.3f}", ha="center", va="center", fontsize=9,
                    color="white" if (v - vmin) / (vmax - vmin) > 0.55 else TEXT1)
    ax.set_xlim(-0.5, k - 0.5)
    ax.set_ylim(k - 0.5, -0.5)
    ax.set_xticks(range(k), [LABEL[m] for m in MODELS], rotation=30, ha="right", fontsize=8.5, color=TEXT2)
    ax.set_yticks(range(k), [LABEL[m] for m in MODELS], fontsize=8.5, color=TEXT2)
    ax.tick_params(length=0)
    for s in ax.spines.values():
        s.set_visible(False)
    sm = plt.cm.ScalarMappable(cmap=cmap, norm=plt.Normalize(vmin, vmax))
    cb = fig.colorbar(sm, ax=ax, fraction=0.046, pad=0.04)
    cb.set_label("Jaccard similarity", fontsize=8.5, color=TEXT2)
    cb.outline.set_visible(False)
    cb.ax.tick_params(labelsize=8, colors=TEXT2, length=0)
    fig.tight_layout()
    fig.savefig(OUT / "figure3_jaccard.png", facecolor="white")
    plt.close(fig)
    hi = max(j["pairs"], key=lambda x: x["jaccard"])
    lo = min(j["pairs"], key=lambda x: x["jaccard"])
    rows = [[LABEL[m]] + ["—" if m == m2 else f"{val[frozenset((m, m2))]:.3f}" for m2 in MODELS] for m in MODELS]
    L = ["## 5. Figure 3: Jaccard similarity of unique hallucinated names (v4)", "", "![Figure 3](figure3_jaccard.png)", "",
         f"Mean {j['mean']:.3f}, range {j['min']:.3f}–{j['max']:.3f}. Maximum: {LABEL[hi['a']]} – {LABEL[hi['b']]} "
         f"({hi['jaccard']:.3f}; {hi['inter']} shared of {hi['union']}). Minimum: {LABEL[lo['a']]} – {LABEL[lo['b']]} "
         f"({lo['jaccard']:.3f}; {lo['inter']} shared of {lo['union']}). Sets = names hallucinated as of generation "
         f"after live revalidation (normalized names). Colour scale spans the data range (not from 0).", ""]
    L += table(["Model"] + [LABEL[m] for m in MODELS], rows)
    return L + [""]


# ── 6. datasets ───────────────────────────────────────────────────────────────
def dataset_tables(n: dict) -> list[str]:
    L = ["## 6. Rates by dataset (v4), % with 95% Wilson CI", ""]
    for lvl in ("mention", "generation"):
        r = n["recompute"][f"c_{lvl}"]
        rows = [[LABEL[m]] + [rate_ci(r["by_model_dataset"][m][ds]) for ds in DATASETS] for m in MODELS]
        rows.append(["All models"] + [rate_ci(r["by_dataset"][ds]) for ds in DATASETS])
        L += [f"**{lvl.capitalize()} level**", ""]
        L += table(["Model"] + DATASETS, rows)
        L.append("")
    return L


# ── master lists ──────────────────────────────────────────────────────────────
def load_master() -> dict:
    out = {}
    for e in ("pypi", "npm"):
        with open(MASTER / f"{e}_package_names.csv", encoding="utf-8", errors="replace") as f:
            names = [ln.strip() for ln in f if ln.strip()]
        out[e] = {x.lower() if e == "npm" else PB.norm_pypi(x) for x in names}
    out["npm_scopes"] = {x.split("/", 1)[0] for x in out["npm"] if x.startswith("@") and "/" in x}
    return out


# ── 7. table 4 ────────────────────────────────────────────────────────────────
def table4(n: dict, master: dict) -> tuple[list[str], dict]:
    uni = json.loads(UNIVERSAL.read_text())
    stdlib, _ = PB.load_stdlib()
    c = sqlite3.connect(f"file:{CLS_DB}?mode=ro", uri=True)
    ment = {(e, nm): k for e, nm, k in c.execute(
        "SELECT ecosystem, name_c, COUNT(*) FROM mentions WHERE halluc_c = 1 GROUP BY 1, 2")}
    c.close()
    cat, extra_stdlib, exact, dist = {}, 0, 0, {}
    for e in ("pypi", "npm"):
        targets = master[e] - (stdlib if e == "pypi" else set())
        by_len = defaultdict(list)
        for x in targets:
            by_len[len(x)].append(x)
        for nm in uni[e]:
            if nm in master[e]:
                exact += 1
            if e == "npm" and nm.startswith("@") and "/" in nm and nm.split("/", 1)[0] in master["npm_scopes"]:
                cat[(e, nm)] = "scope"
                continue
            cands = [x for d in range(-2, 3) for x in by_len.get(len(nm) + d, ()) if x != nm]
            hit = process.extractOne(nm, cands, scorer=Levenshtein.distance, score_cutoff=2) if cands else None
            dist[(e, nm)] = None if hit is None else int(hit[1])          # smallest distance (1 or 2), else None
            if hit is not None:
                cat[(e, nm)] = "near"
                continue
            cat[(e, nm)] = "plausible"
            if e == "pypi":
                sl = [x for x in stdlib if abs(len(x) - len(nm)) <= 2 and x != nm]
                if sl and process.extractOne(nm, sl, scorer=Levenshtein.distance, score_cutoff=2) is not None:
                    extra_stdlib += 1
    labels = {"scope": "npm scope already owned (packages published under it, e.g. @ember/*)",
              "near": "Near match: an existing package within Levenshtein distance ≤ 2",
              "plausible": "Plausible invention (neither of the above)"}
    rows, res = [], {}
    for k in ("scope", "near", "plausible"):
        r = []
        for e in ("pypi", "npm"):
            names = [nm for (ee, nm), v in cat.items() if ee == e and v == k]
            r += [len(names), sum(ment.get((e, nm), 0) for nm in names)]
        rows.append([labels[k]] + [f"{x:,}" for x in r] + [f"{r[0] + r[2]:,}", f"{r[1] + r[3]:,}"])
        res[k] = {"pypi_names": r[0], "pypi_mentions": r[1], "npm_names": r[2], "npm_mentions": r[3]}
    tot = [sum(res[k][x] for k in res) for x in ("pypi_names", "pypi_mentions", "npm_names", "npm_mentions")]
    rows.append(["**Total (universal set)**"] + [f"{x:,}" for x in tot] + [f"{tot[0] + tot[2]:,}", f"{tot[1] + tot[3]:,}"])
    L = ["## 7. Table 4 (new, without names): the universal set after revalidation", "",
         "Categories are exclusive, assigned in the order shown. Mentions = hallucinated mentions (v4) summed over the "
         "names of the category. Scope ownership and near matches use the master lists of 2026-07-23 "
         "(`data/master_lists/2026-07-23/`, PyPI normalized per PEP 503, npm lowercased). A scope counts as owned if at "
         "least one package is published under it (owner may be an organisation or a user; outsiders cannot publish "
         "there). Near match: Levenshtein distance 1–2 to a listed package; the Python stdlib module names added to "
         f"the PyPI list are not counted as packages (counting them would move {extra_stdlib} more name(s) into this "
         f"category). Universal names present verbatim in the 2026-07-23 lists: {exact}.", ""]
    L += table(["Category", "PyPI names", "PyPI mentions", "npm names", "npm mentions", "Names", "Mentions"], rows)
    variants = {"distance_1": lambda e, nm: dist.get((e, nm)) == 1,
                "distance_le_2_and_length_gt_8": lambda e, nm: dist.get((e, nm)) is not None and len(nm) > 8}
    vrows, vres = [], {}
    for k, pred in variants.items():
        r = []
        for e in ("pypi", "npm"):
            names = [nm for (ee, nm), v in cat.items() if ee == e and v != "scope" and pred(e, nm)]
            r += [len(names), sum(ment.get((e, nm), 0) for nm in names)]
        vres[k] = {"pypi_names": r[0], "pypi_mentions": r[1], "npm_names": r[2], "npm_mentions": r[3]}
        vrows.append([{"distance_1": "Levenshtein distance = 1",
                       "distance_le_2_and_length_gt_8": "Distance ≤ 2, only names longer than 8 characters"}[k]]
                     + [f"{x:,}" for x in r] + [f"{r[0] + r[2]:,}", f"{r[1] + r[3]:,}"])
    vrows.append(["Reference: distance ≤ 2, any length (table above)"] +
                 [f"{res['near'][x]:,}" for x in ("pypi_names", "pypi_mentions", "npm_names", "npm_mentions")] +
                 [f"{res['near']['pypi_names'] + res['near']['npm_names']:,}",
                  f"{res['near']['pypi_mentions'] + res['near']['npm_mentions']:,}"])
    L += ["", "**Near-match category, stricter variants** (same universal set, same master lists; names in an owned npm "
          "scope are not counted; length = characters of the normalized name, scope included)", ""]
    L += table(["Variant", "PyPI names", "PyPI mentions", "npm names", "npm mentions", "Names", "Mentions"], vrows)
    res["near_match_variants"] = vres
    fate = n["universal"]["april_list_fate"]
    keys = sorted(set(fate["pypi"]) | set(fate["npm"]))
    L += ["", "**Fate of the April lists (v3 universal set in the database), as in stage A**", ""]
    L += table(["Outcome", "PyPI", "npm"], [[k.replace("_", " "), fate["pypi"].get(k, 0), fate["npm"].get(k, 0)]
                                            for k in keys] + [["**Total**", n["universal"]["v3_april_lists"]["pypi"],
                                                               n["universal"]["v3_april_lists"]["npm"]]])
    return L + [""], {"categories": res, "stdlib_near_matches_not_counted": extra_stdlib,
                      "universal_names_in_master_lists": exact}


# ── 8. v3 -> v4 ───────────────────────────────────────────────────────────────
def short_reason(item: str, reason: str, n: dict) -> str:
    if item == "Generations":
        g = n["generations_table"]
        miss = "; ".join(f"{LABEL[m]} {k}" for m, k in g["missing_per_model"].items() if k)
        return f"generations missing in the database: {miss}; the v3 total = prompts × 5"
    if item == "Range (mention)":
        return "published values not reproduced exactly (v3 method on the database: " + \
            re.search(r"v3 method gives ([^;)]*)", reason).group(1) + "); per-model changes above"
    if item.endswith("(generation)"):
        return "PREREG §3.1 unit; not reported in v3"
    if item.startswith("Rate "):
        m = re.search(r"mentions (\d+) → (\d+): (.*?)(?:; denominator|\. Published)", reason)
        return f"hallucinated mentions {m.group(1)} → {m.group(2)}: {m.group(3)}" if m else reason
    if item.startswith("Refusals"):
        return "refusal definition (generated_code NULL / 'none' / empty)"
    if item.startswith("Universal set, PyPI"):
        return "D1/D11 import-only, pre-existing packages, UNKNOWN; 109 not reproducible (DB: 121)"
    if item.startswith("Universal set, npm"):
        return "2 pre-existing packages"
    if item.startswith("Python / JS"):
        return "language split of the per-model changes"
    return reason.split(";")[0].split(". ")[0]


def v3_v4_table(n: dict) -> list[str]:
    def labels(t: str) -> str:
        for m in MODELS:
            t = t.replace(f"({m})", f"({LABEL[m]})")
        return t

    rows = [[r["item"], "— (mention level only)" if r["item"].endswith("(generation)") else r["v3"],
             labels(r["new"]), short_reason(r["item"], r["reason"], n)] for r in n["v3_to_new"]["rows"]]
    L = ["## 8. v3 → v4 (paper format)", "",
         "v3 = published value (every value checked against the text of v3.pdf); v4 = live revalidation. "
         "Full reasons: `reports/diff_v3_vs_new.md`.", ""]
    return L + table(["Quantity", "v3", "v4", "Main reason"], rows, "llll") + [""]


# ── 9. task 2 ─────────────────────────────────────────────────────────────────
def w(x: dict) -> str:
    return f"{x['k']}/{x['n']} = {100 * x['share']:.1f}% [{100 * x['ci95'][0]:.1f}–{100 * x['ci95'][1]:.1f}]" \
        if x["n"] else "0/0"


def task2_table(n: dict) -> list[str]:
    t = n["task2"]
    g = t["groups"]
    L = ["## 9. Task 2: registration after exposure", "",
         f"Window: exposure start → T0 ({t['t0_utc'][:10]}). Exposure start: A1 = arXiv submission of the first version "
         f"printing the name (v1 {t['exposure_start']['A1_v1_submission'][:10]}, v2 "
         f"{t['exposure_start']['A1_v2_submission'][:10]}); A2 and B = {t['exposure_start']['A2_and_B'][:10]}.", ""]
    L += table(["Group", "PyPI", "npm", "Total"],
               [[k, g["pypi"][k], g["npm"][k], g["pypi"][k] + g["npm"][k]] for k in ("A", "A1", "A2", "B", "B4")])
    p = t["primary"]
    pr = p["primary"]
    d = pr["difference"]
    rows = [["**Primary: A vs B4, common strata**", w(pr["A"]), w(pr["B4"]),
             f"{100 * d['diff']:+.1f} [{100 * d['ci95'][0]:+.1f}, {100 * d['ci95'][1]:+.1f}]", f"{pr['fisher']['p']:.3f}"]]
    for e in ("pypi", "npm"):
        x = p["per_ecosystem"][e]
        dd = x["difference"]
        rows.append([f"{e} (sensitivity)", w(x["A"]), w(x["B4"]),
                     f"{100 * dd['diff']:+.1f} [{100 * dd['ci95'][0]:+.1f}, {100 * dd['ci95'][1]:+.1f}]",
                     f"{x['fisher']['p']:.3f}"])
    for lab, key, a, b in (("A vs all B (descriptive)", "descriptive_A_vs_all_B", "A", "B"),
                           ("A1 vs A2 (descriptive)", "descriptive_A1_vs_A2", "A1", "A2")):
        x = p[key]
        dd = x["difference"]
        rows.append([lab, w(x[a]), w(x[b]), f"{100 * dd['diff']:+.1f} [{100 * dd['ci95'][0]:+.1f}, {100 * dd['ci95'][1]:+.1f}]",
                     f"{x['fisher']['p']:.3f}"])
    L += ["", f"Registered share, 95% Wilson CI; difference in pp, 95% Newcombe CI; two-sided Fisher exact test. "
          f"Common strata: {p['common_strata']}.", ""]
    L += table(["Comparison", "First group", "Second group", "Difference (pp)", "Fisher p"], rows, "lllrr")
    m = t["mde_final_sizes"]["pooled"]
    L += ["", "Minimum detectable effect (pooled, n A = {}, n B4 = {}; power 0.8, α 0.05): ".format(m[0]["n_a"], m[0]["n_b"])
          + "; ".join(f"baseline {x['p0']:.0%} → {x['mde_share_a']:.1%} in A (+{100 * x['mde_abs_diff']:.1f} pp)"
                      for x in m) + "."]
    tm = t["exposure_to_creation_days"]
    L += ["Time from exposure start to creation (registered in the window): "
          + "; ".join(f"{k}: n = {v['n']}" + (f", median {v['median_days']:.0f} days (range {v['min_days']:.0f}–"
                                              f"{v['max_days']:.0f})" if v["n"] else "") for k, v in tm.items()) + ".",
          "Result: no effect detected at this precision.", ""]
    return L


# ── 10. text numbers ──────────────────────────────────────────────────────────
def text_numbers(n: dict) -> tuple[list[str], dict]:
    r = n["recompute"]["c_mention"]
    c = sqlite3.connect(f"file:{CLS_DB}?mode=ro", uri=True)
    c.execute("ATTACH DATABASE ? AS runs", (f"file:{RUNS_DB}?mode=ro",))
    per = defaultdict(lambda: defaultdict(set))
    for m, e, nm in c.execute("SELECT g.model_key, x.ecosystem, x.name_c FROM mentions x JOIN runs.generations g "
                              "ON g.id = x.generation_id WHERE x.halluc_c = 1"):
        per[m][e].add(nm)
    c.close()
    rows, out = [], {}
    for m in MODELS:
        u = {e: len(per[m][e]) for e in ("pypi", "npm")}
        rows.append([LABEL[m], f"{r['per_model'][m]['halluc']:,}", f"{r['per_model'][m]['total']:,}",
                     f"{sum(u.values()):,}", f"{u['pypi']:,}", f"{u['npm']:,}"])
        out[m] = {"hallucinated_mentions": r["per_model"][m]["halluc"], "unique_names": u}
    allu = {e: len(set().union(*(per[m][e] for m in MODELS))) for e in ("pypi", "npm")}
    rows.append(["All models", f"{sum(r['per_model'][m]['halluc'] for m in MODELS):,}",
                 f"{sum(r['per_model'][m]['total'] for m in MODELS):,}", f"{sum(allu.values()):,}",
                 f"{allu['pypi']:,}", f"{allu['npm']:,}"])
    L = ["## 10. Numbers for the text (v4)", ""]
    L += table(["Model", "Hallucinated mentions", "All mentions", "Unique hallucinated names", "…PyPI", "…npm"], rows)
    return L + [""], {"per_model": out, "all_models_unique": allu}


# ── 11. extended thinking ─────────────────────────────────────────────────────
def thinking_check() -> list[str]:
    def lines(path):
        return (REPO / path).read_text().splitlines()

    def find(path, rx, start=0):
        for i, ln in enumerate(lines(path)[start:], start + 1):
            if re.search(rx, ln):
                return i
        raise SystemExit(f"pattern not found in {path}: {rx}")

    cfg = "src/slop_bench/config.py"
    s0, h0 = find(cfg, r'"sonnet-4-6": ModelSpec'), find(cfg, r'"haiku-4-5": ModelSpec')
    s_kw, h_kw = find(cfg, r"extra_kwargs=\{\}", s0), find(cfg, r"extra_kwargs=\{\}", h0)
    s_comment = find(cfg, r"Extended thinking defaults to OFF", s0)
    orch = "src/slop_bench/orchestrator.py"
    o_kw = find(orch, r"\*\*spec\.extra_kwargs")
    bat = "src/slop_bench/batch.py"
    b_params = find(bat, r"params = \{")
    code_hits = []
    for f in sorted((REPO / "src" / "slop_bench").rglob("*.py")):
        for i, ln in enumerate(f.read_text().splitlines(), 1):
            code = ln.split("#", 1)[0]
            if re.search(r"\bthinking\s*[=:]|[\"']thinking[\"']|budget_tokens|thinking_budget", code):
                code_hits.append(f"{f.relative_to(REPO)}:{i}")
    commit = subprocess.run(["git", "log", "-1", "--format=%h %ad", "--date=short", "--", cfg, orch, bat],
                            cwd=REPO, capture_output=True, text=True, check=True).stdout.strip()
    return ["## 11. Extended thinking for Haiku 4.5 and Sonnet 4.6 (code check)", "",
            f"- Sonnet 4.6: `{cfg}:{s_kw}` `extra_kwargs={{}}`; comment at `{cfg}:{s_comment}`: extended thinking is "
            f"off unless `thinking=...` is passed, and it is never passed.",
            f"- Haiku 4.5: `{cfg}:{h_kw}` `extra_kwargs={{}}`.",
            f"- Synchronous calls: request kwargs are built at `{orch}:{o_kw}` from CODE_GEN_KWARGS and "
            f"`spec.extra_kwargs` only.",
            f"- Batch API (Anthropic): request params at `{bat}:{b_params}` contain model, max_tokens, temperature, "
            f"messages; no `thinking`.",
            f"- A `thinking` / `budget_tokens` / `thinking_budget` parameter in code (outside comments) under "
            f"`src/slop_bench/`: "
            f"{len(code_hits)}" + (f" ({', '.join(code_hits)})" if code_hits else "") + ".",
            f"- Fact: the run code passes no extended-thinking parameter for either model. These files are unchanged "
            f"since commit {commit}; whether that commit is byte-identical to the code that ran in April cannot be "
            f"proven from the repository.", ""]


# ── 12. examples (private) ────────────────────────────────────────────────────
def examples(master: dict) -> dict:
    """Five real examples; every extracted name in the generation is existing or closed."""
    stdlib, _ = PB.load_stdlib()
    node_bi, _ = PB.load_node_builtins()
    c = sqlite3.connect(f"file:{CLS_DB}?mode=ro", uri=True)
    st = {(e, nm): (cat, asof, excl) for e, nm, cat, asof, excl in c.execute(
        "SELECT ecosystem, name, category, asof_status, exclusion FROM names")}
    c.close()

    def scope_owned(nm):
        return nm.startswith("@") and "/" in nm and nm.split("/", 1)[0] in master["npm_scopes"]

    def status(e, raw):
        """-> (state, feature): state 'existing' | 'closed:<why>' | 'open'; feature None | 'halluc' | 'import_only'.
        'halluc' = hallucination as of generation in an owned npm scope (closed); 'import_only' = D1/D11 name
        whose own name is taken or closed."""
        nm = PB.NORM[e](raw)
        cat, asof, excl = st.get((e, nm), (None, None, None))
        if excl == "D15_invalid_name" or not T.VALID[e](nm):
            return "closed:invalid name", None
        if (e == "pypi" and nm in stdlib) or (e == "npm" and (nm in node_bi or nm.startswith("node:"))):
            return "closed:stdlib/built-in", None
        if cat is not None:                                    # live-checked name: the live state decides
            d11 = "import_only" if excl == "D1_D11_pipreqs_import_only" else None
            if asof == "H_REGISTERED_LATER" or cat == "UNPUBLISHED":
                return "open", None                            # unknown owner / re-registrable: avoid
            if cat == "REMOVED_MALICIOUS":
                return "closed:removed as malicious", d11
            if cat == "SECURITY_HOLDING":
                return "closed:npm security holding", d11
            if cat in ("EXISTS", "EXISTS_SIMPLE_ONLY"):
                return "existing", d11
            if e == "npm" and scope_owned(nm):
                return "closed:npm scope owned by others", ("halluc" if asof == "H_ABSENT" and excl is None else None)
            return "open", None
        if e == "npm" and scope_owned(nm):
            return "closed:npm scope owned by others", None
        if nm in master[e]:
            return "existing", None
        return "open", None

    r = sqlite3.connect(f"file:{RUNS_DB}?mode=ro", uri=True)
    hits = defaultdict(list)
    spell = defaultdict(set)                          # (gid, key) -> spellings as written in the response
    for gid, raw, heur, praw in r.execute("SELECT generation_id, package_norm, heuristic, package_raw FROM heuristic_hits"):
        hits[gid].append((raw, heur))
        spell[(gid, raw)].add(praw)
    gens = {gid: (pid, m, lang, ds) for gid, pid, m, lang, ds in r.execute(
        "SELECT id, prompt_id, model_key, language, dataset FROM generations")}
    cand = defaultdict(list)
    for gid, hs in hits.items():
        pid, m, lang, ds = gens[gid]
        e = T.LANG2ECO[lang]
        res = [(raw, heur, *status(e, raw)) for raw, heur in hs]
        if any(s == "open" for _r, _h, s, _f in res):
            continue
        for raw, heur, s, f in res:
            if f == "halluc":
                cand[ds].append((gid, raw, s))
            elif f == "import_only" and all(h == "IMPORTS" for rr, h in hs if rr == raw):
                cand["import_only"].append((gid, raw, s))
    def snippet(gid, raw):
        code = r.execute("SELECT generated_code FROM generations WHERE id = ?", (gid,)).fetchone()[0] or ""
        lines = code.splitlines()
        words = {raw.lower()} | {x.lower() for x in spell[(gid, raw)]}
        idx = next((i for i, ln in enumerate(lines) if any(w in ln.lower() for w in words)), None)
        return None if idx is None else [ln[:160] for ln in lines[max(0, idx - 1): idx + 2]][:3]

    rng = random.Random(SEED)
    picked, counts, snips = {}, {k: len(v) for k, v in cand.items()}, {}
    for kind in DATASETS + ["import_only"]:
        pool = sorted(cand.get(kind, []))
        rng.shuffle(pool)
        for gid, raw, s in pool:                       # first candidate (seed 42 order) whose name is in the text
            sn = snippet(gid, raw)
            if sn:
                picked[kind], snips[kind] = (gid, raw, s), sn
                break
    L = ["# v4 Appendix C examples (PRIVATE — never commit)", "",
         "Every extracted name in each generation is existing (2026-07-23 master list, not contradicted by the live "
         "check) or closed (owned npm scope, removed as malicious, npm security holding, stdlib/built-in, invalid "
         "name). Generations with any re-registrable name, an UNPUBLISHED name or a name registered after generation "
         "were skipped. Selection: first candidate in seed-42 order whose name appears in the response text. Check by hand before publishing.", ""]
    for kind, (gid, raw, s) in picked.items():
        pid, m, lang, ds = gens[gid]
        snip = snips[kind]
        L += [f"## {kind}", "", f"- prompt id: `{pid}`; model: {LABEL[m]}; dataset: {ds}; language: {lang}",
              f"- featured name status: {s}; generation id {gid}; other names in the generation: "
              f"{len(hits[gid]) - 1} extraction(s), all existing or closed", "", "```" + lang, *snip, "```", ""]
    r.close()
    EXAMPLES.write_text("\n".join(L))
    return {"candidates_per_kind": counts, "picked": sorted(picked)}


# ── 12b. masked examples (private) ────────────────────────────────────────────
MASK = "⟨withheld⟩"


def name_rx(e: str, spellings: set[str]) -> re.Pattern:
    alts = set()
    for sp in spellings:
        parts = [re.escape(x) for x in re.split(r"[-_.]", sp)] if e == "pypi" else [re.escape(sp)]
        alts.add(r"[-_.]?".join(parts) if e == "pypi" else parts[0])
    body = "|".join(sorted(alts, key=len, reverse=True))
    return re.compile(r"(?<![A-Za-z0-9_@/.-])(?:" + body + r")(?![A-Za-z0-9_-])", re.IGNORECASE)


def examples_masked(master: dict) -> dict:
    """Appendix C: random (seed 42) generations with >= 1 hallucination; one per dataset + one import-only.
    Every name that is not existing / closed is replaced by MASK. Private output only."""
    stdlib, _ = PB.load_stdlib()
    node_bi, _ = PB.load_node_builtins()
    c = sqlite3.connect(f"file:{CLS_DB}?mode=ro", uri=True)
    st = {(e, nm): (cat, asof, excl) for e, nm, cat, asof, excl in c.execute(
        "SELECT ecosystem, name, category, asof_status, exclusion FROM names")}
    halluc_gens = {gid for (gid,) in c.execute("SELECT DISTINCT generation_id FROM mentions WHERE halluc_c = 1")}
    d11_gens = defaultdict(set)
    for gid, nm in c.execute("SELECT generation_id, name_c FROM mentions WHERE status_c = 'D1_D11_pipreqs_import_only'"):
        d11_gens[gid].add(nm)
    c.close()

    def keep(e, nm):
        """True = may be shown: existing, owned npm scope, stdlib/built-in, invalid name, closed D1/D11 import."""
        cat, asof, excl = st.get((e, nm), (None, None, None))
        if excl == "D15_invalid_name" or not T.VALID[e](nm):
            return True
        if (e == "pypi" and nm in stdlib) or (e == "npm" and (nm in node_bi or nm.startswith("node:"))):
            return True
        scoped = e == "npm" and nm.startswith("@") and "/" in nm and nm.split("/", 1)[0] in master["npm_scopes"]
        if cat is not None:
            if asof == "H_REGISTERED_LATER" or cat in ("ABSENT", "UNPUBLISHED", "UNKNOWN"):
                return scoped and cat == "ABSENT"
            return True                       # EXISTS (before generation), EXISTS_SIMPLE_ONLY, SECURITY_HOLDING, REMOVED_MALICIOUS
        return scoped or nm in master[e]

    # every live-checked name that may not be shown, in both ecosystems (PyPI compared after PEP 503)
    risky = {"pypi": {nm for (e, nm) in st if e == "pypi" and st[(e, nm)][0] is not None and not keep(e, nm)},
             "npm": {nm for (e, nm) in st if e == "npm" and st[(e, nm)][0] is not None and not keep(e, nm)}}
    dict_words = set()
    if Path("/usr/share/dict/words").exists():
        dict_words = {w.strip().lower() for w in open("/usr/share/dict/words", encoding="utf-8", errors="ignore")}
    pkg_line = re.compile(r"\b(import|require|from|install|pip|npm|yarn|pnpm|add)\b")
    tok_rx = re.compile(r"[A-Za-z0-9@][A-Za-z0-9@/._-]*")

    def is_risky(tok: str) -> bool:
        t = tok.lower().rstrip(".")
        return t in risky["npm"] or PB.norm_pypi(t) in risky["pypi"]

    def mask_tokens(line: str) -> str:
        """Mask any token equal to a registrable / unknown name; ordinary English words only on package lines."""
        on_pkg_line = bool(pkg_line.search(line))

        def sub(m):
            tok = m.group(0)
            if not is_risky(tok):
                return tok
            if tok.lower().rstrip(".") in dict_words and not on_pkg_line:
                return tok
            return MASK
        return tok_rx.sub(sub, line)

    r = sqlite3.connect(f"file:{RUNS_DB}?mode=ro", uri=True)
    hits = defaultdict(lambda: defaultdict(set))
    for gid, key, praw in r.execute("SELECT generation_id, package_norm, package_raw FROM heuristic_hits"):
        hits[gid][key].add(praw)
    gens = {gid: (pid, m, lang, ds) for gid, pid, m, lang, ds in r.execute(
        "SELECT id, prompt_id, model_key, language, dataset FROM generations")}
    prompts = dict(r.execute("SELECT prompt_id, MAX(prompt_text) FROM generations WHERE prompt_text IS NOT NULL "
                             "AND prompt_text <> '' GROUP BY prompt_id"))

    def build(gid, focus_keys):
        pid, m, lang, ds = gens[gid]
        e = T.LANG2ECO[lang]
        masked = [(PB.NORM[e](k), sp | {k}) for k, sp in hits[gid].items() if not keep(e, PB.NORM[e](k))]
        rxs = [name_rx(e, sp) for _nm, sp in masked]

        def mask(t):
            for rx in rxs:
                t = rx.sub(MASK, t)
            return "\n".join(mask_tokens(x) for x in t.split("\n"))
        code = r.execute("SELECT generated_code FROM generations WHERE id = ?", (gid,)).fetchone()[0] or ""
        lines = [ln for ln in code.splitlines() if not ln.strip().startswith("```")]   # drop markdown fences
        frx = name_rx(e, set().union(*(hits[gid][k] | {k} for k in focus_keys)))
        idx = next((i for i, ln in enumerate(lines) if frx.search(ln)), None)
        if idx is None:
            return None
        snip = [mask(ln)[:160] for ln in lines[max(0, idx - 1): idx + 2]][:3]
        words = mask(prompts.get(pid, "")).split()[:20]
        return {"pid": pid, "model": LABEL[m], "dataset": ds, "lang": lang, "prompt_words": " ".join(words),
                "snippet": snip, "masked_names": len(masked), "names": len(hits[gid])}

    rng = random.Random(SEED)
    out = {}
    for ds in DATASETS:
        pool = sorted(g for g in halluc_gens if gens[g][3] == ds)
        rng.shuffle(pool)
        for gid in pool:
            e = T.LANG2ECO[gens[gid][2]]
            focus = [k for k in hits[gid] if (e, PB.NORM[e](k)) in st and st[(e, PB.NORM[e](k))][1] in
                     ("H_ABSENT", "H_REGISTERED_LATER") and st[(e, PB.NORM[e](k))][2] is None]
            ex = build(gid, focus) if focus else None
            if ex:
                out[ds] = ex
                break
    # import-only: a v3 hallucination that is a correct import of a differently named distribution (D1/D11);
    # only names whose own PyPI name is taken or closed, so the shown import name is not registrable
    pool = sorted(g for g, nms in d11_gens.items() if any(keep("pypi", nm) for nm in nms))
    rng.shuffle(pool)
    for gid in pool:
        focus = [k for k in hits[gid] if PB.norm_pypi(k) in d11_gens[gid] and keep("pypi", PB.norm_pypi(k))]
        ex = build(gid, focus)
        if ex:
            out["import_only"] = ex
            break
    r.close()
    L = ["# v4 Appendix C examples, masked (PRIVATE — never commit)", "",
         f"Random (seed 42) among generations with ≥ 1 hallucination (as of generation, v4); the import-only example is "
         f"a v3 hallucination that is a correct import of a differently named distribution (D1/D11) whose own name "
         f"is taken or closed. Every name that is not existing or closed is replaced by {MASK}; names in an owned "
         f"npm scope are kept. Check by hand before publishing.", ""]
    for k, x in out.items():
        L += [f"## {k}", "", f"- prompt id: `{x['pid']}`; model: {x['model']}; dataset: {x['dataset']}; "
              f"language: {x['lang']}", f"- names extracted from the response: {x['names']}; withheld: {x['masked_names']}",
              f"- prompt (first 20 words): {x['prompt_words']}", "", "````" + x["lang"], *x["snippet"], "````", ""]
    MASKED = REPO / "data" / "private" / "v4_examples_masked.md"
    MASKED.write_text("\n".join(L))
    # residual check over prompt words and snippets (same token rule as the masking)
    body = "\n".join(x["prompt_words"] + "\n" + "\n".join(x["snippet"]) for x in out.values())
    res = Counter()
    for line in body.split("\n"):
        for m in tok_rx.finditer(line):
            if is_risky(m.group(0)):
                res["english_word_outside_package_lines" if m.group(0).lower().rstrip(".") in dict_words
                    and not pkg_line.search(line) else "other"] += 1
    return {"picked": sorted(out), "residual_risky_tokens": dict(res)}


# ── main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    n = json.loads(NUMBERS.read_text())
    OUT.mkdir(parents=True, exist_ok=True)
    ref = spracklen_gpt4t()
    master = load_master()
    L = ["# Tables and figures for v4", "",
         "Generated by `scripts/v4_tables.py` from `reports/numbers.json` and the private classification "
         "(offline). Aggregates and model names only. v4 = live revalidation (stage A, variant c).", ""]
    l1, j1 = models_table(n)
    L += l1 + [""]
    lc, jc = costs_table()
    L += lc
    L += language_tables(n)
    L += pairs_table(n)
    L += figure2(n, ref)
    L += figure3(n)
    L += dataset_tables(n)
    l7, j7 = table4(n, master)
    L += l7
    L += v3_v4_table(n)
    L += task2_table(n)
    l10, j10 = text_numbers(n)
    L += l10
    L += thinking_check()
    ex = examples(master)
    exm = examples_masked(master)
    (OUT / "tables.md").write_text("\n".join(L))
    (OUT / "v4_numbers.json").write_text(json.dumps({"models": j1, "costs": jc, "table4": j7, "text_numbers": j10,
                                                     "spracklen_gpt4_turbo": ref,
                                                     "examples_private": {"candidates_per_kind": ex["candidates_per_kind"],
                                                                          "picked": ex["picked"]}},
                                                    indent=2, sort_keys=True) + "\n")
    leak_check()
    print(f"masked examples: {exm['picked']}; residual risky tokens: {exm['residual_risky_tokens']}")
    print("wrote reports/v4/tables.md, figure2_rates.png, figure3_jaccard.png, v4_numbers.json; examples: "
          + ", ".join(f"{k} {v}" for k, v in sorted(ex["candidates_per_kind"].items())))


def leak_check() -> None:
    names = set()
    for f in (REPO / "data" / "private" / "prereg").glob("group_*_*.json"):
        names |= set(json.loads(f.read_text()))
    names = {x.lower() for x in names if len(x) >= 4}
    tok = lambda t: set(re.findall(r"[a-z0-9@/._-]+", t.lower()))  # noqa: E731
    static = set().union(*(tok((REPO / "scripts" / f).read_text()) for f in
                           ("v4_tables.py", "task1_analysis.py", "task2_analysis.py", "make_diff_report.py",
                            "recompute_all.py")))
    for p in (OUT / "tables.md", OUT / "v4_numbers.json"):
        hit = sum(1 for x in (names & tok(p.read_text())) if x not in static)
        if hit:
            raise SystemExit(f"leak check: {hit} name-like tokens in {p.name}; inspect privately")


if __name__ == "__main__":
    main()
