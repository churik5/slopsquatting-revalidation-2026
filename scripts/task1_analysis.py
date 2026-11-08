"""Task 1 analysis, stage A (PREREG.md §2-§3, PREREG-deviations.md D0-D15). Offline.

    .venv/bin/python scripts/task1_analysis.py

Inputs (read-only): data/runs.db, data/private/registry_checks.db (rounds 1 and 2),
data/private/prereg/group_*.json, data/reference/*, disclosure/*.csv (April lists).
Task 2 (A vs B registration comparison) is NOT computed here.

Outputs
  data/private/classification.db  final state + as-of-generation status per name, labels per mention
  data/private/top10.csv           top-10 names of the new universal set per ecosystem (for v4)
  data/private/recompute/          name lists of every recompute variant
  notes/classification.sha256      hashes of classification.db (file + canonical content)
  reports/numbers.json             every number (no names)
  reports/task1_report.md          the report (no names)
  reports/diff_v3_vs_new.md        via scripts/make_diff_report.py

Guarantees: network blocked (socket replaced); runs.db and registry_checks.db opened with
sqlite mode=ro and hashed before/after; no package name is printed or written outside
data/private/; seed 42 everywhere.
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
import hashlib  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import sqlite3  # noqa: E402
from collections import Counter, defaultdict  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

import make_diff_report as DIFF  # noqa: E402
import recompute_all as R  # noqa: E402

RUNS_DB = REPO / "data" / "runs.db"
RC_DB = REPO / "data" / "private" / "registry_checks.db"
PRIVATE = REPO / "data" / "private"
CLS_DB = PRIVATE / "classification.db"
TOP10 = PRIVATE / "top10.csv"
REPORTS = REPO / "reports"
NOTES = REPO / "notes"
DISCLOSURE = REPO / "disclosure"
ECOS = PB.ECOS
LANG2ECO = PB.LANG2ECO
REFUSAL_SQL = "g.generated_code IS NULL OR LOWER(TRIM(g.generated_code)) IN ('none', '')"  # = recompute_all.refusals
REG_METHODS = {"pypi": ("pypi_json", "pypi_simple"), "npm": ("npm_registry", "depsdev")}
ROUND_GAP = timedelta(hours=12)
OUTPUTS = ("reports/numbers.json", "reports/task1_report.md", "reports/diff_v3_vs_new.md",
           "notes/classification.sha256")

# as-of-generation statuses (PREREG §2 "Hallucination (as of generation)")
H_ABSENT, H_LATER, FP = "H_ABSENT", "H_REGISTERED_LATER", "FALSE_POSITIVE"
U_D2, U_NO_DATE, U_NO_GEN_TIME = "UNKNOWN_D2_BATCH_INTERVAL", "UNKNOWN_NO_CREATION_DATE", "UNKNOWN_NO_GENERATION_TIME"
U_STATE, U_SIMPLE, U_MAL = "UNKNOWN_FINAL_STATE", "UNKNOWN_EXISTS_SIMPLE_ONLY", "UNKNOWN_REMOVED_MALICIOUS"
ASOF_ORDER = [H_ABSENT, H_LATER, FP, U_D2, U_NO_DATE, U_NO_GEN_TIME, U_STATE, U_SIMPLE, U_MAL]
CATEGORY_ORDER = ["EXISTS", "SECURITY_HOLDING", "UNPUBLISHED", "REMOVED_MALICIOUS", "EXISTS_SIMPLE_ONLY",
                  "ABSENT", "UNKNOWN", "NOT_CHECKED"]
EXCL_ORDER = ["noise_filter", "stdlib_or_builtin", "py_false_positive_import_names", "D15_invalid_name",
              "D1_D11_pipreqs_import_only"]


def rounds12_sha() -> str:
    """Canonical hash of the rows this analysis reads (rounds 1 and 2), stable when other rounds are appended."""
    rc = sqlite3.connect(f"file:{RC_DB}?mode=ro", uri=True)
    h = hashlib.sha256()
    for row in rc.execute("SELECT * FROM registry_checks WHERE round IN (1, 2) ORDER BY id"):
        h.update(("\t".join("" if x is None else str(x) for x in row) + "\n").encode())
    rc.close()
    return h.hexdigest()


def sha256_file(p: Path) -> str:
    return PB.file_sha256(p)


def ts(s: str | None) -> datetime | None:
    """Parse DB / registry timestamps; naive values are UTC (SQLite CURRENT_TIMESTAMP)."""
    if not s:
        return None
    d = datetime.fromisoformat(s.strip().replace(" ", "T").replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def iso(d: datetime | None) -> str | None:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ") if d else None


# ── D15: registry naming rules ────────────────────────────────────────────────
_PEP508 = re.compile(r"[A-Z0-9]|[A-Z0-9][A-Z0-9._-]*[A-Z0-9]", re.IGNORECASE)
_NPM_PART = re.compile(r"[a-z0-9\-._~!'()*]+")      # characters encodeURIComponent leaves as is (lowercase)


def valid_pypi(name: str) -> bool:
    """PEP 508 name syntax."""
    return bool(_PEP508.fullmatch(name))


def valid_npm(name: str) -> bool:
    """validate-npm-package-name, validForNewPackages (core-module names are handled by the
    built-in exclusion rule that runs before D15)."""
    if not name or len(name) > 214 or name != name.strip() or name[0] in "._":
        return False
    if name in ("node_modules", "favicon.ico") or name != name.lower():
        return False
    if name.startswith("@"):
        m = re.fullmatch(r"@([^/]+)/([^/]+)", name)
        if not m:
            return False
        parts = m.groups()
    else:
        parts = (name,)
    if any(not _NPM_PART.fullmatch(p) for p in parts):
        return False
    return not re.search(r"[~'!()*]", name.split("/")[-1])


VALID = {"pypi": valid_pypi, "npm": valid_npm}


# ── preconditions ─────────────────────────────────────────────────────────────
def preconditions(allow_dirty: bool) -> dict:
    st = [ln for ln in PB.git("status", "--porcelain").splitlines() if ln[3:].strip('"') not in OUTPUTS]
    if st and not allow_dirty:
        raise SystemExit(f"working tree not clean ({len(st)} paths); commit first (or --allow-dirty for a test run)")
    if "reports/round2_summary.json" not in PB.git("ls-files", "reports").splitlines():
        raise SystemExit("reports/round2_summary.json is not committed")
    rc = sqlite3.connect(f"file:{RC_DB}?mode=ro", uri=True)
    ends = {r[0] for r in rc.execute("SELECT round FROM run_events WHERE event='end'")}
    rc.close()
    if not {1, 2} <= ends:
        raise SystemExit("round 1 and round 2 must both have an 'end' event")
    return {"git_head": PB.git("rev-parse", "HEAD"), "git_worktree_dirty": bool(st), "round2_summary_commit":
            PB.git("log", "-1", "--format=%H", "--", "reports/round2_summary.json")}


# ── runs.db ───────────────────────────────────────────────────────────────────
def load_generations(cur) -> dict:
    """gid -> (model, language, dataset, lo, hi, source). Sync call: lo = hi = created_at.
    Batch call: lo = batches.submitted_at, hi = batches.completed_at (D2). Otherwise None."""
    batches = {(m, lang, d): (ts(s), ts(c)) for m, lang, d, s, c in cur.execute(
        "SELECT model_key, language, dataset, submitted_at, completed_at FROM batches")}
    out = {}
    for gid, m, lang, ds, created, is_batch in cur.execute("""
            WITH a AS (SELECT generation_id, MAX(batch) AS is_batch FROM api_calls
                       WHERE endpoint = 'code_gen' GROUP BY generation_id)
            SELECT g.id, g.model_key, g.language, g.dataset, g.created_at, a.is_batch
            FROM generations g LEFT JOIN a ON a.generation_id = g.id"""):
        if is_batch == 0 and created:
            t = ts(created)
            out[gid] = (m, lang, ds, t, t, "own")
        elif is_batch == 1 and batches.get((m, lang, ds), (None, None))[1]:
            lo, hi = batches[(m, lang, ds)]
            out[gid] = (m, lang, ds, lo, hi, "batch")
        else:
            out[gid] = (m, lang, ds, None, None, "none")
    return out


def load_hits(cur) -> list[tuple]:
    """(hit_id, gid, raw_key, package_raw, heuristic, keep_v3, halluc_v3) for every mention that
    joins validations — exactly the rows recompute_all.py counts."""
    return cur.execute(
        f"SELECT h.id, g.id, h.package_norm, h.package_raw, h.heuristic, "
        f"CASE WHEN {R.KEEP_V3} THEN 1 ELSE 0 END, CASE WHEN {R.HALLUC_V3} THEN 1 ELSE 0 END "
        f"{R.JOIN} ORDER BY h.id").fetchall()


# ── registry_checks.db → final state per name ─────────────────────────────────
def load_checks(rc) -> dict:
    """(eco, name) -> {round: {method: (state, meta, checked_at)}} from final rows."""
    out: dict = defaultdict(lambda: defaultdict(dict))
    for rnd, n, e, m, s, mj, at in rc.execute(
            "SELECT round, name, ecosystem, method, state, meta_json, checked_at_utc FROM registry_checks "
            "WHERE is_final = 1 AND round IN (1, 2) ORDER BY id"):
        out[(e, n)][rnd][m] = (s, json.loads(mj) if mj else {}, ts(at))
    return out


def final_state(e: str, rounds: dict) -> dict:
    """PREREG §2 final state + D13 (npm special statuses override) + D14 (EXISTS_SIMPLE_ONLY)."""
    m1, m2 = REG_METHODS[e]
    r1, r2 = rounds.get(1, {}), rounds.get(2, {})
    last = r2 if r2 else r1
    s = lambda r, m: r.get(m, (None,))[0]  # noqa: E731
    reason = None
    if s(last, m1) == s(last, m2) == "EXISTS":
        base = "EXISTS"
    elif r2 and all(s(r, m) == "ABSENT" for r in (r1, r2) for m in (m1, m2)):
        gap = min(r2[m][2] - r1[m][2] for m in (m1, m2))
        base, reason = ("ABSENT", None) if gap >= ROUND_GAP else ("UNKNOWN", "rounds_less_than_12h_apart")
    elif e == "pypi" and r2 and all(s(r, "pypi_simple") == "EXISTS" and s(r, "pypi_json") == "ABSENT"
                                    for r in (r1, r2)):
        base = "EXISTS_SIMPLE_ONLY"
    else:
        base = "UNKNOWN"
        if not r1 or s(r1, m1) is None or s(r1, m2) is None:
            reason = "missing_check"
        elif "UNKNOWN" in (s(last, m1), s(last, m2)):
            reason = "http_unknown"
        elif not r2:
            reason = "no_round2"
        else:
            reason = "methods_disagree"
    # registry documents of the latest round in which each method said EXISTS
    doc = {m: next((rounds[r][m][1] for r in (2, 1) if s(rounds.get(r, {}), m) == "EXISTS"), None)
           for m in REG_METHODS[e]}
    mal = (rounds.get(1, {}).get("osv", (None,))[0] == "MAL")
    category = base
    created, late = None, False
    if e == "npm":
        last_npm = next((rounds[r]["npm_registry"] for r in (2, 1) if "npm_registry" in rounds.get(r, {})), None)
        if last_npm and last_npm[0] == "EXISTS":
            md = last_npm[1]
            if md.get("time_unpublished"):
                category = "UNPUBLISHED"
            elif md.get("security_holding"):
                category = "SECURITY_HOLDING"
        if doc["npm_registry"]:
            created = ts(doc["npm_registry"].get("time_created"))
    else:
        ups = [ts(d.get("earliest_upload")) for d in doc.values() if d and d.get("earliest_upload")]
        created = min(ups) if ups else None
        late = bool(doc["pypi_json"] and doc["pypi_json"].get("flag_release_without_files"))
    if category == base and mal and base != "EXISTS":
        category = "REMOVED_MALICIOUS"
    return {"base": base, "unknown_reason": reason, "category": category, "mal": mal,
            "created": created, "created_flag_late": late,
            "checked_round2": bool(r2)}


def asof(st: dict | None, lo: datetime | None, hi: datetime | None) -> str:
    """As-of-generation status of one (name, generation)."""
    cat = st["category"]
    if cat == "ABSENT":
        return H_ABSENT
    if cat == "REMOVED_MALICIOUS":
        return U_MAL
    if cat == "EXISTS_SIMPLE_ONLY":
        return U_SIMPLE
    if cat not in ("EXISTS", "SECURITY_HOLDING", "UNPUBLISHED"):
        return U_STATE
    c = st["created"]
    if c is None:
        return U_NO_DATE
    if hi is None:
        return U_NO_GEN_TIME
    if c > hi:
        return H_LATER
    if c < lo:
        return FP
    return U_D2


# ── main analysis ─────────────────────────────────────────────────────────────
def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--allow-dirty", action="store_true", help="test runs only; recorded in provenance")
    ap.add_argument("--quick", action="store_true", help="test runs only: skip permutation tests")
    args = ap.parse_args()
    pre = preconditions(args.allow_dirty)
    pre["quick_run_without_permutations"] = args.quick
    runs_sha, rc_sha = sha256_file(RUNS_DB), sha256_file(RC_DB)
    base = json.loads((REPORTS / "prereg_baseline.json").read_text())

    # 1. names and v3 candidates exactly as in the offline baseline
    db = PB.load_db(RUNS_DB)
    names = PB.build_names(db)
    stdlib, _ = PB.load_stdlib()
    node_bi, _ = PB.load_node_builtins()
    fp_imports = PB.load_fp_imports()
    rules = dict((rid, (ecos, pred)) for rid, ecos, pred in PB.make_rules(stdlib, node_bi, fp_imports))
    pmap = PB.load_pipreqs()
    pmap_raw: dict = defaultdict(set)              # normalized key -> raw module spellings in the mapping
    for line in PB.PIPREQS.read_text().splitlines():
        if ":" in line:
            imp = line.split(":", 1)[0].strip()
            pmap_raw[PB.norm_pypi(imp)].add(imp)

    conn = sqlite3.connect(f"file:{RUNS_DB}?mode=ro", uri=True)
    cur = conn.cursor()
    gens = load_generations(cur)
    hits = load_hits(cur)
    rc = sqlite3.connect(f"file:{RC_DB}?mode=ro", uri=True)
    checks = load_checks(rc)
    roles = defaultdict(set)
    for n, e, r in rc.execute("SELECT name, ecosystem, role FROM targets WHERE round = 1"):
        roles[(e, n)].add(r)
    rc.close()

    states = {k: final_state(k[0], v) for k, v in checks.items()}

    # 2. exclusion chain over v3 candidates: noise -> stdlib/built-ins -> (py FP list) -> D15 -> D1/D11
    excl_rule: dict = {e: {} for e in ECOS}
    chain: dict = {}
    case_diff: dict = {}
    study: dict = {}
    for e in ECOS:
        cands = sorted(n for n, r in names[e].items() if r["candidate"])
        kept = list(cands)
        counts = {"input_v3_candidates": len(cands)}
        steps = [("noise_filter", rules["noise_filter"][1]),
                 ("stdlib_or_builtin", rules["python_stdlib_3.10-3.13" if e == "pypi" else
                                             "node_builtin_18_20_22_or_node_prefix"][1])]
        if e == "pypi":
            steps.append(("py_false_positive_import_names", rules["py_false_positive_import_names"][1]))
        for rid, pred in steps:
            rem = [n for n in kept if pred(n, names[e][n])]
            for n in rem:
                excl_rule[e][n] = rid
            kept = [n for n in kept if n not in set(rem)]
            counts[rid] = len(rem)
        # the remaining set must be exactly the committed study groups A ∪ B
        A = {n for n in kept if len(names[e][n]["models"]) == 5}
        B = set(kept) - A
        if PB.list_sha256(A) != base["groups"][e]["sha256"]["A"] or PB.list_sha256(B) != base["groups"][e]["sha256"]["B"]:
            raise SystemExit(f"{e}: recomputed groups differ from reports/prereg_baseline.json")
        counts["study_names_live_checked"] = len(kept)
        missing = [n for n in kept if (e, n) not in states]
        if missing:
            raise SystemExit(f"{e}: {len(missing)} study names have no registry checks")
        rem = [n for n in kept if not VALID[e](n)]
        for n in rem:
            excl_rule[e][n] = "D15_invalid_name"
        kept = [n for n in kept if n not in set(rem)]
        counts["D15_invalid_name"] = len(rem)
        if e == "pypi":
            key_matches = sum(1 for n in kept if n in pmap)
            import_only = [n for n in kept if n in pmap and "H1" not in names[e][n]["heur"]]
            rem = [n for n in import_only if any(states.get(("pypi", d), {}).get("category") == "EXISTS"
                                                 for d in pmap[n])]
            for n in rem:
                excl_rule[e][n] = "D1_D11_pipreqs_import_only"
            kept = [n for n in kept if n not in set(rem)]
            counts["D1_D11_pipreqs_import_only"] = len(rem)
            counts["D1_D11_detail"] = {
                "pipreqs_key_matches_after_D15": key_matches,
                "import_only_matches": len(import_only),
                "import_only_but_no_mapped_distribution_EXISTS": len(import_only) - len(rem),
                "import_only_with_zero_extractions": sum(1 for n in import_only if names[e][n]["freq"] == 0)}
            # spelling in code vs module spelling in the mapping (counts only; rule unchanged)
            raw_imports: dict = defaultdict(set)
            for _hid, gid, key, praw, heur, _k, _h in hits:
                if heur == "IMPORTS" and LANG2ECO[gens[gid][1]] == "pypi":
                    raw_imports[PB.norm_pypi(key)].add(re.split(r"[./]", praw)[0])
            c = Counter()
            for n in rem:
                code, mod = raw_imports.get(n, set()), pmap_raw.get(n, set())
                exact = code & mod
                ci = {x for x in code if x not in mod and x.lower() in {m.lower() for m in mod}}
                if not exact and ci and len(ci) == len(code):
                    c["all_code_spellings_differ_from_mapping_by_case_only"] += 1
                elif ci:
                    c["some_code_spellings_differ_by_case_only"] += 1
                elif not exact:
                    c["no_exact_match_other_difference"] += 1
                else:
                    c["exact_match"] += 1
            case_diff = {"names_removed_by_D1_D11": len(rem),
                         "any_code_spelling_differs_from_mapping_module_by_case_only":
                             c["all_code_spellings_differ_from_mapping_by_case_only"]
                             + c["some_code_spellings_differ_by_case_only"],
                         **{k: c[k] for k in ("all_code_spellings_differ_from_mapping_by_case_only",
                                              "some_code_spellings_differ_by_case_only",
                                              "no_exact_match_other_difference", "exact_match")}}
        counts["retained"] = len(kept)
        chain[e] = counts
        study[e] = set(kept)

    # 3. as-of status per mention and per name
    mention_rows = []
    name_mentions: dict = defaultdict(list)       # (e, n) -> [(lo, hi, status)]
    for hid, gid, key, praw, heur, keep3, hal3 in hits:
        m, lang, ds, lo, hi, src = gens[gid]
        e = LANG2ECO[lang]
        n = PB.NORM[e](key)
        sb = 1 if (hal3 and not rules["python_stdlib_3.10-3.13" if e == "pypi" else
                                         "node_builtin_18_20_22_or_node_prefix"][1](n, names[e][n])) else 0
        keep_c = 0 if (keep3 == 0 or excl_rule[e].get(n) == "D15_invalid_name") else 1
        status = None
        if n in study[e]:
            status = asof(states[(e, n)], lo, hi)
            name_mentions[(e, n)].append((lo, hi, status, m))
        hal_c = 1 if keep_c and status in (H_ABSENT, H_LATER) else 0
        unk_c = 1 if keep_c and status and status.startswith("UNKNOWN") else 0
        why = status if status else (excl_rule[e].get(n) or ("v3_existing" if not names[e][n]["candidate"] else "other"))
        mention_rows.append((hid, gid, e, keep3, hal3, 0, key, keep3, sb, 0, key, keep_c, hal_c, unk_c, n, why))

    name_asof = {}
    mixed = Counter()
    for e in ECOS:
        for n in study[e]:
            ms = name_mentions.get((e, n), [])
            timed = [x for x in ms if x[1] is not None]
            if timed:
                lo, hi, _, _ = min(timed, key=lambda x: (x[1], x[0]))
            else:
                lo = hi = None
            name_asof[(e, n)] = asof(states[(e, n)], lo, hi)
            if len({x[2] for x in ms}) > 1:
                mixed[e] += 1

    # 4. classification.db (private)
    disc = {"pypi": {PB.norm_pypi(r[0].strip()) for r in csv.reader((DISCLOSURE / "pypi_universal_hallucinations.csv").open()) if r},
            "npm": {r[0].strip().lower() for r in csv.reader((DISCLOSURE / "npm_universal_hallucinations.csv").open()) if r}}
    write_classification(names, states, excl_rule, study, name_asof, roles, disc, mention_rows)
    cls_file_sha, cls_canon_sha = classification_hashes()

    # 5. recompute every metric: native v3 check, then variants a/b/c at both levels
    rec, rec_names = {}, {}
    native = R.compute(conn, "mention", permutations=False)
    for lvl in R.LEVELS:
        for v in R.VARIANTS:
            res = R.compute(conn, lvl, str(CLS_DB), v, permutations=not args.quick)
            R.write_private(res, PRIVATE / "recompute", f"{v}_{lvl}")
            rec_names[(v, lvl)] = res["universal"], res["top_names"]
            rec[f"{v}_{lvl}"] = R.public(res)
            if lvl == "generation":   # descriptive, not pre-registered: denominator without refusals
                rec[f"{v}_{lvl}"]["per_model_excluding_refusals"] = {
                    m: R.rate(conn.cursor(), lvl, f"g.model_key = ? AND NOT ({REFUSAL_SQL})", (m,)) for m in R.MODELS}
            print(f"recompute variant {v} / {lvl}: done", flush=True)
    a_m = rec["a_mention"]
    if any(native["per_model"][m]["halluc"] != a_m["per_model"][m]["halluc"] or
           native["per_model"][m]["total"] != a_m["per_model"][m]["total"] for m in R.MODELS) or \
            native["universal"] != rec_names[("a", "mention")][0]:
        raise SystemExit("variant a does not reproduce the native v3 rule")

    # decomposition of v3 -> new per model (mentions and generations)
    decomp = decomposition(cur, gens)
    conn.close()

    # 6. top-10 of the new universal set (variant c, mention counts) -> private csv
    uni_c = rec_names[("c", "mention")][0]
    top10 = write_top10(uni_c)

    # 7. assemble numbers
    nums = assemble(pre, runs_sha, rc_sha, base, names, states, excl_rule, chain, case_diff, study,
                    name_asof, mixed, roles, disc, rec, rec_names, decomp, gens, top10,
                    cls_file_sha, cls_canon_sha)
    if sha256_file(RUNS_DB) != runs_sha or sha256_file(RC_DB) != rc_sha:
        raise SystemExit("an input database changed during the run")

    nums["v3_to_new"] = DIFF.build(nums)
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "numbers.json").write_text(json.dumps(nums, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    DIFF.write(nums)
    (REPORTS / "task1_report.md").write_text(render(nums))
    NOTES.mkdir(exist_ok=True)
    (NOTES / "classification.sha256").write_text(
        f"{cls_file_sha}  data/private/classification.db (file bytes)\n"
        f"{cls_canon_sha}  canonical content (TSV of tables names and mentions, sorted; see task1_analysis.py)\n")
    leak_check()
    print("wrote reports/numbers.json, reports/task1_report.md, reports/diff_v3_vs_new.md, notes/classification.sha256")


# ── classification.db ─────────────────────────────────────────────────────────
def write_classification(names, states, excl_rule, study, name_asof, roles, disc, mention_rows) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = CLS_DB.with_name(CLS_DB.name + suffix)
        if p.exists():
            p.unlink()
    c = sqlite3.connect(CLS_DB)
    c.executescript("""
        CREATE TABLE names (ecosystem TEXT, name TEXT, v3_candidate INTEGER, n_models INTEGER, freq INTEGER,
            heuristics TEXT, exclusion TEXT, checked INTEGER, base_state TEXT, unknown_reason TEXT, category TEXT,
            osv_mal INTEGER, created_utc TEXT, created_flag_late INTEGER, asof_status TEXT, roles TEXT,
            in_april_list INTEGER, PRIMARY KEY (ecosystem, name));
        CREATE TABLE mentions (hit_id INTEGER PRIMARY KEY, generation_id INTEGER, ecosystem TEXT,
            keep_a INTEGER, halluc_a INTEGER, unknown_a INTEGER, name_a TEXT,
            keep_b INTEGER, halluc_b INTEGER, unknown_b INTEGER, name_b TEXT,
            keep_c INTEGER, halluc_c INTEGER, unknown_c INTEGER, name_c TEXT, status_c TEXT);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    rows = []
    for e in ECOS:
        keys = set(names[e]) | {n for (ee, n) in states if ee == e}
        for n in sorted(keys):
            r = names[e].get(n)
            st = states.get((e, n))
            rows.append((e, n, int(bool(r and r["candidate"])), len(r["models"]) if r else None,
                         r["freq"] if r else None, ",".join(sorted(r["heur"])) if r else None,
                         excl_rule[e].get(n), int(st is not None),
                         st and st["base"], st and st["unknown_reason"], st and st["category"],
                         st and int(st["mal"]), st and iso(st["created"]), st and int(st["created_flag_late"]),
                         name_asof.get((e, n)), ",".join(sorted(roles.get((e, n), ()))),
                         int(n in disc[e])))
    c.executemany(f"INSERT INTO names VALUES ({','.join('?' * 17)})", rows)
    c.executemany(f"INSERT INTO mentions VALUES ({','.join('?' * 16)})", mention_rows)
    c.executemany("INSERT INTO meta VALUES (?, ?)", [
        ("written_by", "scripts/task1_analysis.py"),
        ("variants", "a = v3 rule; b = a minus stdlib/built-ins; c = live revalidation (PREREG §2, D1-D15)"),
        ("status_c", "as-of-generation status for retained study names, else the exclusion / v3_existing reason")])
    c.commit()
    c.execute("VACUUM")
    c.close()


def classification_hashes() -> tuple[str, str]:
    c = sqlite3.connect(f"file:{CLS_DB}?mode=ro", uri=True)
    h = hashlib.sha256()
    for table, order in (("names", "ecosystem, name"), ("mentions", "hit_id")):
        for row in c.execute(f"SELECT * FROM {table} ORDER BY {order}"):
            h.update(("\t".join("" if x is None else str(x) for x in row) + "\n").encode())
    c.close()
    return sha256_file(CLS_DB), h.hexdigest()


# ── decomposition: where v3 hallucinations went (per model) ───────────────────
def decomposition(cur, gens) -> dict:
    if "cd" not in {r[1] for r in cur.execute("PRAGMA database_list")}:
        cur.execute("ATTACH DATABASE ? AS cd", (f"file:{CLS_DB}?mode=ro",))
    out = {"mention": {}, "generation": {}}
    rows = cur.execute("SELECT generation_id, keep_a, halluc_a, halluc_b, keep_c, halluc_c, status_c "
                       "FROM cd.mentions").fetchall()
    per_gen = defaultdict(lambda: [0, 0, 0])     # gid -> [any a, any b, any c]
    for m in R.MODELS:
        out["mention"][m] = Counter()
    for gid, ka, ha, hb, kc, hc, why in rows:
        m = gens[gid][0]
        d = out["mention"][m]
        if ha:
            if not hb:
                d["v3_halluc_now_stdlib_or_builtin"] += 1
            elif hc:
                d["v3_halluc_still_halluc"] += 1
            elif not kc:
                d["v3_halluc_now_D15_noise"] += 1
            else:
                d[f"v3_halluc_now_{why}"] += 1
        elif hc:
            d["new_halluc_not_in_v3"] += 1
        g = per_gen[gid]
        g[0] |= ha
        g[1] |= hb
        g[2] |= hc
    for m in R.MODELS:
        c = Counter()
        for gid, (a, b, cc) in per_gen.items():
            if gens[gid][0] != m:
                continue
            c[f"a{a}_b{b}_c{cc}"] += 1
        out["generation"][m] = {"generations_counted_a_b_c": dict(sorted(c.items()))}
        out["mention"][m] = dict(sorted(out["mention"][m].items()))
    return out


def write_top10(uni: dict) -> dict:
    """Top-10 by hallucinated-mention count (variant c) among names of the new universal set."""
    c = sqlite3.connect(f"file:{CLS_DB}?mode=ro", uri=True)
    out, lines = {}, []
    for e in ECOS:
        cnt = Counter()
        for name, k in c.execute("SELECT name_c, COUNT(*) FROM mentions WHERE ecosystem = ? AND halluc_c = 1 "
                                 "GROUP BY name_c", (e,)):
            if name in set(uni[e]):
                cnt[name] = k
        top = sorted(cnt.items(), key=lambda x: (-x[1], x[0]))[:10]
        out[e] = {"n": len(top), "mention_counts": [k for _, k in top],
                  "sha256": PB.list_sha256(n for n, _ in top)}
        lines += [(e, i + 1, n, k) for i, (n, k) in enumerate(top)]
    c.close()
    with open(TOP10, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ecosystem", "rank", "name", "hallucinated_mentions"])
        w.writerows(lines)
    return out


# ── assemble numbers.json ─────────────────────────────────────────────────────
def cnt(it, order=None) -> dict:
    c = Counter(it)
    keys = order or sorted(c)
    return {k: c.get(k, 0) for k in keys if c.get(k, 0) or order}


def assemble(pre, runs_sha, rc_sha, base, names, states, excl_rule, chain, case_diff, study, name_asof,
             mixed, roles, disc, rec, rec_names, decomp, gens, top10, cls_file_sha, cls_canon_sha) -> dict:
    t0 = json.loads((REPORTS / "t0.json").read_text())
    nums: dict = {"seed": R.SEED, "t0_utc": t0["t0_utc"]}

    # final states of every live-checked study name (before D15 / D1-D11) and of retained names
    fs = {}
    for e in ECOS:
        checked = [n for n in names[e] if names[e][n]["candidate"] and excl_rule[e].get(n) not in
                   ("noise_filter", "stdlib_or_builtin", "py_false_positive_import_names")]
        fs[e] = {"all_study_names": cnt((states[(e, n)]["category"] for n in checked), CATEGORY_ORDER[:-1]),
                 "retained_after_exclusions": cnt((states[(e, n)]["category"] for n in study[e]), CATEGORY_ORDER[:-1]),
                 "unknown_reasons_retained": cnt(states[(e, n)]["unknown_reason"] for n in study[e]
                                                 if states[(e, n)]["category"] == "UNKNOWN"),
                 "base_state_before_D13_retained": cnt(f"{states[(e, n)]['category']}<-{states[(e, n)]['base']}"
                                                       for n in study[e]
                                                       if states[(e, n)]["category"] != states[(e, n)]["base"]),
                 "osv_mal_flag_retained": sum(1 for n in study[e] if states[(e, n)]["mal"]),
                 "osv_mal_flag_by_category_retained": cnt(states[(e, n)]["category"] for n in study[e]
                                                          if states[(e, n)]["mal"]),
                 "checked_in_round2_retained": sum(1 for n in study[e] if states[(e, n)]["checked_round2"])}
    nums["final_states"] = fs
    nums["final_state_rules"] = {
        "EXISTS": "both registry methods EXISTS in the latest round that checked the name",
        "ABSENT": "both methods ABSENT in round 1 and in round 2, >= 12 h apart for every method",
        "EXISTS_SIMPLE_ONLY": "PyPI Simple EXISTS and JSON ABSENT in both rounds (D14); UNKNOWN in the primary analysis",
        "UNPUBLISHED / SECURITY_HOLDING": "npm registry document of the latest round: time.unpublished / "
                                          "only version 0.0.1-security; overrides the two-method rule (D13)",
        "REMOVED_MALICIOUS": "OSV has a MAL- record and the name is not EXISTS now (creation date unknown)",
        "BLOCKED_BY_REGISTRY": "not assigned: the April 2026 PyPI reply gives a count (68 of 109), not a name list",
        "UNKNOWN": "every other case; never merged into ABSENT"}

    nums["exclusions"] = {"order": EXCL_ORDER, "per_ecosystem": chain,
                          "D1_D11_case_difference": case_diff,
                          "note": "noise filter, stdlib/built-ins and the py FP list run on all v3 candidates "
                                  "(as in the offline baseline; the result equals the committed groups A∪B); "
                                  "D15 and D1/D11 run on the live-checked study names."}

    # as-of generation
    asof_c = {}
    for e in ECOS:
        d = {"names": cnt((name_asof[(e, n)] for n in study[e]), ASOF_ORDER),
             "names_with_mixed_status_across_mentions": mixed[e],
             "rule": "name-level status uses the name's earliest generation (smallest upper time bound)"}
        if e == "pypi":
            flagged = [n for n in study[e] if states[(e, n)]["created_flag_late"]]
            d["creation_date_possibly_late_flag"] = {"names": len(flagged),
                                                     "by_status": cnt(name_asof[(e, n)] for n in flagged)}
        asof_c[e] = d
    nums["asof_generation"] = asof_c
    g_src = Counter(v[5] for v in gens.values())
    nums["generation_time"] = {"own_timestamp": g_src["own"], "batch_interval": g_src["batch"],
                               "no_time": g_src["none"], "total": len(gens),
                               "rule": "sync call: generations.created_at; batch call: [batches.submitted_at, "
                                       "batches.completed_at] (D2: creation inside the interval -> UNKNOWN)"}

    nums["recompute"] = rec
    nums["decomposition_v3_to_c"] = decomp

    # universal sets
    uni = {}
    for (v, lvl), (u, _) in rec_names.items():
        if lvl == "mention":
            uni[v] = {e: len(u[e]) for e in ECOS}
    uc = rec_names[("c", "mention")][0]
    v3u = disc
    trans = {}
    for e in ECOS:
        c = Counter()
        for n in v3u[e]:
            if n in set(uc[e]):
                c["still_universal"] += 1
            elif n not in names[e]:
                c["not_in_runs_db"] += 1
            elif n in excl_rule[e]:
                c[f"excluded_{excl_rule[e][n]}"] += 1
            elif n in study[e]:
                s = name_asof[(e, n)]
                c["not_universal_FALSE_POSITIVE" if s == FP else
                  "not_universal_UNKNOWN_as_of_generation" if s.startswith("UNKNOWN") else
                  "not_universal_hallucinated_by_fewer_than_5_models_as_of_generation"] += 1
            else:
                c["not_a_v3_candidate_after_normalization"] += 1
        trans[e] = dict(sorted(c.items()))
    nums["universal"] = {
        "v3_april_lists": {e: len(v3u[e]) for e in ECOS},
        "by_variant_mention_labels": uni,
        "new_c": {e: len(uc[e]) for e in ECOS},
        "new_c_total": sum(len(uc[e]) for e in ECOS),
        "new_c_in_april_lists": {e: len(set(uc[e]) & v3u[e]) for e in ECOS},
        "april_list_fate": trans,
        "new_c_sha256": {e: PB.list_sha256(uc[e]) for e in ECOS},
        "top10_new_universal": top10}

    # transition table v3 label -> new state (names)
    tt = {}
    for e in ECOS:
        c = Counter()
        for n, r in names[e].items():
            lab = "v3_candidate" if r["candidate"] else "v3_existing"
            if lab == "v3_candidate":
                if n in excl_rule[e]:
                    new = f"excluded:{excl_rule[e][n]}"
                else:
                    new = f"{states[(e, n)]['category']} / {name_asof[(e, n)]}"
            else:
                new = (f"control:{states[(e, n)]['category']}" if (e, n) in states and "control" in roles[(e, n)]
                       else "not_live_checked")
            c[(lab, new)] += 1
        tt[e] = [{"v3": a, "new": b, "names": k} for (a, b), k in sorted(c.items())]
    nums["status_transitions"] = tt

    # control sample: v3-existing names, now not found
    ctl = {}
    for e in ECOS:
        cn = [n for (ee, n), rs in roles.items() if ee == e and "control" in rs]
        cats = Counter(states[(e, n)]["category"] for n in cn)
        ctl[e] = {"sample": len(cn), "by_category": dict(sorted(cats.items())),
                  "not_found_now": sum(v for k, v in cats.items() if k != "EXISTS"),
                  "osv_mal_flag": sum(1 for n in cn if states[(e, n)]["mal"])}
    nums["control_sample"] = ctl

    # April PyPI list (121): pathway of each name
    ap = Counter()
    for n in disc["pypi"]:
        if n not in names["pypi"]:
            ap["not_in_runs_db"] += 1
        elif n in excl_rule["pypi"]:
            ap[excl_rule["pypi"][n]] += 1
        elif n in study["pypi"]:
            ap[name_asof[("pypi", n)]] += 1
        else:
            ap["not_a_v3_candidate_after_normalization"] += 1
    named = {"D1_D11_pipreqs_import_only", "D15_invalid_name", FP, "noise_filter", "stdlib_or_builtin"}
    nums["pypi_april_list"] = {"n": len(disc["pypi"]),
                               "D1_D11_import_only": ap.get("D1_D11_pipreqs_import_only", 0),
                               "D15_invalid_name": ap.get("D15_invalid_name", 0),
                               "false_positive_existed_before_generation": ap.get(FP, 0),
                               "noise_filter": ap.get("noise_filter", 0),
                               "rest_hallucination_or_unknown_as_of_generation": sum(
                                   v for k, v in ap.items() if k not in named),
                               "note": "the rest is not split further: registration outcomes of the universal "
                                       "names belong to stage B (task 2)"}

    # generations table vs 199,845
    c = sqlite3.connect(f"file:{RUNS_DB}?mode=ro", uri=True)
    per_model = dict(c.execute("SELECT model_key, COUNT(*) FROM generations GROUP BY model_key").fetchall())
    n_prompts = c.execute("SELECT COUNT(DISTINCT prompt_id) FROM generations").fetchone()[0]
    c.close()
    nums["generations_table"] = {"rows": sum(per_model.values()), "per_model": per_model,
                                 "distinct_prompts": n_prompts,
                                 "prompts_x_models": n_prompts * len(R.MODELS),
                                 "missing_per_model": {m: n_prompts - k for m, k in per_model.items()}}

    nums["provenance"] = {**pre, "runs_db_sha256": runs_sha, "registry_checks_db_sha256": rc_sha,
                          "registry_checks_rounds_1_2_sha256": rounds12_sha(),
                          "classification_db_sha256_file": cls_file_sha,
                          "classification_db_sha256_canonical": cls_canon_sha,
                          "noise_filter_blob": PB.git("hash-object", PB.NOISE_MODULE),
                          "pipreqs_mapping_sha256": sha256_file(PB.PIPREQS),
                          "network": "blocked (socket.socket replaced)", "databases": "sqlite mode=ro"}
    return nums


# ── report ────────────────────────────────────────────────────────────────────
def fd(d: dict) -> str:
    return ", ".join(f"{k} {v}" for k, v in d.items()) if d else "none"


def render(n: dict) -> str:
    L = ["# Task 1, stage A: live revalidation (PREREG §2–§3, D0–D15)", "",
         "Generated by `scripts/task1_analysis.py` (offline, seed 42, databases read-only). "
         "Counts and hashes only; no package names. Task 2 (A vs B) is not computed here.", ""]
    p = n["provenance"]
    L += [f"- git HEAD at run: `{p['git_head']}`; round 2 summary commit `{p['round2_summary_commit'][:10]}`",
          f"- runs.db sha256 `{p['runs_db_sha256'][:16]}…`, registry_checks.db sha256 `{p['registry_checks_db_sha256'][:16]}…`",
          f"- classification.db sha256 (canonical content) `{p['classification_db_sha256_canonical']}` "
          f"(also in `notes/classification.sha256`)", ""]

    # 1. final states
    L += ["## 1. Final state of every live-checked name", ""]
    for k, v in n["final_state_rules"].items():
        L.append(f"- **{k}**: {v}")
    L += ["", "| State | PyPI: all study | PyPI: retained | npm: all study | npm: retained |", "|---|---:|---:|---:|---:|"]
    fs = n["final_states"]
    for cat in CATEGORY_ORDER[:-1]:
        L.append(f"| {cat} | {fs['pypi']['all_study_names'][cat]} | {fs['pypi']['retained_after_exclusions'][cat]} | "
                 f"{fs['npm']['all_study_names'][cat]} | {fs['npm']['retained_after_exclusions'][cat]} |")
    L.append(f"| **total** | {sum(fs['pypi']['all_study_names'].values())} | "
             f"{sum(fs['pypi']['retained_after_exclusions'].values())} | {sum(fs['npm']['all_study_names'].values())} | "
             f"{sum(fs['npm']['retained_after_exclusions'].values())} |")
    for e in ECOS:
        L.append("")
        L.append(f"{e} retained: UNKNOWN reasons: {fd(fs[e]['unknown_reasons_retained'])}; D13/MAL overrides "
                 f"(category<-two-method state): {fd(fs[e]['base_state_before_D13_retained'])}; OSV MAL flag on "
                 f"{fs[e]['osv_mal_flag_retained']} names ({fd(fs[e]['osv_mal_flag_by_category_retained'])}).")

    # 2. exclusions
    ex = n["exclusions"]
    L += ["", "## 2. Exclusions (applied in this order)", "", ex["note"], "",
          "| Step | PyPI removed | npm removed |", "|---|---:|---:|",
          f"| v3 candidates (input) | {ex['per_ecosystem']['pypi']['input_v3_candidates']} | "
          f"{ex['per_ecosystem']['npm']['input_v3_candidates']} |"]
    for rid in EXCL_ORDER:
        L.append(f"| {rid} | {ex['per_ecosystem']['pypi'].get(rid, '—')} | {ex['per_ecosystem']['npm'].get(rid, '—')} |")
    L.append(f"| **retained** | {ex['per_ecosystem']['pypi']['retained']} | {ex['per_ecosystem']['npm']['retained']} |")
    d = ex["per_ecosystem"]["pypi"]["D1_D11_detail"]
    cd = ex["D1_D11_case_difference"]
    L += ["", f"D1/D11 detail (PyPI): {d['pipreqs_key_matches_after_D15']} study names (after D15) match a pipreqs key; "
          f"{d['import_only_matches']} are import-only; {d['import_only_but_no_mapped_distribution_EXISTS']} of those "
          f"stay because no mapped distribution is EXISTS ({d['import_only_with_zero_extractions']} with zero extractions).", "",
          f"Spelling check for the {cd['names_removed_by_D1_D11']} names removed by D1/D11 (rule unchanged): "
          f"**{cd['any_code_spelling_differs_from_mapping_module_by_case_only']}** have a spelling in code that differs "
          f"from the mapping's module only by case — {cd['all_code_spellings_differ_from_mapping_by_case_only']} where every "
          f"code spelling differs, {cd['some_code_spellings_differ_by_case_only']} where some do; "
          f"{cd['no_exact_match_other_difference']} differ otherwise (separators); {cd['exact_match']} match exactly."]

    # 3. as-of
    L += ["", "## 3. Hallucination as of generation (retained names)", "",
          n["asof_generation"]["pypi"]["rule"] + ".", "", "| Status | PyPI | npm |", "|---|---:|---:|"]
    for s in ASOF_ORDER:
        L.append(f"| {s} | {n['asof_generation']['pypi']['names'][s]} | {n['asof_generation']['npm']['names'][s]} |")
    fl = n["asof_generation"]["pypi"]["creation_date_possibly_late_flag"]
    gt = n["generation_time"]
    L += ["", f"PyPI names with the flag “creation date may be late” (a release without files): **{fl['names']}** "
          f"(by status: {fd(fl['by_status'])}).",
          f"Names whose mentions have different statuses: PyPI {n['asof_generation']['pypi']['names_with_mixed_status_across_mentions']}, "
          f"npm {n['asof_generation']['npm']['names_with_mixed_status_across_mentions']}.",
          f"Generation time: {gt['own_timestamp']} own timestamp, {gt['batch_interval']} batch interval, "
          f"{gt['no_time']} none (of {gt['total']})."]

    # 4. rates
    L += ["", "## 4. Task 1 numbers (PREREG §3)", "",
          "Variants: **(a)** v3 method (current code on the original data); **(b)** (a) minus stdlib/built-in names; "
          "**(c)** live revalidation (hallucination as of generation; UNKNOWN not counted). "
          "Generation level = PREREG §3.1 (denominator: all stored generations of the model). "
          "Mention level = the unit of the published v3 numbers.", ""]
    for lvl in R.LEVELS:
        L += [f"### Per-model rate, {lvl} level (95% Wilson CI)", "",
              "| Model | (a) v3 | (b) v3 − stdlib | (c) live | (c) upper if UNKNOWN counted |", "|---|---|---|---|---:|"]
        for m in R.MODELS:
            cells = []
            for v in R.VARIANTS:
                x = n["recompute"][f"{v}_{lvl}"]["per_model"][m]
                cells.append(f"{x['rate']:.2f}% [{x['ci95'][0]:.2f}–{x['ci95'][1]:.2f}] ({x['halluc']}/{x['total']})")
            up = n["recompute"][f"c_{lvl}"]["per_model"][m]["rate_upper_if_unknown_counted"]
            L.append(f"| `{m}` | " + " | ".join(cells) + f" | {up:.2f}% |")
        L.append("")
        L += [f"Other metrics, {lvl} level:", "", "| Metric | (a) | (b) | (c) |", "|---|---|---|---|"]
        rr = {v: n["recompute"][f"{v}_{lvl}"] for v in R.VARIANTS}
        L.append("| χ² + Holm: significant pairs | " + " | ".join(
            f"{rr[v]['chi2']['n_significant']}/{rr[v]['chi2']['n_pairs']}" for v in R.VARIANTS) + " |")
        L.append("| Permutation (10 000, seed 42): pairs p<0.05; max p | " + " | ".join(
            (f"{rr[v]['permutation']['n_significant']}/10; {rr[v]['permutation']['max_p']:.4f}"
             if rr[v]["permutation"] else "skipped") for v in R.VARIANTS) + " |")
        L.append("| Jaccard mean (min–max) | " + " | ".join(
            f"{rr[v]['jaccard']['mean']:.3f} ({rr[v]['jaccard']['min']:.3f}–{rr[v]['jaccard']['max']:.3f})"
            for v in R.VARIANTS) + " |")
        for lang in R.LANGUAGES:
            L.append(f"| {lang} | " + " | ".join(f"{rr[v]['by_language'][lang]['rate']:.2f}%" for v in R.VARIANTS) + " |")
        for ds in R.DATASETS:
            L.append(f"| {ds} | " + " | ".join(f"{rr[v]['by_dataset'][ds]['rate']:.2f}%" for v in R.VARIANTS) + " |")
        L.append("| universal set PyPI / npm | " + " | ".join(
            f"{rr[v]['universal']['n_pypi']} / {rr[v]['universal']['n_npm']}" for v in R.VARIANTS) + " |")
        L.append("")
        L += [f"Python − JavaScript by model ({lvl}, variant c): " + "; ".join(
            f"`{m}` {rr['c']['by_model_language'][m]['python']['rate']:.2f} − "
            f"{rr['c']['by_model_language'][m]['javascript']['rate']:.2f}" for m in R.MODELS), ""]
        L += [f"χ² + Holm pairs ({lvl}, variant c):", "", "| Pair | Δpp | χ² | p_holm | sig | p_perm |",
              "|---|---:|---:|---:|:--:|---:|"]
        perm = {(x["a"], x["b"]): x["p_perm"] for x in (rr["c"]["permutation"] or {"pairs": []})["pairs"]}
        for x in rr["c"]["chi2"]["pairs"]:
            L.append(f"| {x['a']} vs {x['b']} | {x['delta_pp']:.2f} | {x['chi2']:.2f} | {x['p_holm']:.2e} | "
                     f"{'***' if x['significant'] else 'ns'} | {perm.get((x['a'], x['b']), float('nan')):.4f} |")
        L.append("")
    ref = n["recompute"]["c_mention"]["refusals"]
    L += ["### Descriptive (not pre-registered): generation level without refusals", "",
          "Denominator = generations that are not refusals. The DB marks refusals the same way as the refusal "
          "rates below (generated_code NULL, 'none' or empty), so this denominator is used rather than "
          "“generations with at least one extracted package”. Not pre-registered; descriptive only.", "",
          "| Model | (a) v3 | (c) live |", "|---|---|---|"]
    for m in R.MODELS:
        xa = n["recompute"]["a_generation"]["per_model_excluding_refusals"][m]
        xc = n["recompute"]["c_generation"]["per_model_excluding_refusals"][m]
        L.append(f"| `{m}` | " + " | ".join(f"{x['rate']:.2f}% [{x['ci95'][0]:.2f}–{x['ci95'][1]:.2f}] "
                                           f"({x['halluc']}/{x['total']})" for x in (xa, xc)) + " |")
    L.append("")
    L += ["Refusals (same in every variant): " + "; ".join(
        f"`{m}` {ref[m]['refusals']}/{ref[m]['generations']} = {ref[m]['rate']:.2f}%" for m in R.MODELS), ""]

    # universal
    u = n["universal"]
    L += ["### Universal set after revalidation (variant c)", "",
          f"PyPI **{u['new_c']['pypi']}**, npm **{u['new_c']['npm']}**, total **{u['new_c_total']}** "
          f"(of them in the April lists: PyPI {u['new_c_in_april_lists']['pypi']}, npm {u['new_c_in_april_lists']['npm']}).", "",
          "Every name of this set is a hallucination as of generation for all five models. It is not split into "
          "ABSENT / registered later here: registration outcomes of these names are stage B (task 2).",
          f"Top-10 (names in `data/private/top10.csv` only): PyPI {u['top10_new_universal']['pypi']['n']} names "
          f"(mentions {u['top10_new_universal']['pypi']['mention_counts']}), npm {u['top10_new_universal']['npm']['n']} "
          f"(mentions {u['top10_new_universal']['npm']['mention_counts']}).", "",
          "Fate of the April lists (v3 universal set in the DB; UNKNOWN subtypes merged for the same reason):", "", "| Outcome | PyPI | npm |", "|---|---:|---:|"]
    keys = sorted(set(u["april_list_fate"]["pypi"]) | set(u["april_list_fate"]["npm"]))
    for k in keys:
        L.append(f"| {k} | {u['april_list_fate']['pypi'].get(k, 0)} | {u['april_list_fate']['npm'].get(k, 0)} |")
    L.append(f"| **total** | {u['v3_april_lists']['pypi']} | {u['v3_april_lists']['npm']} |")

    # v3 -> new table
    L += ["", "### v3 → new → reason", "", "| Number | v3 (published) | v3 method on DB (a) | New | Reason |",
          "|---|---|---|---|---|"]
    for r in n["v3_to_new"]["rows"]:
        L.append(f"| {r['item']} | {r['v3']} | {r.get('repro', '')} | {r['new']} | {r['reason']} |")
    L += ["", f"All v3 values were checked against the text of v3.pdf ({n['v3_to_new']['pdf_check']}).", ""]

    # transitions
    L += ["### Status transitions (v3 label → new state), names", "", "| Ecosystem | v3 | New | Names |", "|---|---|---|---:|"]
    for e in ECOS:
        for r in n["status_transitions"][e]:
            L.append(f"| {e} | {r['v3']} | {r['new']} | {r['names']} |")

    # control
    L += ["", "### Control sample (v3-existing names, 200 per ecosystem)", "", "| Ecosystem | Sample | Not found now | By state | OSV MAL flag |",
          "|---|---:|---:|---|---:|"]
    for e in ECOS:
        x = n["control_sample"][e]
        L.append(f"| {e} | {x['sample']} | {x['not_found_now']} | {fd(x['by_category'])} | {x['osv_mal_flag']} |")

    # 5. PyPI letter
    pa = n["pypi_april_list"]
    L += ["", "## 5. For the PyPI letter: the 121 names of the April PyPI list", "",
          f"- D1/D11 (import only, mapped distribution exists): **{pa['D1_D11_import_only']}**",
          f"- D15 (invalid name): **{pa['D15_invalid_name']}**",
          f"- False positive (package existed before generation): **{pa['false_positive_existed_before_generation']}**",
          f"- Noise filter: {pa['noise_filter']}",
          f"- Rest — hallucination or UNKNOWN as of generation: {pa['rest_hallucination_or_unknown_as_of_generation']} "
          f"(total {pa['n']}; {pa['note']})", ""]
    gtab = n["generations_table"]
    L += ["## Generations table", "",
          f"Rows: {gtab['rows']}; distinct prompts {gtab['distinct_prompts']} × 5 = {gtab['prompts_x_models']}; "
          f"missing per model: {fd(gtab['missing_per_model'])}.", ""]
    return "\n".join(L)


def leak_check() -> None:
    """Fail if any study name (>= 4 chars, token-bounded) appears in a public output."""
    names = set()
    for f in (PRIVATE / "prereg").glob("group_*_*.json"):
        names |= set(json.loads(f.read_text()))
    for f in DISCLOSURE.glob("*.csv"):
        names |= {r[0].strip() for r in csv.reader(f.open()) if r}
    names = {x.lower() for x in names if len(x) >= 4}
    tok = lambda t: set(re.findall(r"[a-z0-9@/._-]+", t.lower()))  # noqa: E731
    # tokens of the committed script sources (static prose such as ordinary English words) are not leaks
    static = set().union(*(tok((REPO / "scripts" / f).read_text())
                           for f in ("task1_analysis.py", "make_diff_report.py", "recompute_all.py")))
    for rel in OUTPUTS:
        hit = sum(1 for x in (names & tok((REPO / rel).read_text())) if x not in static)
        if hit:
            raise SystemExit(f"leak check: {hit} name-like tokens in {rel}; inspect privately")


if __name__ == "__main__":
    main()
