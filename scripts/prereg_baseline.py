"""Offline pre-registration baseline (PREREG.md, commit 686f648). Runs BEFORE any live check.

    python scripts/prereg_baseline.py

Hard guarantees
  * No network: socket.socket is replaced at start-up; every connect attempt raises.
  * data/runs.db is opened read-only (sqlite URI ?mode=ro); its SHA-256 is taken
    before and after the run and must match.
  * No package name is printed or written to reports/. Name lists go only to
    data/private/prereg/ (git-ignored, blocked by the pre-commit hook).
  * Deterministic: seed 42, sorted iteration, no wall-clock values in outputs.

Outputs: reports/prereg_baseline.json (aggregates + hashes), reports/prereg_baseline.md.
"""
from __future__ import annotations

import socket


class NetworkBlockedError(RuntimeError):
    pass


def _refuse(*_a, **_k):
    raise NetworkBlockedError("network access is disabled in prereg_baseline.py")


class _BlockedSocket(socket.socket):
    def connect(self, *a, **k):
        _refuse()

    def connect_ex(self, *a, **k):
        _refuse()

    def sendto(self, *a, **k):
        _refuse()


def block_network() -> None:
    """Make every outgoing connection attempt from this process fail."""
    socket.socket = _BlockedSocket  # type: ignore[misc]
    socket.create_connection = _refuse  # type: ignore[assignment]
    socket.getaddrinfo = _refuse  # type: ignore[assignment]


if __name__ == "__main__":  # first thing the script does; importing for tests does not block
    block_network()

# ── everything below runs with the network already blocked ───────────────────
import csv  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from collections import defaultdict  # noqa: E402
from pathlib import Path  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

DB = REPO / "data" / "runs.db"
REF = REPO / "data" / "reference"
FP_CSV = REPO / "data" / "master_lists" / "py_false_positive_packages.csv"
PIPREQS = REPO / "data" / "reference" / "pipreqs_mapping.txt"
PAPERS = REPO / "data" / "private" / "papers"
PRIVATE = REPO / "data" / "private" / "prereg"
REPORTS = REPO / "reports"
NOISE_MODULE = "src/slop_bench/noise_filter.py"

MODELS = ["haiku-4-5", "deepseek-v3.2", "sonnet-4-6", "gpt-5.4-mini", "gemini-2.5-pro"]
ECOS = ("pypi", "npm")
LANG2ECO = {"python": "pypi", "javascript": "npm"}
SEED = 42
N_SIM = 20_000
ALPHA = 0.05
POWER = 0.8
BASELINES = (0.01, 0.02, 0.05)
GRID_STEP = 0.005
CONTEXT = 60


# ── normalization ─────────────────────────────────────────────────────────────
_PEP503 = re.compile(r"[-_.]+")


def norm_pypi(name: str) -> str:
    """PEP 503: runs of -, _, . become a single '-', then lowercase."""
    return _PEP503.sub("-", name).lower()


def norm_npm(name: str) -> str:
    """npm: lowercase for comparison; the @scope/ part is kept as is (lowercased)."""
    return name.lower()


NORM = {"pypi": norm_pypi, "npm": norm_npm}


def list_sha256(names) -> str:
    """SHA-256 of the sorted list, one name per line, trailing newline."""
    s = sorted(names)
    return hashlib.sha256(("\n".join(s) + "\n" if s else "").encode()).hexdigest()


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=True).stdout.strip()


# ── exclusion lists ───────────────────────────────────────────────────────────
def load_stdlib() -> tuple[set[str], dict]:
    d = json.loads((REF / "python_stdlib_modules.json").read_text())
    names: set[str] = set()
    meta = {}
    for v in ("3.10", "3.11", "3.12", "3.13"):
        mods = d["versions"][v]["modules"]
        names |= {norm_pypi(m) for m in mods}
        meta[v] = {"n_modules": len(mods), "source": d["versions"][v]["source"]}
    meta["union_normalized"] = len(names)
    return names, meta


def load_node_builtins() -> tuple[set[str], dict]:
    d = json.loads((REF / "node_builtin_modules.json").read_text())
    names: set[str] = set()
    meta = {}
    for v in ("18", "20", "22"):
        mods = d["versions"][v]["modules"]
        names |= {norm_npm(m) for m in mods}
        meta[v] = {"n_modules": len(mods), "source": d["versions"][v]["source"]}
    meta["union"] = len(names)
    return names, meta


def load_pipreqs() -> dict[str, set[str]]:
    """pipreqs mapping: PEP 503-normalized import name -> set of normalized distributions."""
    m: dict[str, set[str]] = defaultdict(set)
    for line in PIPREQS.read_text().splitlines():
        if ":" in line:
            imp, dist = line.split(":", 1)
            m[norm_pypi(imp.strip())].add(norm_pypi(dist.strip()))
    return m


def pipreqs_matches(group: set[str], mapping: dict[str, set[str]]) -> dict:
    """Counts only. A match = the normalized name is an import-name key of the mapping."""
    hit = {n for n in group if n in mapping}
    return {"n_group": len(group), "match_key": len(hit),
            "match_key_dist_differs": sum(1 for n in hit if mapping[n] != {n}),
            "match_key_dist_same": sum(1 for n in hit if mapping[n] == {n})}


def load_fp_imports() -> set[str]:
    with open(FP_CSV, newline="") as f:
        return {norm_pypi(r[1]) for r in csv.reader(f) if len(r) > 1 and r[1]}


# ── data ──────────────────────────────────────────────────────────────────────
def load_db(path: Path) -> dict:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    cur = conn.cursor()
    out: dict = {}
    out["validations"] = cur.execute(
        "SELECT package_norm, registry, exists_in_master, live_status FROM validations").fetchall()
    # generation time: own timestamp for sync calls, batch completion time for batch calls
    gens = cur.execute("""
        WITH a AS (SELECT generation_id, MAX(batch) AS is_batch FROM api_calls
                   WHERE endpoint = 'code_gen' GROUP BY generation_id)
        SELECT g.id, g.model_key, g.language, g.dataset, g.created_at, a.is_batch
        FROM generations g LEFT JOIN a ON a.generation_id = g.id""").fetchall()
    batches = {(m, l, d): c for m, l, d, c in cur.execute(
        "SELECT model_key, language, dataset, completed_at FROM batches")}
    gtime, gsrc = {}, {}
    for gid, m, lang, ds, created, is_batch in gens:
        if is_batch == 0 and created:
            gtime[gid], gsrc[gid] = created[:19], "own"
        elif is_batch == 1 and batches.get((m, lang, ds)):
            gtime[gid], gsrc[gid] = batches[(m, lang, ds)][:19].replace("T", " "), "batch"
        else:
            gtime[gid], gsrc[gid] = None, "none"
    out["gtime"], out["gsrc"] = gtime, gsrc
    out["gen_meta"] = {gid: (m, lang) for gid, m, lang, *_ in gens}
    out["hits"] = cur.execute("""
        SELECT h.generation_id, h.package_norm FROM heuristic_hits h
        JOIN generations g ON g.id = h.generation_id
        JOIN validations v ON v.package_norm = h.package_norm
             AND v.registry = (CASE WHEN g.language='python' THEN 'pypi' ELSE 'npm' END)
    """).fetchall()
    conn.close()
    return out


def build_names(db: dict) -> dict:
    """Per ecosystem: normalized name -> record (raw keys, candidate flag, models, freq, time)."""
    names: dict = {e: {} for e in ECOS}
    for raw, reg, exists, live in db["validations"]:
        n = NORM[reg](raw)
        r = names[reg].setdefault(n, {"raw": set(), "cand_flags": [], "models": set(), "freq": 0,
                                      "first_time": None, "first_src": None})
        r["raw"].add(raw)
        # v3 rule: absent from master list and not confirmed by a live HTTP 200
        r["cand_flags"].append(exists == 0 and live != 200)
    for gid, raw in db["hits"]:
        m, lang = db["gen_meta"][gid]
        eco = LANG2ECO[lang]
        r = names[eco][NORM[eco](raw)]
        r["models"].add(m)
        r["freq"] += 1
        t = db["gtime"][gid]
        if t is not None and (r["first_time"] is None or t < r["first_time"]):
            r["first_time"], r["first_src"] = t, db["gsrc"][gid]
    for e in ECOS:
        for r in names[e].values():
            # a normalized name is a candidate only if every raw spelling is one (conservative)
            r["candidate"] = all(r.pop("cand_flags"))
    return names


# ── exclusions ────────────────────────────────────────────────────────────────
def make_rules(stdlib, node_bi, fp_imports):
    from slop_bench.noise_filter import is_noise

    return [
        ("noise_filter", ECOS, lambda n, r: is_noise(n) or any(is_noise(x) for x in r["raw"])),
        ("python_stdlib_3.10-3.13", ("pypi",), lambda n, r: n in stdlib),
        ("node_builtin_18_20_22_or_node_prefix", ("npm",),
         lambda n, r: n in node_bi or n.startswith("node:") or any(x.lower().startswith("node:") for x in r["raw"])),
        ("py_false_positive_import_names", ("pypi",), lambda n, r: n in fp_imports),
    ]


def apply_exclusions(names: dict, rules, eco: str, subset: set[str]) -> tuple[set[str], dict]:
    """Return kept names and per-rule counts: `matched` (independent) and `removed` (in rule order)."""
    counts, kept = {}, set(subset)
    for rid, ecos, pred in rules:
        if eco not in ecos:
            continue
        hit = {n for n in subset if pred(n, names[eco][n])}
        rem = hit & kept
        kept -= rem
        counts[rid] = {"matched": len(hit), "removed": len(rem)}
    counts["_kept"] = len(kept)
    counts["_input"] = len(subset)
    return kept, counts


# ── strata ────────────────────────────────────────────────────────────────────
def tertile_cuts(values) -> list[float]:
    import numpy as np

    return [float(x) for x in np.quantile(np.asarray(values, dtype=float), [1 / 3, 2 / 3])]


def tertile(v: float, cuts: list[float]) -> int:
    return 0 if v <= cuts[0] else (1 if v <= cuts[1] else 2)


# ── power / MDE ───────────────────────────────────────────────────────────────
def mde(n_a: int, n_b: int, p0: float) -> dict:
    """Smallest share in A (grid step GRID_STEP above p0) with Monte-Carlo power >= POWER
    for a two-sided Fisher exact test at ALPHA, B fixed at p0. Fresh RNG(seed 42) per call."""
    import numpy as np
    from scipy.stats import fisher_exact

    if n_a == 0 or n_b == 0:
        return {"n_a": n_a, "n_b": n_b, "p0": p0, "mde_share_a": None, "note": "empty group"}
    rng = np.random.default_rng(SEED)
    cache: dict = {}

    def pval(xa, xb):
        k = (xa, xb)
        if k not in cache:
            cache[k] = fisher_exact([[xa, n_a - xa], [xb, n_b - xb]], alternative="two-sided")[1]
        return cache[k]

    k0 = round(p0 / GRID_STEP)
    trace = []
    for k in range(k0 + 1, round(1 / GRID_STEP) + 1):
        pa = round(k * GRID_STEP, 6)
        xa = rng.binomial(n_a, pa, N_SIM)
        xb = rng.binomial(n_b, p0, N_SIM)
        pairs, inv = np.unique(np.stack([xa, xb], 1), axis=0, return_inverse=True)
        pv = np.array([pval(int(a), int(b)) for a, b in pairs])[inv.ravel()]
        power = float((pv < ALPHA).mean())
        trace.append((pa, power))
        if power >= POWER:
            return {"n_a": n_a, "n_b": n_b, "p0": p0, "mde_share_a": pa,
                    "mde_abs_diff": round(pa - p0, 6), "mde_ratio": round(pa / p0, 4),
                    "power_at_mde": power,
                    "power_previous_grid_point": trace[-2][1] if len(trace) > 1 else None}
    return {"n_a": n_a, "n_b": n_b, "p0": p0, "mde_share_a": None, "note": "power < 0.8 for all shares <= 1"}


# ── A1/A2 paper search ────────────────────────────────────────────────────────
def pdf_text(path: Path) -> str:
    raw = subprocess.run(["pdftotext", "-enc", "UTF-8", str(path), "-"],
                         capture_output=True, text=True, check=True).stdout
    raw = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "-", raw)   # re-join words split at a line-end hyphen
    return re.sub(r"\s+", " ", raw)


def name_pattern(name: str) -> re.Pattern:
    # separators -, _, . (or none: hyphen lost in extraction) are interchangeable; case-insensitive
    parts = [re.escape(p) for p in re.split(r"[-_.]", name)]
    body = r"[-_.]?".join(parts)
    return re.compile(r"(?<![A-Za-z0-9_@/-])" + body + r"(?!-?[A-Za-z0-9_])", re.IGNORECASE)


def find_in_papers(a_names: dict[str, set[str]]) -> tuple[list[tuple], dict]:
    texts, meta = {}, {}
    for v in ("v1", "v2", "v3"):
        p = PAPERS / f"{v}.pdf"
        texts[v] = pdf_text(p)
        meta[v] = {"sha256": file_sha256(p), "text_chars": len(texts[v])}
    rows = []
    for eco in ECOS:
        for n in sorted(a_names[eco]):
            pat = name_pattern(n)
            for v in ("v1", "v2", "v3"):
                t = texts[v]
                for m in pat.finditer(t):
                    ctx = t[max(0, m.start() - CONTEXT): m.end() + CONTEXT]
                    rows.append((v, eco, n, m.start(), m.group(0), ctx))
    return rows, meta


# ── main ──────────────────────────────────────────────────────────────────────
def write_private(path: Path, lines) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{x}\n" for x in lines))


def main() -> dict:
    db_sha_before = file_sha256(DB)
    wal = DB.with_name(DB.name + "-wal")
    db = load_db(DB)
    names = build_names(db)

    stdlib, stdlib_meta = load_stdlib()
    node_bi, node_meta = load_node_builtins()
    fp_imports = load_fp_imports()
    rules = make_rules(stdlib, node_bi, fp_imports)

    res: dict = {"seed": SEED}
    # 1. universe
    uni = {}
    for e in ECOS:
        keys = {x for n in names[e] for x in names[e][n]["raw"]}
        per_model = {}
        for m in MODELS:
            s = {n for n, r in names[e].items() if m in r["models"]}
            per_model[m] = {"n": len(s), "sha256": list_sha256(s)}
        uni[e] = {"n_raw_keys": len(keys), "n_normalized": len(names[e]),
                  "normalization_merges": len(keys) - len(names[e]),
                  "n_never_mentioned": sum(1 for r in names[e].values() if r["freq"] == 0),
                  "sha256": list_sha256(names[e]), "per_model": per_model}
        write_private(PRIVATE / f"universe_{e}.txt", sorted(names[e]))
    res["universe"] = uni

    # 2-3. exclusions and groups
    excl, groups, grp_names = {}, {}, {}
    for e in ECOS:
        allnames = set(names[e])
        cands = {n for n, r in names[e].items() if r["candidate"]}
        univ_raw = {n for n in cands if len(names[e][n]["models"]) == 5}
        _, c_uni = apply_exclusions(names, rules, e, allnames)
        kept_c, c_c = apply_exclusions(names, rules, e, cands)
        _, c_u5 = apply_exclusions(names, rules, e, univ_raw)
        excl[e] = {"universe": c_uni, "v3_candidates": c_c, "v3_universal_before_exclusions": c_u5}
        A = {n for n in kept_c if len(names[e][n]["models"]) == 5}
        B = kept_c - A
        B4 = {n for n in B if len(names[e][n]["models"]) == 4}
        groups[e] = {"v3_candidates": len(cands), "v3_universal_before_exclusions": len(univ_raw),
                     "candidates_after_exclusions": len(kept_c),
                     "A": len(A), "B": len(B), "B4": len(B4),
                     "B_by_n_models": {str(k): sum(1 for n in B if len(names[e][n]["models"]) == k)
                                       for k in range(0, 5)},
                     "sha256": {"A": list_sha256(A), "B": list_sha256(B), "B4": list_sha256(B4)}}
        grp_names[e] = {"A": A, "B": B, "B4": B4}
        for g, s in (("A", A), ("B", B), ("B4", B4)):
            write_private(PRIVATE / f"group_{g}_{e}.txt", sorted(s))
        excl_lines = []
        for rid, ecos, pred in rules:
            if e in ecos:
                excl_lines += [f"{rid}\t{n}" for n in sorted(cands) if pred(n, names[e][n])]
        write_private(PRIVATE / f"excluded_{e}.tsv", excl_lines)
    res["exclusions"] = excl
    res["groups"] = groups
    pmap = load_pipreqs()
    res["pipreqs_mapping_matches"] = {
        "file": str(PIPREQS.relative_to(REPO)), "sha256": file_sha256(PIPREQS),
        "n_keys_normalized": len(pmap),
        "rule": "name (PEP 503) equals a mapping import-name key (PEP 503); counts only. "
                "The EXISTS rule for the mapped distribution is applied after T0 (PREREG-deviations D10).",
        "pypi": {g: pipreqs_matches(grp_names["pypi"][g], pmap) for g in ("A", "B", "B4")},
        "npm": "not applicable (pipreqs maps Python import names to PyPI distributions)",
    }
    res["exclusion_sources"] = {
        "noise_filter": {"module": NOISE_MODULE,
                         "last_commit": git("log", "-1", "--format=%H", "--", NOISE_MODULE),
                         "blob_sha1": git("hash-object", NOISE_MODULE),
                         "sha256": file_sha256(REPO / NOISE_MODULE)},
        "python_stdlib": {**stdlib_meta, "file": "data/reference/python_stdlib_modules.json",
                          "sha256": file_sha256(REF / "python_stdlib_modules.json")},
        "node_builtins": {**node_meta, "file": "data/reference/node_builtin_modules.json",
                          "sha256": file_sha256(REF / "node_builtin_modules.json"),
                          "plus": "any name starting with 'node:'"},
        "py_false_positive": {"file": str(FP_CSV.relative_to(REPO)), "sha256": file_sha256(FP_CSV),
                              "n_import_names_normalized": len(fp_imports), "column": 1},
    }

    # 4. A1 candidates in paper text
    rows, pdf_meta = find_in_papers({e: grp_names[e]["A"] for e in ECOS})
    write_private(REPO / "data" / "private" / "a1_candidates.txt",
                  ["version\tecosystem\tname\toffset\tmatched\tcontext"]
                  + [f"{v}\t{e}\t{n}\t{o}\t{mt}\t{c}" for v, e, n, o, mt, c in rows])
    res["a1_search"] = {"n_candidates": len(rows), "n_distinct_names": len({(e, n) for _, e, n, *_ in rows}),
                        "pdfs": pdf_meta, "context_chars": CONTEXT,
                        "pdftotext": subprocess.run(["pdftotext", "-v"], capture_output=True, text=True)
                        .stderr.splitlines()[0],
                        "decision": "manual (data/private/a1_candidates.txt)"}

    # 5. strata
    strata, common_sizes = {}, {}
    for e in ECOS:
        A, B4 = grp_names[e]["A"], grp_names[e]["B4"]
        pool = A | B4
        fcuts = tertile_cuts([names[e][n]["freq"] for n in pool])
        lcuts = tertile_cuts([len(n) for n in pool])
        cells = {}
        for fi in range(3):
            for li in range(3):
                cells[f"f{fi + 1}_l{li + 1}"] = {"A": 0, "B4": 0}
        lines = []
        for g, s in (("A", A), ("B4", B4)):
            for n in sorted(s):
                k = f"f{tertile(names[e][n]['freq'], fcuts) + 1}_l{tertile(len(n), lcuts) + 1}"
                cells[k][g] += 1
                lines.append(f"{g}\t{k}\t{names[e][n]['freq']}\t{len(n)}\t{n}")
        write_private(PRIVATE / f"strata_{e}.tsv", lines)
        for c in cells.values():
            c["empty"] = c["A"] == 0 or c["B4"] == 0
        common = [c for c in cells.values() if not c["empty"]]
        common_sizes[e] = (sum(c["A"] for c in common), sum(c["B4"] for c in common))
        strata[e] = {"freq_cuts": fcuts, "len_cuts": lcuts, "cells": cells,
                     "n_common_strata": len(common),
                     "A_in_common": common_sizes[e][0], "B4_in_common": common_sizes[e][1],
                     "A_dropped": len(A) - common_sizes[e][0], "B4_dropped": len(B4) - common_sizes[e][1]}
    res["strata"] = {"method": "tertile cut points = numpy.quantile (linear) at 1/3, 2/3 of pooled A∪B4 "
                               "within ecosystem; value <= cut goes to the lower tertile. Frequency = "
                               "mentions in heuristic_hits (all models). Length = characters of the "
                               "normalized name. A stratum is empty if A or B4 has 0 names in it.",
                     **strata}

    # 6. generation time coverage
    src_counts = {"own": 0, "batch": 0, "none": 0}
    for s in db["gsrc"].values():
        src_counts[s] += 1
    name_cov = {}
    for e in ECOS:
        for g in ("A", "B4", "B"):
            cnt = {"own": 0, "batch": 0, "none": 0}
            for n in grp_names[e][g]:
                cnt[names[e][n]["first_src"] or "none"] += 1
            name_cov[f"{e}.{g}"] = cnt
    res["time_coverage"] = {
        "generations": {"total": len(db["gsrc"]), **src_counts},
        "names_by_source_of_earliest_generation": name_cov,
        "rule": "own = sync API call, generations.created_at; batch = batch API call, "
                "batches.completed_at of the (model, language, dataset) batch; none = neither"}

    # 7. MDE
    pooled = (sum(common_sizes[e][0] for e in ECOS), sum(common_sizes[e][1] for e in ECOS))
    scopes = {"pooled": pooled, **{e: common_sizes[e] for e in ECOS}}
    res["mde"] = {"test": "Fisher exact, two-sided", "alpha": ALPHA, "power": POWER,
                  "simulations_per_point": N_SIM, "seed": SEED, "grid_step": GRID_STEP,
                  "grid": "share in A from p0+step to 1.0; first point with power >= 0.8; "
                          "new default_rng(42) for each (scope, p0)",
                  "sizes": "A and B4 inside common strata",
                  "results": {sc: [mde(na, nb, p0) for p0 in BASELINES] for sc, (na, nb) in scopes.items()}}

    # 8. hashes
    db_sha_after = file_sha256(DB)
    if db_sha_after != db_sha_before:
        raise SystemExit("runs.db changed during the run")
    res["provenance"] = {
        "runs_db_sha256": db_sha_after, "runs_db_bytes": DB.stat().st_size,
        "runs_db_wal_bytes": wal.stat().st_size if wal.exists() else None,
        "runs_db_open_mode": "sqlite URI mode=ro",
        "git_head": git("rev-parse", "HEAD"),
        "git_worktree_dirty": bool(git("status", "--porcelain", "--untracked-files=no")),
        "prereg_commit": "686f648", "network": "blocked (socket.socket replaced)",
    }

    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "prereg_baseline.json").write_text(json.dumps(res, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    (REPORTS / "prereg_baseline.md").write_text(render_md(res))
    print("wrote reports/prereg_baseline.json and reports/prereg_baseline.md")
    return res


# ── markdown ──────────────────────────────────────────────────────────────────
def render_md(r: dict) -> str:
    L = ["# Pre-registration baseline (offline)", "",
         f"Generated by `scripts/prereg_baseline.py` (seed {r['seed']}, network blocked, runs.db read-only). "
         "Aggregates and hashes only; name lists are in `data/private/` and never committed.", "",
         "## 1. Name universe (`validations` keyset)", "",
         "| Ecosystem | Raw keys | Normalized | Merged by normalization | Never mentioned | SHA-256 (sorted, normalized) |",
         "|---|---:|---:|---:|---:|---|"]
    for e in ECOS:
        u = r["universe"][e]
        L.append(f"| {e} | {u['n_raw_keys']} | {u['n_normalized']} | {u['normalization_merges']} | "
                 f"{u['n_never_mentioned']} | `{u['sha256']}` |")
    L += ["", "Per model (names the model produced):", "", "| Model | " + " | ".join(ECOS) + " |",
          "|---|" + "---:|" * len(ECOS)]
    for m in MODELS:
        L.append(f"| `{m}` | " + " | ".join(str(r["universe"][e]["per_model"][m]["n"]) for e in ECOS) + " |")
    L += ["", "Per-model SHA-256 values are in the JSON.", "", "## 2. Exclusions", "",
          "`matched` = names the rule matches on its own; `removed` = names it removes when the rules "
          "are applied in the order shown.", ""]
    src = r["exclusion_sources"]
    for e in ECOS:
        L += [f"**{e}**", "", "| Rule | Universe matched | Universe removed | v3 candidates matched | "
              "v3 candidates removed | v3 universal matched | v3 universal removed |",
              "|---|---:|---:|---:|---:|---:|---:|"]
        ex = r["exclusions"][e]
        for rid in [k for k in ex["universe"] if not k.startswith("_")]:
            L.append(f"| {rid} | " + " | ".join(
                f"{ex[lvl][rid]['matched']} | {ex[lvl][rid]['removed']}"
                for lvl in ("universe", "v3_candidates", "v3_universal_before_exclusions")) + " |")
        L.append("| **input → kept** | " + " | ".join(
            f"{ex[lvl]['_input']} | {ex[lvl]['_kept']}"
            for lvl in ("universe", "v3_candidates", "v3_universal_before_exclusions")) + " |")
        L.append("")
    nf = src["noise_filter"]
    L += ["Sources:", "",
          f"- noise filter `{nf['module']}`: last commit `{nf['last_commit']}`, blob `{nf['blob_sha1']}`",
          f"- Python stdlib: union {src['python_stdlib']['union_normalized']} names "
          f"(`{src['python_stdlib']['file']}`, sha256 `{src['python_stdlib']['sha256'][:16]}…`)"]
    for v in ("3.10", "3.11", "3.12", "3.13"):
        L.append(f"  - {v}: {src['python_stdlib'][v]['n_modules']} — {src['python_stdlib'][v]['source']}")
    L.append(f"- Node built-ins: union {src['node_builtins']['union']} names + `node:` prefix "
             f"(`{src['node_builtins']['file']}`, sha256 `{src['node_builtins']['sha256'][:16]}…`)")
    for v in ("18", "20", "22"):
        L.append(f"  - Node {v}: {src['node_builtins'][v]['n_modules']} — {src['node_builtins'][v]['source']}")
    L += [f"- `{src['py_false_positive']['file']}`: {src['py_false_positive']['n_import_names_normalized']} "
          f"normalized import names (sha256 `{src['py_false_positive']['sha256'][:16]}…`)", "",
          "## 3. Groups (v3 rule: `exists_in_master = 0 AND live_status <> 200`)", "",
          "| Ecosystem | v3 candidates | v3 universal (before excl.) | After exclusions | A | B | B4 |",
          "|---|---:|---:|---:|---:|---:|---:|"]
    for e in ECOS:
        g = r["groups"][e]
        L.append(f"| {e} | {g['v3_candidates']} | {g['v3_universal_before_exclusions']} | "
                 f"{g['candidates_after_exclusions']} | {g['A']} | {g['B']} | {g['B4']} |")
    tot = {k: sum(r["groups"][e][k] for e in ECOS) for k in
           ("v3_candidates", "v3_universal_before_exclusions", "candidates_after_exclusions", "A", "B", "B4")}
    L.append(f"| **total** | {tot['v3_candidates']} | {tot['v3_universal_before_exclusions']} | "
             f"{tot['candidates_after_exclusions']} | {tot['A']} | {tot['B']} | {tot['B4']} |")
    L += ["", "B by number of models:", "", "| Ecosystem | 0 | 1 | 2 | 3 | 4 |", "|---|---:|---:|---:|---:|---:|"]
    for e in ECOS:
        b = r["groups"][e]["B_by_n_models"]
        L.append(f"| {e} | " + " | ".join(str(b[str(k)]) for k in range(5)) + " |")
    L += ["", "| Group hash | " + " | ".join(ECOS) + " |", "|---|---|---|"]
    for g in ("A", "B", "B4"):
        L.append(f"| {g} | " + " | ".join(f"`{r['groups'][e]['sha256'][g]}`" for e in ECOS) + " |")
    pm = r["pipreqs_mapping_matches"]
    L += ["", f"### pipreqs mapping matches (before T0, counts only)", "",
          f"`{pm['file']}` (sha256 `{pm['sha256'][:16]}…`, {pm['n_keys_normalized']} normalized import-name keys). "
          + pm["rule"], "",
          "| Group (pypi) | Names | Match a key | …mapped distribution differs | …mapped distribution same |",
          "|---|---:|---:|---:|---:|"]
    for g in ("A", "B", "B4"):
        x = pm["pypi"][g]
        L.append(f"| {g} | {x['n_group']} | {x['match_key']} | {x['match_key_dist_differs']} | {x['match_key_dist_same']} |")
    L += ["", f"npm: {pm['npm']}."]
    a1 = r["a1_search"]
    L += ["", "## 4. A1 candidates in the paper text", "",
          f"Candidates (matches with ±{a1['context_chars']} chars of context) for manual review: "
          f"**{a1['n_candidates']}** (distinct names: {a1['n_distinct_names']}). No automatic decision.", "",
          f"Text extraction: {a1['pdftotext']}.", "", "| PDF | SHA-256 | Text chars |", "|---|---|---:|"]
    for v, m in a1["pdfs"].items():
        L.append(f"| {v} | `{m['sha256']}` | {m['text_chars']} |")
    L += ["", "## 5. Strata (A vs B4)", "", r["strata"]["method"], ""]
    for e in ECOS:
        s = r["strata"][e]
        L += [f"**{e}** — frequency cuts {s['freq_cuts'][0]:g} / {s['freq_cuts'][1]:g}; "
              f"length cuts {s['len_cuts'][0]:g} / {s['len_cuts'][1]:g}", "",
              "| Frequency tertile \\ Length tertile | L1 | L2 | L3 |", "|---|---|---|---|"]
        for fi in range(1, 4):
            row = []
            for li in range(1, 4):
                c = s["cells"][f"f{fi}_l{li}"]
                row.append(f"A {c['A']} / B4 {c['B4']}" + (" — **empty**" if c["empty"] else ""))
            L.append(f"| F{fi} | " + " | ".join(row) + " |")
        L += ["", f"Common strata: {s['n_common_strata']}/9; inside them A = {s['A_in_common']}, "
              f"B4 = {s['B4_in_common']} (dropped: A {s['A_dropped']}, B4 {s['B4_dropped']}).", ""]
    tc = r["time_coverage"]
    g = tc["generations"]
    L += ["## 6. Generation time coverage", "", tc["rule"] + ".", "",
          "| Records | Total | Own timestamp | Batch time | No time |", "|---|---:|---:|---:|---:|",
          f"| generations | {g['total']} | {g['own']} | {g['batch']} | {g['none']} |"]
    for k, c in tc["names_by_source_of_earliest_generation"].items():
        L.append(f"| names {k} (earliest generation) | {sum(c.values())} | {c['own']} | {c['batch']} | {c['none']} |")
    md = r["mde"]
    L += ["", "## 7. Minimum detectable effect", "",
          f"{md['test']}, alpha {md['alpha']}, power ≥ {md['power']}, Monte Carlo {md['simulations_per_point']} "
          f"simulations per grid point, seed {md['seed']}, **grid step {md['grid_step']}** "
          f"({md['grid']}). Sizes: {md['sizes']}.", "",
          "| Scope | n A | n B4 | Baseline p0 | Min. share in A | Difference | Ratio | Power at MDE |",
          "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for sc, rows in md["results"].items():
        for x in rows:
            if x.get("mde_share_a") is None:
                L.append(f"| {sc} | {x['n_a']} | {x['n_b']} | {x['p0']} | — | — | — | {x.get('note', '')} |")
            else:
                L.append(f"| {sc} | {x['n_a']} | {x['n_b']} | {x['p0']} | {x['mde_share_a']:.3f} | "
                         f"{x['mde_abs_diff']:.3f} | {x['mde_ratio']:.2f} | {x['power_at_mde']:.4f} |")
    p = r["provenance"]
    L += ["", "## 8. Provenance", "",
          f"- runs.db SHA-256: `{p['runs_db_sha256']}` ({p['runs_db_bytes']} bytes; WAL {p['runs_db_wal_bytes']} bytes; "
          f"opened with {p['runs_db_open_mode']}; unchanged after the run)",
          f"- Repository HEAD commit at run: `{p['git_head']}` (tracked files modified: {p['git_worktree_dirty']})",
          f"- PREREG.md commit: `{p['prereg_commit']}`", f"- network: {p['network']}", ""]
    return "\n".join(L)


if __name__ == "__main__":
    main()
