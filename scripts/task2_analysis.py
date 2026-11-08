"""Task 2, stage B: registration dynamics (PREREG §4, PREREG-deviations D0-D18). Offline.

    .venv/bin/python scripts/task2_analysis.py

Run after scripts/task1_analysis.py and scripts/npm_placeholders.py (it adds numbers.json["task2"]).
Inputs (read-only): data/private/classification.db, data/private/registry_checks.db, data/runs.db,
data/private/a1_final.txt, data/private/arxiv/abs_2605.17062.html (D18), data/private/recompute/,
reports/prereg_baseline.json (frozen strata cut points), reports/t0.json.

Groups: retained names (after all stage A exclusions) that were hallucinations as of generation
(ABSENT now, or registered after generation). A = new universal set; A1 = A ∩ a1_final;
A2 = A \\ A1; B = the rest; B4 = B names hallucinated by exactly 4 models.
Outcome: registered = EXISTS / SECURITY_HOLDING / UNPUBLISHED created on or after generation (D13);
primary window = [exposure start, T0]; registrations between generation and exposure start are
reported separately (pre-publication background).
Primary test: two-sided Fisher exact, A vs B4 inside common strata (frozen tertile cuts).

Outputs: reports/task2_report.md, reports/numbers.json -> "task2". No package names.
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
import html  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import re  # noqa: E402
import sqlite3  # noqa: E402
from collections import Counter, defaultdict  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from statistics import median  # noqa: E402

from scipy.stats import fisher_exact  # noqa: E402
from statsmodels.stats.contingency_tables import StratifiedTable  # noqa: E402

import task1_analysis as T  # noqa: E402

PRIVATE = REPO / "data" / "private"
CLS_DB = PRIVATE / "classification.db"
RC_DB = PRIVATE / "registry_checks.db"
RUNS_DB = REPO / "data" / "runs.db"
A1_FILE = PRIVATE / "a1_final.txt"
ARXIV = PRIVATE / "arxiv" / "abs_2605.17062.html"
UNIVERSAL = PRIVATE / "recompute" / "universal_c_mention.json"
REPORTS = REPO / "reports"
NUMBERS = REPORTS / "numbers.json"
OUT = REPORTS / "task2_report.md"
ECOS = ("pypi", "npm")
EXPOSURE_LIST = datetime(2026, 4, 30, tzinfo=timezone.utc)      # A2 and B (PREREG §4.2)
Z = 1.959963984540054
H = ("H_ABSENT", "H_REGISTERED_LATER")
REG_CATS = ("EXISTS", "SECURITY_HOLDING", "UNPUBLISHED")


# ── statistics ────────────────────────────────────────────────────────────────
def wilson(k: int, n: int) -> dict:
    if n == 0:
        return {"k": k, "n": n, "share": None, "ci95": [None, None]}
    p = k / n
    d = 1 + Z * Z / n
    c = (p + Z * Z / (2 * n)) / d
    m = Z * math.sqrt((p * (1 - p) + Z * Z / (4 * n)) / n) / d
    return {"k": k, "n": n, "share": p, "ci95": [max(0.0, c - m), min(1.0, c + m)]}


def newcombe(w1: dict, w2: dict) -> dict:
    """Newcombe (1998) method 10: difference p1 - p2 with hybrid Wilson score interval."""
    if not w1["n"] or not w2["n"]:
        return {"diff": None, "ci95": [None, None]}
    p1, (l1, u1), p2, (l2, u2) = w1["share"], w1["ci95"], w2["share"], w2["ci95"]
    d = p1 - p2
    return {"diff": d, "ci95": [d - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2),
                                d + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)]}


def compare(xa: list[int], xb: list[int], label_a: str = "A", label_b: str = "B4") -> dict:
    """Fisher exact (two-sided) on registered / not, with Wilson shares and a Newcombe difference."""
    ka, na, kb, nb = sum(xa), len(xa), sum(xb), len(xb)
    wa, wb = wilson(ka, na), wilson(kb, nb)
    out = {label_a: wa, label_b: wb, "difference": newcombe(wa, wb)}
    if na and nb:
        odds, p = fisher_exact([[ka, na - ka], [kb, nb - kb]], alternative="two-sided")
        out["fisher"] = {"p": float(p), "odds_ratio": None if math.isinf(odds) or math.isnan(odds) else float(odds)}
    else:
        out["fisher"] = None
    return out


def cmh(tables: list[list[list[int]]]) -> dict:
    """Cochran-Mantel-Haenszel test of a common odds ratio of 1 (strata with both groups only)."""
    tabs = [t for t in tables if sum(t[0]) and sum(t[1])]
    k = sum(t[0][0] + t[1][0] for t in tabs)
    if not tabs or k == 0 or k == sum(sum(t[0]) + sum(t[1]) for t in tabs):
        return {"strata": len(tabs), "registered": k, "computable": False,
                "note": "no variation in the outcome inside the strata"}
    st = StratifiedTable([[[a, b], [c, d]] for (a, b), (c, d) in tabs])
    r = st.test_null_odds(correction=True)
    try:
        or_mh = float(st.oddsratio_pooled)
    except (ZeroDivisionError, FloatingPointError):
        or_mh = None
    return {"strata": len(tabs), "registered": k, "computable": True, "statistic": float(r.statistic),
            "p": float(r.pvalue), "odds_ratio_mh": None if or_mh is None or math.isinf(or_mh) else or_mh}


# ── inputs ────────────────────────────────────────────────────────────────────
def arxiv_versions() -> dict[str, datetime]:
    t = ARXIV.read_text(encoding="utf-8", errors="replace")
    t = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t[t.find("Submission history"):][:4000])))
    out = {}
    for v, s in re.findall(r"\[(v\d+)\]\s+\w{3}, (\d{1,2} \w{3} \d{4} \d{2}:\d{2}:\d{2}) UTC", t):
        out[v] = datetime.strptime(s, "%d %b %Y %H:%M:%S").replace(tzinfo=timezone.utc)
    if not {"v1", "v2"} <= set(out):
        raise SystemExit("arXiv submission history not parsed")
    return out


def load_names() -> dict:
    """(eco, name) -> record for retained live-checked names."""
    c = sqlite3.connect(f"file:{CLS_DB}?mode=ro", uri=True)
    out = {}
    for e, n, freq, cat, created, asof, mal in c.execute(
            "SELECT ecosystem, name, freq, category, created_utc, asof_status, osv_mal FROM names "
            "WHERE checked = 1 AND exclusion IS NULL AND v3_candidate = 1"):
        out[(e, n)] = {"freq": freq, "cat": cat, "created": T.ts(created), "asof": asof, "mal": bool(mal)}
    # models whose kept mentions name it (status is uniform across a name's mentions in stage A)
    r = sqlite3.connect(f"file:{RUNS_DB}?mode=ro", uri=True)
    model_of = dict(r.execute("SELECT id, model_key FROM generations"))
    gens = T.load_generations(r.cursor())
    r.close()
    models = defaultdict(set)
    first: dict = {}
    for e, n, gid in c.execute("SELECT ecosystem, name_c, generation_id FROM mentions WHERE keep_c = 1"):
        if (e, n) in out:
            models[(e, n)].add(model_of[gid])
            lo, hi = gens[gid][3], gens[gid][4]
            if hi is not None and ((e, n) not in first or (hi, lo) < (first[(e, n)][1], first[(e, n)][0])):
                first[(e, n)] = (lo, hi)
    c.close()
    for k, v in out.items():
        v["n_models"] = len(models[k])
        v["gen"] = first.get(k)
    return out


def registry_docs() -> dict:
    """(eco, name) -> latest round 1-2 registry metadata (npm document / PyPI JSON) and OSV MAL id years."""
    rc = sqlite3.connect(f"file:{RC_DB}?mode=ro", uri=True)
    out: dict = defaultdict(dict)
    for e, n, m, s, mj in rc.execute("SELECT ecosystem, name, method, state, meta_json FROM registry_checks "
                                     "WHERE round IN (1, 2) AND is_final = 1 ORDER BY id"):
        md = json.loads(mj or "{}")
        if m in ("npm_registry", "pypi_json", "pypi_simple") and s == "EXISTS":
            out[(e, n)][m] = md
        if m == "osv" and s == "MAL":
            ids = md.get("all_page_vuln_ids") or md.get("vuln_ids") or []
            out[(e, n)]["mal_years"] = sorted({int(x.group(1)) for i in ids
                                               for x in [re.match(r"MAL-(\d{4})-", str(i))] if x})
    meta = {}
    for n, mj in rc.execute("SELECT name, meta_json FROM registry_checks WHERE round = 'meta' AND "
                            "method = 'npm_registry' AND is_final = 1 AND http_status IS NOT NULL ORDER BY id"):
        meta[n] = json.loads(mj or "{}")
    rc.close()
    return out, meta


# ── strata ────────────────────────────────────────────────────────────────────
def stratum(e: str, n: str, rec: dict, cuts: dict) -> str:
    f = PB.tertile(rec["freq"], cuts[e]["freq_cuts"]) + 1
    ln = PB.tertile(len(n), cuts[e]["len_cuts"]) + 1
    return f"{e}.f{f}_l{ln}"


# ── outcome ───────────────────────────────────────────────────────────────────
def outcome(rec: dict, start: datetime, t0: datetime) -> str:
    """registered_in_window | background_before_exposure | after_T0 | not_registered."""
    if rec["asof"] != "H_REGISTERED_LATER" or rec["cat"] not in REG_CATS:
        return "not_registered"
    c = rec["created"]
    if c > t0:
        return "after_T0"
    return "registered_in_window" if c >= start else "background_before_exposure"


def reg_category(rec: dict, docs: dict, meta: dict, e: str, n: str) -> str:
    """Category of a registered name from registry metadata only."""
    if rec["cat"] == "SECURITY_HOLDING":
        m = meta.get(n) or {}
        desc = (m.get("description") or "").strip().lower()
        if (desc or m.get("readme")) and desc != "security holding package":
            return "empty placeholder (version 0.0.1-security, publisher's own text)"
        return "security holding"
    if rec["cat"] == "UNPUBLISHED":
        return "unpublished"
    if rec["cat"] == "REMOVED_MALICIOUS":
        return f"removed malicious (MAL record year {','.join(map(str, docs.get('mal_years', []))) or '?'})"
    if rec["cat"] == "EXISTS_SIMPLE_ONLY":
        return "empty placeholder (PyPI project without files)"
    if rec["cat"] == "EXISTS":
        if e == "pypi":
            nf = (docs.get("pypi_json") or {}).get("n_files")
            return "published with files (contents not inspected)" if nf else \
                "empty placeholder (no files)" if nf == 0 else "unknown"
        vs = (docs.get("npm_registry") or {}).get("versions")
        return "published with versions (contents not inspected)" if vs else \
            "empty placeholder (no versions)" if vs == [] else "unknown"
    return "unknown"


# ── analysis ──────────────────────────────────────────────────────────────────
def group_of(k: tuple, rec: dict, a1: dict) -> str:
    if rec["n_models"] == 5:
        return "A1" if k in a1 else "A2"
    return "B4" if rec["n_models"] == 4 else "B_other"


def build_groups(names: dict, a1: dict, include) -> dict:
    """include(rec) -> True if the name is a hallucination as of generation in this scenario."""
    return {k: group_of(k, rec, a1) for k, rec in names.items() if include(rec)}


def scenario(names, groups, outcome_of, cuts, label: str, drop=lambda e, n: False) -> dict:
    """Primary-style comparison + sensitivity tables for one scenario."""
    xs = defaultdict(list)
    strata: dict = defaultdict(lambda: {"A": [], "B4": []})
    for (e, n), grp in groups.items():
        if drop(e, n):
            continue
        y = 1 if outcome_of(e, n) == "registered_in_window" else 0
        top = "A" if grp in ("A1", "A2") else grp
        xs[(e, grp)].append(y)
        if top in ("A", "B4"):
            strata[stratum(e, n, names[(e, n)], cuts)][top].append(y)
    common = {k: v for k, v in strata.items() if v["A"] and v["B4"]}
    pool = lambda key: [y for k, v in common.items() for y in v[key]]  # noqa: E731
    res = {"label": label,
           "common_strata": len(common),
           "primary": compare(pool("A"), pool("B4")),
           "per_ecosystem": {e: compare([y for k, v in common.items() if k.startswith(e) for y in v["A"]],
                                        [y for k, v in common.items() if k.startswith(e) for y in v["B4"]])
                             for e in ECOS},
           "cmh_ecosystem": cmh([[[sum(y for k, v in common.items() if k.startswith(e) for y in v["A"]),
                                   sum(1 - y for k, v in common.items() if k.startswith(e) for y in v["A"])],
                                  [sum(y for k, v in common.items() if k.startswith(e) for y in v["B4"]),
                                   sum(1 - y for k, v in common.items() if k.startswith(e) for y in v["B4"])]]
                                 for e in ECOS]),
           "cmh_strata": cmh([[[sum(v["A"]), len(v["A"]) - sum(v["A"])], [sum(v["B4"]), len(v["B4"]) - sum(v["B4"])]]
                              for v in common.values()]),
           "balance": {k: {"A": len(v["A"]), "A_registered": sum(v["A"]), "B4": len(v["B4"]),
                           "B4_registered": sum(v["B4"]), "common": k in common}
                       for k, v in sorted(strata.items())},
           "outside_common_strata": {"A": sum(len(v["A"]) for k, v in strata.items() if k not in common),
                                     "B4": sum(len(v["B4"]) for k, v in strata.items() if k not in common),
                                     "A_registered": sum(sum(v["A"]) for k, v in strata.items() if k not in common),
                                     "B4_registered": sum(sum(v["B4"]) for k, v in strata.items() if k not in common)}}
    allA = [y for (e, g), v in xs.items() if g in ("A1", "A2") for y in v]
    allB = [y for (e, g), v in xs.items() if g in ("B4", "B_other") for y in v]
    res["descriptive_A_vs_all_B"] = compare(allA, allB, "A", "B")
    res["descriptive_A1_vs_A2"] = compare([y for (e, g), v in xs.items() if g == "A1" for y in v],
                                          [y for (e, g), v in xs.items() if g == "A2" for y in v], "A1", "A2")
    return res


def main() -> None:
    t0 = T.ts(json.loads((REPORTS / "t0.json").read_text())["t0_utc"])
    base = json.loads((REPORTS / "prereg_baseline.json").read_text())
    cuts = {e: base["strata"][e] for e in ECOS}
    arx = arxiv_versions()
    a1 = {}
    with open(A1_FILE, newline="") as f:
        for r in csv.DictReader(f):
            e = r["ecosystem"]
            a1[(e, PB.NORM[e](r["name"]))] = arx[r["first_version"]]
    universal = json.loads(UNIVERSAL.read_text())
    names = load_names()
    docs, meta = registry_docs()

    # ── primary groups ──
    hall = lambda rec: rec["asof"] in H  # noqa: E731
    groups = build_groups(names, a1, hall)
    uni = {(e, n) for e in ECOS for n in universal[e]}
    a_set = {k for k, g in groups.items() if g in ("A1", "A2")}
    if a_set != uni:
        raise SystemExit("A (5 hallucinating models) differs from the stage A universal set")
    start = {k: (a1[k] if groups.get(k) == "A1" else EXPOSURE_LIST) for k in names}

    def out_primary(e, n):
        return outcome(names[(e, n)], start[(e, n)], t0)

    # composition and exclusions
    comp = {e: Counter(g for (ee, _n), g in groups.items() if ee == e) for e in ECOS}
    unk = {e: Counter(("A_candidate" if rec["n_models"] == 5 else "B") + ":" + rec["asof"]
                      for (ee, _n), rec in names.items() if ee == e and rec["asof"].startswith("UNKNOWN"))
           for e in ECOS}
    no_time = sum(1 for k in groups if names[k]["gen"] is None)
    nums_a = json.loads(NUMBERS.read_text())
    late_flag = nums_a["asof_generation"]["pypi"]["creation_date_possibly_late_flag"]["names"]
    if late_flag:
        raise SystemExit("PyPI names flagged 'creation date may be late' must be excluded (PREREG §4.5); not handled")
    a1_in_a = sum(1 for k in a1 if groups.get(k) == "A1")

    # outcome counts by group (all names of each group)
    oc = {e: {g: Counter(out_primary(ee, n) for (ee, n), gg in groups.items() if ee == e and gg == g)
              for g in ("A1", "A2", "B4", "B_other")} for e in ECOS}

    primary = scenario(names, groups, out_primary, cuts, "primary")

    # ── sensitivity ──
    sens = {}
    sens["D7_without_ember_scope"] = scenario(names, groups, out_primary, cuts, "D7",
                                              drop=lambda e, n: e == "npm" and n.startswith("@ember/")
                                              and groups[(e, n)] in ("A1", "A2"))
    n_ember = sum(1 for (e, n), g in groups.items() if e == "npm" and n.startswith("@ember/") and g in ("A1", "A2"))

    # D14: EXISTS_SIMPLE_ONLY counted as registered (in the window; no creation date)
    simple = {k for k, r in names.items() if r["cat"] == "EXISTS_SIMPLE_ONLY"}
    g14 = build_groups(names, a1, lambda r: r["asof"] in H or r["cat"] == "EXISTS_SIMPLE_ONLY")
    sens["D14_simple_only_registered"] = scenario(
        names, g14, lambda e, n: "registered_in_window" if (e, n) in simple else out_primary(e, n), cuts, "D14")

    # D16: REMOVED_MALICIOUS by MAL record date; only the record year is stored (MAL-YYYY-N) and no
    # new OSV request is allowed: year < generation year -> pre-existing; year = 2026 -> ambiguous (bounds)
    removed = {k for k, r in names.items() if r["cat"] == "REMOVED_MALICIOUS"}
    pre16 = {k for k in removed if docs[k].get("mal_years") and max(docs[k]["mal_years"]) < 2026}
    amb16 = removed - pre16
    sens_d16 = {}
    for bound, amb_reg in (("lower_ambiguous_as_pre_existing", False), ("upper_ambiguous_as_registered_in_window", True)):
        reg16 = amb16 if amb_reg else set()
        g16 = build_groups(names, a1, hall)
        for k in reg16:
            g16[k] = group_of(k, names[k], a1)
        sens_d16[bound] = scenario(names, g16, lambda e, n, _r=reg16: "registered_in_window" if (e, n) in _r
                                   else out_primary(e, n), cuts, f"D16 {bound}")
    sens["D16_removed_malicious"] = {"removed_malicious_names": len(removed),
                                     "mal_record_year_before_2026_pre_existing": len(pre16),
                                     "mal_record_year_2026_ambiguous": len(amb16), **sens_d16}

    # D17: npm UNPUBLISHED whose unpublication precedes generation -> hallucination, not registered
    unpub_before = set()
    for k, r in names.items():
        if r["cat"] == "UNPUBLISHED" and r["gen"]:
            tu = (docs[k].get("npm_registry") or {}).get("time_unpublished")
            tu = T.ts(tu.get("time")) if isinstance(tu, dict) else None
            if tu is not None and tu < r["gen"][0]:
                unpub_before.add(k)
    g17 = build_groups(names, a1, hall)
    for k in unpub_before:
        g17[k] = group_of(k, names[k], a1)
    sens["D17_unpublished_before_generation"] = {
        "names_added_as_hallucinations": len(unpub_before),
        **scenario(names, g17, lambda e, n: "not_registered" if (e, n) in unpub_before else out_primary(e, n),
                   cuts, "D17")}

    # ── MDE on the final sizes (same method as the baseline, seed 42) ──
    na = primary["primary"]["A"]["n"]
    nb = primary["primary"]["B4"]["n"]
    mde = {"pooled": [PB.mde(na, nb, p0) for p0 in PB.BASELINES]}
    for e in ECOS:
        pe = primary["per_ecosystem"][e]
        mde[e] = [PB.mde(pe["A"]["n"], pe["B4"]["n"], p0) for p0 in PB.BASELINES]

    # ── registered names: categories and timing ──
    cats = defaultdict(Counter)
    days = defaultdict(list)
    for (e, n), g in groups.items():
        o = out_primary(e, n)
        top = "A" if g in ("A1", "A2") else "B"
        if o != "not_registered":
            cats[f"{top}.{o}"][reg_category(names[(e, n)], docs.get((e, n), {}), meta, e, n)] += 1
        if o == "registered_in_window":
            days[top].append((names[(e, n)]["created"] - start[(e, n)]).total_seconds() / 86400)
    for k in removed:                      # sensitivity-only category (D16)
        cats["removed_malicious_D16"][reg_category(names[k], docs.get(k, {}), meta, *k)] += 1
    timing = {g: ({"n": len(v), "median_days": median(v), "min_days": min(v), "max_days": max(v)} if v else {"n": 0})
              for g, v in (("A", days["A"]), ("B", days["B"]))}

    res = {"t0_utc": T.iso(t0), "exposure_start": {"A1_v1_submission": T.iso(arx["v1"]),
                                                  "A1_v2_submission": T.iso(arx["v2"]),
                                                  "A2_and_B": T.iso(EXPOSURE_LIST)},
           "groups": {e: {**{g: comp[e][g] for g in ("A1", "A2", "B4", "B_other")},
                          "A": comp[e]["A1"] + comp[e]["A2"], "B": comp[e]["B4"] + comp[e]["B_other"]}
                      for e in ECOS},
           "a1_final_names_in_A": a1_in_a, "a1_final_names": len(a1),
           "excluded": {"unknown_as_of_generation": {e: dict(sorted(unk[e].items())) for e in ECOS},
                        "no_generation_time": no_time, "pypi_creation_date_possibly_late": late_flag},
           "outcomes_by_group": {e: {g: dict(sorted(c.items())) for g, c in oc[e].items()} for e in ECOS},
           "primary": primary, "sensitivity": sens, "ember_names_in_A": n_ember,
           "mde_final_sizes": mde, "registered_categories": {k: dict(sorted(v.items())) for k, v in sorted(cats.items())},
           "exposure_to_creation_days": timing,
           "frozen_strata_cuts": {e: {"freq": cuts[e]["freq_cuts"], "len": cuts[e]["len_cuts"]} for e in ECOS},
           "baseline_common_strata_D3": 13}
    nums = json.loads(NUMBERS.read_text())
    nums["task2"] = res
    NUMBERS.write_text(json.dumps(nums, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    OUT.write_text(render(res))
    leak_check()
    print("wrote reports/task2_report.md and numbers.json['task2']")


# ── report ────────────────────────────────────────────────────────────────────
def pc(x) -> str:
    return "—" if x is None else f"{100 * x:.1f}%"


def share(w: dict) -> str:
    return f"{w['k']}/{w['n']} = {pc(w['share'])} [{pc(w['ci95'][0])}–{pc(w['ci95'][1])}]" if w["n"] else "0/0"


def cmp_line(c: dict, a="A", b="B4") -> str:
    f = c["fisher"]
    d = c["difference"]
    fs = "—" if f is None else f"p = {f['p']:.3g}" + (f", OR {f['odds_ratio']:.2f}" if f["odds_ratio"] is not None else "")
    ds = "—" if d["diff"] is None else f"{100 * d['diff']:+.1f} pp [{100 * d['ci95'][0]:+.1f}, {100 * d['ci95'][1]:+.1f}]"
    return f"| {share(c[a])} | {share(c[b])} | {ds} | {fs} |"


def cmh_line(c: dict) -> str:
    if not c["computable"]:
        return f"{c['strata']} strata, {c['registered']} registered: not computable ({c['note']})"
    orr = "—" if c["odds_ratio_mh"] is None else f"{c['odds_ratio_mh']:.2f}"
    return f"{c['strata']} strata: χ² = {c['statistic']:.2f}, p = {c['p']:.3g}, OR_MH = {orr}"


def fd(d: dict) -> str:
    return ", ".join(f"{k} {v}" for k, v in d.items()) if d else "none"


def render(r: dict) -> str:
    L = ["# Task 2, stage B: registration dynamics (PREREG §4, D0–D18)", "",
         "Generated by `scripts/task2_analysis.py` (offline, databases read-only, seed 42). Counts only; no "
         "package names; no registrant data.", "",
         "## Groups", "",
         "Retained names (after all stage A exclusions) that were hallucinations as of generation (ABSENT, or "
         "registered after generation). A = universal set after revalidation (5 models); A1 = A ∩ printed in "
         "the paper; A2 = A \\ A1; B = the rest; B4 = B with exactly 4 models.", "",
         "| Ecosystem | A | A1 | A2 | B | B4 | B (other) |", "|---|---:|---:|---:|---:|---:|---:|"]
    for e in ECOS:
        g = r["groups"][e]
        L.append(f"| {e} | {g['A']} | {g['A1']} | {g['A2']} | {g['B']} | {g['B4']} | {g['B_other']} |")
    ex = r["excluded"]
    L += ["", f"A1: {r['a1_final_names_in_A']} of the {r['a1_final_names']} frozen A1 names are in the final A.",
          f"Excluded as UNKNOWN as of generation (by would-be group and status): pypi {fd(ex['unknown_as_of_generation']['pypi'])}; "
          f"npm {fd(ex['unknown_as_of_generation']['npm'])}.",
          f"Excluded for no generation time: {ex['no_generation_time']}; PyPI creation date possibly late: "
          f"{ex['pypi_creation_date_possibly_late']}.", "",
          "Exposure start: A1 = arXiv submission of the first version that prints the name (D18): v1 "
          f"{r['exposure_start']['A1_v1_submission'][:19]}Z, v2 {r['exposure_start']['A1_v2_submission'][:19]}Z; "
          f"A2 and B = {r['exposure_start']['A2_and_B'][:10]}. T0 = {r['t0_utc'][:19]}Z.", "",
          "## Outcomes by group (all names)", "",
          "| Ecosystem | Group | Registered in window | Before exposure (background) | After T0 | Not registered |",
          "|---|---|---:|---:|---:|---:|"]
    for e in ECOS:
        for g in ("A1", "A2", "B4", "B_other"):
            c = r["outcomes_by_group"][e][g]
            L.append(f"| {e} | {g} | {c.get('registered_in_window', 0)} | {c.get('background_before_exposure', 0)} | "
                     f"{c.get('after_T0', 0)} | {c.get('not_registered', 0)} |")
    p = r["primary"]
    L += ["", "## Primary test: A vs B4 inside common strata", "",
          f"Two-sided Fisher exact test; shares with 95% Wilson intervals; difference with 95% Newcombe interval. "
          f"Strata = ecosystem × frequency tertile × length tertile with the cut points frozen in "
          f"`reports/prereg_baseline.json` (before T0). Common strata now: {p['common_strata']} "
          f"(baseline: {r['baseline_common_strata_D3']}). Outside common strata: A {p['outside_common_strata']['A']} "
          f"names ({p['outside_common_strata']['A_registered']} registered in the window), B4 "
          f"{p['outside_common_strata']['B4']} ({p['outside_common_strata']['B4_registered']} registered); these are "
          "not part of the primary test (see the balance table).", "",
          "| Comparison | A registered | B4 registered | Difference A − B4 | Fisher |", "|---|---|---|---|---|",
          "| **Primary (pooled)** " + cmp_line(p["primary"])]
    for e in ECOS:
        L.append(f"| {e} " + cmp_line(p["per_ecosystem"][e]))
    L += ["", f"- CMH by ecosystem: {cmh_line(p['cmh_ecosystem'])}",
          f"- CMH by common strata (D3): {cmh_line(p['cmh_strata'])}", "",
          "Minimum detectable effect on the final sizes (Fisher two-sided, α 0.05, power 0.8, 20 000 simulations, "
          "seed 42, same method as the baseline):", "",
          "| Scope | n A | n B4 | Baseline p0 | Min. share in A | Difference |", "|---|---:|---:|---:|---:|---:|"]
    for sc, rows in r["mde_final_sizes"].items():
        for x in rows:
            L.append(f"| {sc} | {x['n_a']} | {x['n_b']} | {x['p0']} | "
                     + ("— | — |" if x.get("mde_share_a") is None else f"{x['mde_share_a']:.3f} | {x['mde_abs_diff']:.3f} |"))
    f = p["primary"]["fisher"]
    verdict = ("**No effect detected at this precision** (see the minimum detectable effect above)."
               if f is None or f["p"] >= 0.05 else
               "**The difference is significant at α = 0.05** (two-sided).")
    L += ["", verdict, "", "### Balance table (D3)", "",
          "| Stratum | A | A registered | B4 | B4 registered | Common |", "|---|---:|---:|---:|---:|:--:|"]
    for k, v in p["balance"].items():
        L.append(f"| {k} | {v['A']} | {v['A_registered']} | {v['B4']} | {v['B4_registered']} | {'yes' if v['common'] else 'no'} |")
    L += ["", "## Sensitivity analyses", "",
          "| Scenario | A registered | B4 registered | Difference A − B4 | Fisher |", "|---|---|---|---|---|",
          "| Primary " + cmp_line(p["primary"])]
    s = r["sensitivity"]
    L.append(f"| D7: without the {r['ember_names_in_A']} @ember/* names " + cmp_line(s["D7_without_ember_scope"]["primary"]))
    L.append("| D14: EXISTS_SIMPLE_ONLY = registered " + cmp_line(s["D14_simple_only_registered"]["primary"]))
    d16 = s["D16_removed_malicious"]
    for b in ("lower_ambiguous_as_pre_existing", "upper_ambiguous_as_registered_in_window"):
        L.append(f"| D16 ({b.replace('_', ' ')}) " + cmp_line(d16[b]["primary"]))
    L.append(f"| D17: {s['D17_unpublished_before_generation']['names_added_as_hallucinations']} unpublished-before-generation "
             "names as hallucinations " + cmp_line(s["D17_unpublished_before_generation"]["primary"]))
    L += ["", f"D16 detail: {d16['removed_malicious_names']} REMOVED_MALICIOUS names; the OSV MAL record year (from the "
          f"record id MAL-YYYY-N; full dates were not stored and no new OSV request was allowed) is before 2026 for "
          f"{d16['mal_record_year_before_2026_pre_existing']} (pre-existing) and 2026 for "
          f"{d16['mal_record_year_2026_ambiguous']} (ambiguous: shown as lower and upper bound).", "",
          "CMH in each scenario:", ""]
    for name, sc in (("D7", s["D7_without_ember_scope"]), ("D14", s["D14_simple_only_registered"]),
                     ("D16 lower", d16["lower_ambiguous_as_pre_existing"]),
                     ("D16 upper", d16["upper_ambiguous_as_registered_in_window"]),
                     ("D17", s["D17_unpublished_before_generation"])):
        L.append(f"- {name}: by ecosystem — {cmh_line(sc['cmh_ecosystem'])}; by strata — {cmh_line(sc['cmh_strata'])}")
    L += ["", "## Descriptive (not tests of H1)", "",
          "| Comparison | First group registered | Second group registered | Difference | Fisher |", "|---|---|---|---|---|",
          "| A vs all B " + cmp_line(p["descriptive_A_vs_all_B"], "A", "B"),
          "| A1 vs A2 " + cmp_line(p["descriptive_A1_vs_A2"], "A1", "A2"), "",
          "Time from exposure start to creation (registered in the window):", ""]
    for g, t in r["exposure_to_creation_days"].items():
        L.append(f"- {g}: n = {t['n']}" + (f", median {t['median_days']:.0f} days (min {t['min_days']:.0f}, max "
                                           f"{t['max_days']:.0f})" if t["n"] else ""))
    L += ["", "## Registered names: categories (registry metadata only)", "",
          "No intent is attributed; packages were not downloaded, installed or run; registrants are not recorded. "
          "“Project with code” cannot be told apart from an empty package without reading package contents, so "
          "published packages are reported as “published with files/versions (contents not inspected)”.", "",
          "| Group.outcome | Category | Names |", "|---|---|---:|"]
    for k, v in r["registered_categories"].items():
        for c, n in v.items():
            L.append(f"| {k} | {c} | {n} |")
    L.append("")
    return "\n".join(L)


def leak_check() -> None:
    names = set()
    for f in (PRIVATE / "prereg").glob("group_*_*.json"):
        names |= set(json.loads(f.read_text()))
    names = {x.lower() for x in names if len(x) >= 4}
    tok = lambda t: set(re.findall(r"[a-z0-9@/._-]+", t.lower()))  # noqa: E731
    static = set().union(*(tok((REPO / "scripts" / f).read_text()) for f in
                           ("task2_analysis.py", "task1_analysis.py", "make_diff_report.py", "recompute_all.py",
                            "npm_placeholders.py")))
    for p in (OUT, NUMBERS):
        hit = sum(1 for x in (names & tok(p.read_text())) if x not in static)
        if hit:
            raise SystemExit(f"leak check: {hit} name-like tokens in {p.name}; inspect privately")


if __name__ == "__main__":
    main()
