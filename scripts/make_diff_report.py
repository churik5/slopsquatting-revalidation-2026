"""v3 (published) -> new: one row per paper number, with the reason. No package names.

    Called by scripts/task1_analysis.py (DIFF.build(numbers), DIFF.write(numbers));
    standalone: python scripts/make_diff_report.py   # re-renders from reports/numbers.json

Published values are inputs, not results. Each one carries the exact text it comes from, and
build() checks every quote against the text of data/private/papers/v3.pdf (pdftotext, both
raw and -layout, whitespace collapsed). A quote that is not found stops the run, so a value
cannot be mistyped silently. New values are read from reports/numbers.json only.

Writes reports/diff_v3_vs_new.md.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PDF = REPO / "data" / "private" / "papers" / "v3.pdf"
MODELS = ["haiku-4-5", "sonnet-4-6", "gemini-2.5-pro", "deepseek-v3.2", "gpt-5.4-mini"]
LABEL = {"haiku-4-5": "Claude Haiku 4.5", "sonnet-4-6": "Claude Sonnet 4.6", "gemini-2.5-pro": "Gemini 2.5 Pro",
         "deepseek-v3.2": "DeepSeek V3.2", "gpt-5.4-mini": "GPT-5.4-mini"}

# ── published v3 values: (value, quote that must occur in v3.pdf) ─────────────
V3 = {
    "generations": (199845, "a total of 199,845"),
    "prompts_per_model": (39969, "approximately 39,969 per model"),
    "appx_b": ({"anthropic": 79933, "openai": 39969, "google": 39968, "deepseek": 39975},
               "79,933 $375.26"),
    "appx_b_google": (39968, "gemini-2.5-pro 39,968"),
    "appx_b_deepseek": (39975, "39,975 $11.11"),
    "rate": ({"haiku-4-5": 4.62, "sonnet-4-6": 5.41, "gemini-2.5-pro": 5.80, "deepseek-v3.2": 5.89,
              "gpt-5.4-mini": 6.10}, "Claude Haiku 4.5 5.49 2.76 +2.73 4.62"),
    "rate_quotes": ["Claude Sonnet 4.6 6.63 2.62 +4.01 5.41", "Gemini 2.5 Pro 6.75 3.61 +3.14 5.80",
                    "DeepSeek V3.2 6.69 3.78 +2.91 5.89", "GPT-5.4-mini 7.27 3.14 +4.13 6.10"],
    "by_language": ({"haiku-4-5": (5.49, 2.76), "sonnet-4-6": (6.63, 2.62), "gemini-2.5-pro": (6.75, 3.61),
                     "deepseek-v3.2": (6.69, 3.78), "gpt-5.4-mini": (7.27, 3.14)}, "Python JavaScript"),
    "range": ((4.62, 6.10), "we measure 4.62%–6.10%"),
    "spread_pp": (1.48, "(1.48"),
    "chi2_significant": (5, "5 are significant after Holm–"),
    "chi2_max": (52.49, "χ² = 52.49"),
    "smallest_pair_delta": (0.08, "DeepSeek V3.2 vs. Gemini 2.5 Pro (Δ = 0.08 percentage points)"),
    "jaccard": ((0.222, 0.179, 0.343), "mean Jaccard is 0.222 (range 0.179–0.343)"),
    "universal": ((127, 109, 18), "127 names (109 PyPI, 18 npm)"),
    "registrable": ((53, 41, 12), "53 (41 PyPI, 12 npm)"),
    "refusals": ({"sonnet-4-6": 18.79, "haiku-4-5": 1.27, "gpt-5.4-mini": 32.14, "gemini-2.5-pro": 8.14,
                  "deepseek-v3.2": 2.08}, "Claude Sonnet 4.6 claude-sonnet-4-6 default 18.79%"),
    "refusal_quotes": ["claude-haiku-4-5-20251001 default 1.27%", "minimal 32.14%", "gemini-2.5-pro default 8.14%",
                       "deepseek-chat (alias; see §8) default 2.08%"],
    "mentions_per_model": (1300, "more than 1,300 hallucinated mentions per model"),
    "llm_vs_so": ("LLM-synthesized > Stack Overflow",
                  "LLM-synthesized prompts produce higher hallucination rates than curated Stack Overflow prompts"),
}


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", t.replace("−", "-"))


def pdf_check() -> str:
    texts = []
    for args in (["pdftotext", "-enc", "UTF-8"], ["pdftotext", "-enc", "UTF-8", "-layout"]):
        raw = subprocess.run(args + [str(PDF), "-"], capture_output=True, text=True, check=True).stdout
        texts.append(_norm(re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "-", raw)))
    quotes = [v[1] for k, v in V3.items() if isinstance(v, tuple)] + V3["rate_quotes"] + V3["refusal_quotes"]
    missing = [q for q in quotes if not any(_norm(q) in t for t in texts)]
    if missing:
        raise SystemExit(f"{len(missing)} v3 quote(s) not found in v3.pdf: {missing}")
    return f"{len(quotes)} quotes found"


def pct(x: float) -> str:
    return f"{x:.2f}%"


def reason_model(n: dict, m: str) -> str:
    d = n["decomposition_v3_to_c"]["mention"][m]
    a = n["recompute"]["a_mention"]["per_model"][m]
    parts = []
    s = d.get("v3_halluc_now_stdlib_or_builtin", 0)
    if s:
        parts.append(f"−{s} stdlib/built-in")
    for k, lab in (("v3_halluc_now_D15_noise", "D15"),
                   ("v3_halluc_now_D1_D11_pipreqs_import_only", "D1/D11"),
                   ("v3_halluc_now_FALSE_POSITIVE", "existed before generation")):
        if d.get(k):
            parts.append(f"−{d[k]} {lab}")
    unk = sum(v for k, v in d.items() if k.startswith("v3_halluc_now_UNKNOWN"))
    if unk:
        parts.append(f"−{unk} UNKNOWN")
    if d.get("v3_halluc_now_v3_existing"):
        parts.append(f"−{d['v3_halluc_now_v3_existing']} spelling variant of an existing name (PEP 503)")
    if d.get("v3_halluc_now_noise_filter"):
        parts.append(f"−{d['v3_halluc_now_noise_filter']} noise filter on the normalized name")
    known = ("UNKNOWN", "FALSE_POSITIVE", "D1_D11_pipreqs_import_only", "D15_noise", "stdlib_or_builtin",
             "v3_existing", "noise_filter")
    other = sum(v for k, v in d.items() if k.startswith("v3_halluc_now_") and
                not k[len("v3_halluc_now_"):].startswith(known))
    if other:
        parts.append(f"−{other} other")
    if d.get("new_halluc_not_in_v3"):
        parts.append(f"+{d['new_halluc_not_in_v3']} new")
    c = n["recompute"]["c_mention"]["per_model"][m]
    dn = a["total"] - c["total"]
    tail = f"; denominator −{dn} (D15 noise)" if dn else ""
    return (f"hallucinated mentions {a['halluc']} → {c['halluc']}: " + ", ".join(parts) + tail
            + ". Published vs (a): later noise-filter revision")


def build(n: dict) -> dict:
    check = pdf_check()
    rc, rg, ra = n["recompute"]["c_mention"], n["recompute"]["c_generation"], n["recompute"]["a_mention"]
    rows = []
    g = n["generations_table"]
    miss = {m: k for m, k in g["missing_per_model"].items() if k}
    rows.append({"item": "Generations", "v3": f"{V3['generations'][0]:,}", "repro": f"{g['rows']:,} rows",
                 "new": f"{g['rows']:,} (stored rows, D6)",
                 "reason": f"{g['distinct_prompts']:,} prompts × 5 = {g['prompts_x_models']:,}; rows missing in the DB: "
                           f"{miss}. Appendix B per-provider counts (Google {V3['appx_b_google'][0]:,}, DeepSeek "
                           f"{V3['appx_b_deepseek'][0]:,}) differ from the DB ({g['per_model']['gemini-2.5-pro']:,}, "
                           f"{g['per_model']['deepseek-v3.2']:,}) and are not reproducible from it"})
    for m in MODELS:
        p = V3["rate"][0][m]
        a, c, cg = ra["per_model"][m], rc["per_model"][m], rg["per_model"][m]
        rows.append({"item": f"Rate {LABEL[m]}", "v3": pct(p), "repro": f"{pct(a['rate'])} ({a['halluc']}/{a['total']})",
                     "new": f"mention {pct(c['rate'])} [{c['ci95'][0]:.2f}–{c['ci95'][1]:.2f}]; generation "
                            f"{pct(cg['rate'])} [{cg['ci95'][0]:.2f}–{cg['ci95'][1]:.2f}]",
                     "reason": reason_model(n, m)})
    for lvl, r in (("mention", rc), ("generation", rg)):
        rates = {m: r["per_model"][m]["rate"] for m in MODELS}
        lo, hi = min(rates, key=rates.get), max(rates, key=rates.get)
        ra_l = n["recompute"][f"a_{lvl}"]["per_model"]
        repro = (f"{min(ra_l[m]['rate'] for m in MODELS):.2f}–{max(ra_l[m]['rate'] for m in MODELS):.2f}%"
                 + (" (generation level)" if lvl == "generation" else ""))
        rows.append({"item": f"Range ({lvl})",
                     "v3": f"{V3['range'][0][0]:.2f}–{V3['range'][0][1]:.2f}%" if lvl == "mention"
                     else "— (the paper reports the mention level only)",
                     "repro": repro,
                     "new": f"{rates[lo]:.2f}% ({lo}) – {rates[hi]:.2f}% ({hi}); spread {rates[hi] - rates[lo]:.2f} pp",
                     "reason": (f"the exact published values {V3['range'][0][0]:.2f}–{V3['range'][0][1]:.2f}% are not "
                                f"reproduced from the DB (v3 method gives {repro}; later noise-filter revision); "
                                "new values: per-model changes above") if lvl == "mention" else
                     "PREREG §3.1 unit (generation with ≥1 hallucination; denominator includes refusals and "
                     "generations without any package)"})
    for lvl, r in (("mention", rc), ("generation", rg)):
        hg = next(x for x in r["chi2"]["pairs"] if {x["a"], x["b"]} == {"haiku-4-5", "gpt-5.4-mini"})
        rows.append({"item": f"χ² + Holm ({lvl})",
                     "v3": f"{V3['chi2_significant'][0]}/10 significant; Haiku vs GPT χ² {V3['chi2_max'][0]}",
                     "repro": f"{ra['chi2']['n_significant']}/10" if lvl == "mention" else "",
                     "new": f"{r['chi2']['n_significant']}/10 significant; Haiku vs GPT χ² {hg['chi2']:.2f}, "
                            f"Δ {hg['delta_pp']:.2f} pp",
                     "reason": "recomputed from the new per-model counts"})
        perm = r["permutation"]
        if perm is None:
            continue
        agree = sum(1 for x, y in zip(r["chi2"]["pairs"], perm["pairs"]) if x["significant"] == (y["p_perm"] < 0.05))
        rows.append({"item": f"Permutation test ({lvl})", "v3": "agrees with χ² on all significant pairs",
                     "repro": "", "new": f"{perm['n_significant']}/10 with p<0.05; agrees with χ²+Holm on {agree}/10 pairs",
                     "reason": "10 000 resamples, seed 42"})
        dg = next(x for x in r["chi2"]["pairs"] if {x["a"], x["b"]} == {"deepseek-v3.2", "gemini-2.5-pro"})
        rows.append({"item": f"DeepSeek vs Gemini Δ ({lvl})", "v3": f"{V3['smallest_pair_delta'][0]:.2f} pp (ns)",
                     "repro": "", "new": f"{dg['delta_pp']:.2f} pp ({'significant' if dg['significant'] else 'ns'})",
                     "reason": "recomputed"})
    j, ja = rc["jaccard"], ra["jaccard"]
    pj = {(x["a"], x["b"]): x["jaccard"] for x in j["pairs"]}
    pick = lambda a, b: pj.get((a, b), pj.get((b, a)))  # noqa: E731
    rows.append({"item": "Jaccard mean (range)", "v3": "{:.3f} ({:.3f}–{:.3f})".format(*V3["jaccard"][0]),
                 "repro": f"{ja['mean']:.3f} ({ja['min']:.3f}–{ja['max']:.3f})",
                 "new": f"{j['mean']:.3f} ({j['min']:.3f}–{j['max']:.3f}); DeepSeek–GPT "
                        f"{pick('deepseek-v3.2', 'gpt-5.4-mini'):.3f}; Haiku–Sonnet {pick('haiku-4-5', 'sonnet-4-6'):.3f}",
                 "reason": "sets of names hallucinated as of generation (normalized names) instead of v3 raw keys"})
    for m in MODELS:
        py, js = V3["by_language"][0][m]
        c = rc["by_model_language"][m]
        a = ra["by_model_language"][m]
        rows.append({"item": f"Python / JS {LABEL[m]}", "v3": f"{py:.2f} / {js:.2f}",
                     "repro": f"{a['python']['rate']:.2f} / {a['javascript']['rate']:.2f}",
                     "new": f"mention {c['python']['rate']:.2f} / {c['javascript']['rate']:.2f}; generation "
                            f"{rg['by_model_language'][m]['python']['rate']:.2f} / "
                            f"{rg['by_model_language'][m]['javascript']['rate']:.2f}",
                     "reason": "per-language share of the changes above"})
    ds = rc["by_dataset"]
    llm = min(ds["LLM_AT"]["rate"], ds["LLM_LY"]["rate"]) > max(ds["SO_AT"]["rate"], ds["SO_LY"]["rate"])
    rows.append({"item": "LLM vs SO prompts", "v3": V3["llm_vs_so"][0], "repro": "",
                 "new": "; ".join(f"{k} {v['rate']:.2f}%" for k, v in ds.items()) +
                        (" (holds)" if llm else " (does not hold)"), "reason": "recomputed (mention level)"})
    u = n["universal"]
    v3a = n["recompute"]["a_mention"]["universal"]
    rows.append({"item": "Universal set, PyPI (109/121)", "v3": f"{V3['universal'][0][1]}",
                 "repro": f"{u['v3_april_lists']['pypi']} in DB/April list; {v3a['n_pypi']} with the current noise filter",
                 "new": f"{u['new_c']['pypi']}", "reason": "fate of each April-list name: " + json.dumps(
                     u["april_list_fate"]["pypi"], sort_keys=True)})
    rows.append({"item": "Universal set, npm", "v3": f"{V3['universal'][0][2]}",
                 "repro": f"{u['v3_april_lists']['npm']}; {v3a['n_npm']} with the current noise filter",
                 "new": f"{u['new_c']['npm']}",
                 "reason": "fate of each April-list name: " + json.dumps(u["april_list_fate"]["npm"], sort_keys=True)})
    rows.append({"item": "Universal set, total (127/139)", "v3": f"{V3['universal'][0][0]}",
                 "repro": f"{sum(u['v3_april_lists'].values())}; {v3a['total']} with the current noise filter",
                 "new": f"{u['new_c_total']}",
                 "reason": "109 is not reproducible (the DB and the April list give 121); see the two rows above"})
    rows.append({"item": "Registrable (53 = 41 + 12)", "v3": "{} ({} PyPI, {} npm)".format(*V3["registrable"][0]),
                 "repro": "", "new": "not recomputed",
                 "reason": "registrability is a registry decision (PREREG §2: BLOCKED_BY_REGISTRY only when the registry "
                           "says so); the April PyPI reply gives a count, not names; HTTP 404 does not mean registrable"})
    for m in MODELS:
        r = rc["refusals"][m]
        rows.append({"item": f"Refusals {LABEL[m]}", "v3": pct(V3["refusals"][0][m]),
                     "repro": f"{r['rate']:.2f}% ({r['refusals']}/{r['generations']})", "new": f"{r['rate']:.2f}%",
                     "reason": "refusals do not depend on the classification; difference = refusal definition in the "
                               "v3 pipeline vs generated_code NULL/'none'/empty"})
    lo = min(rc["per_model"][m]["halluc"] for m in MODELS)
    rows.append({"item": "Hallucinated mentions per model", "v3": f"more than {V3['mentions_per_model'][0]:,}",
                 "repro": f"min {min(ra['per_model'][m]['halluc'] for m in MODELS):,}", "new": f"min {lo:,}",
                 "reason": "per-model changes above"})
    return {"pdf_check": check, "rows": rows}


def write(n: dict) -> None:
    L = ["# v3 → new (task 1, stage A)", "", "Generated by `scripts/make_diff_report.py` from `reports/numbers.json`. "
         "v3 = published value (checked against v3.pdf); (a) = v3 method on the DB; new = live revalidation "
         "(variant c). No package names.", "", "| Number | v3 | (a) | New | Reason |", "|---|---|---|---|---|"]
    for r in n["v3_to_new"]["rows"]:
        L.append(f"| {r['item']} | {r['v3']} | {r.get('repro', '')} | {r['new']} | {r['reason']} |")
    L += ["", n["v3_to_new"]["pdf_check"], ""]
    (REPO / "reports" / "diff_v3_vs_new.md").write_text("\n".join(L) + "\n")


if __name__ == "__main__":
    nums = json.loads((REPO / "reports" / "numbers.json").read_text())
    nums["v3_to_new"] = build(nums)
    write(nums)
    print("wrote reports/diff_v3_vs_new.md", file=sys.stderr)
