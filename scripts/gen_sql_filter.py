"""Regenerate final_sql_filter.txt from the single-source noise_filter module.

Keeps the legacy SQL artifact in lock-step with src/slop_bench/noise_filter.py so
it can never drift again (that drift is exactly what let `node:url` into the set).
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from slop_bench.noise_filter import rules_doc, sql_keep_clause  # noqa: E402

HEADER = (
    "-- GENERATED from src/slop_bench/noise_filter.py — DO NOT EDIT BY HAND.\n"
    "-- Regenerate:  python scripts/gen_sql_filter.py\n"
    "-- Semantics: this is the KEEP clause — a heuristic_hits row is kept when TRUE.\n"
    "-- It replaces the old hand-maintained filter that had drifted from the Python\n"
    "-- copy (it was missing node:/dart:/'//' and the path-artifact/placeholder rules).\n"
    "--\n-- Rules:\n"
    + "\n".join("--   " + line[2:] for line in rules_doc().splitlines())
    + "\n\n"
)


def main() -> None:
    out = REPO / "final_sql_filter.txt"
    out.write_text(HEADER + sql_keep_clause("h.package_norm") + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
