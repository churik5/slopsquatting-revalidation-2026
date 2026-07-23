"""Tests for the unified noise filter (revalidation task, item 3).

The load-bearing test is :func:`test_python_and_sql_agree`: it proves the Python
predicate and the generated SQL keep-clause return the SAME verdict for every name
in the battery. That is exactly the invariant that broke before (SQL let `node:url`
through, Python did not) and caused the published universal set to be wrong.
"""
import sqlite3

import pytest

from slop_bench.noise_filter import (
    RULES,
    is_counted_hallucination,
    is_noise,
    keep,
    sql_hallucination_clause,
    sql_keep_clause,
)

# (name, expected_is_noise). Every rule has at least one positive example here;
# `test_every_rule_is_exercised` enforces that coverage.
NOISE = [
    ("package:flutter", "dart_package_prefix"),
    ("node:url", "node_prefix"),
    ("node:fs", "node_prefix"),
    ("dart:io", "dart_prefix"),
    ("@/components", "path_alias_at"),
    ("your-module", "ph_your"),
    ("whatever-pkg", "ph_whatever"),
    ("sample-app", "ph_sample"),
    ("$package", "template_var"),
    ("react@latest", "version_at_latest"),
    ("logo.jpg", "asset_jpg"),
    ("icon.png", "asset_png"),
    ("<package-name>", "angle_brackets"),
    ("{pkg}", "curly_braces"),
    ("//", "single_symbol"),
    ("~", "single_symbol"),
    (",", "single_symbol"),
    ("usr", "path_artifact"),
    ("etc", "path_artifact"),
    ("var", "path_artifact"),
    ("home", "path_artifact"),
    ("foo", "placeholder_word"),
    ("mypackage", "placeholder_word"),
    ("example", "placeholder_word"),
    ("a" * 51, "too_long"),
    ("two words", "has_whitespace"),
    ("", "nonempty"),
]

# Real, plausible package names that MUST survive the filter.
KEPT = [
    "requests", "numpy", "flask", "django", "pandas",
    "express", "react", "left-pad", "lodash.debounce",
    "@redacted/pkg-a", "@scope/pkg", "@babel/core",
    "redacted-pkg-c", "django-rest-framework", "zope-interface",
    "redacted-pkg-d", "redacted-pkg-b",  # exist in the registry now — dropped by live check, NOT by noise
]


@pytest.mark.parametrize("name,rule_id", NOISE)
def test_noise_names_are_dropped(name, rule_id):
    assert is_noise(name) is True
    assert keep(name) is False


@pytest.mark.parametrize("name", KEPT)
def test_real_names_are_kept(name):
    assert is_noise(name) is False, f"{name!r} should not be filtered as noise"
    assert keep(name) is True


def test_every_rule_is_exercised():
    """Each rule must fire on at least one battery name (no dead rules)."""
    fired = {rule.id for rule in RULES for name, _ in NOISE if rule.drop_py(name)}
    missing = {rule.id for rule in RULES} - fired
    assert not missing, f"rules with no positive example in the battery: {missing}"


def test_python_and_sql_agree():
    """The Python predicate and the generated SQL clause agree name-for-name.

    This is the regression guard for the SQL/Python divergence (bug #4).
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE h (package_norm TEXT)")
    clause = sql_keep_clause("package_norm")

    all_names = [n for n, _ in NOISE] + KEPT + [None]
    for name in all_names:
        conn.execute("DELETE FROM h")
        conn.execute("INSERT INTO h (package_norm) VALUES (?)", (name,))
        row = conn.execute(f"SELECT 1 FROM h WHERE {clause}").fetchone()
        sql_keeps = row is not None
        assert sql_keeps == keep(name), (
            f"disagreement for {name!r}: sql_keeps={sql_keeps}, python_keep={keep(name)}"
        )
    conn.close()


def test_node_and_dart_prefixes_present_in_sql():
    """The exact prefixes that were missing from final_sql_filter.txt are now generated."""
    clause = sql_keep_clause()
    assert "node:%" in clause
    assert "dart:%" in clause
    assert "package:%" in clause


HALLUC_CASES = [
    # (name, exists_in_master, live_status, expected_counted)
    ("fakelib-xyz", 0, 404, True),    # absent + 404 -> hallucination
    ("fakelib-xyz", 0, None, True),   # absent + not yet checked -> counted
    ("redacted-pkg-d", 0, 200, False),      # absent from master BUT live 200 -> exists now
    ("requests", 1, 200, False),      # in master -> not a hallucination
    ("requests", 1, 404, False),      # in master (yanked live) -> still not counted
    ("node:url", 0, 404, False),      # noise -> never counted
    ("usr", 0, 404, False),           # path artifact -> never counted
]


@pytest.mark.parametrize("name,exists,live,expected", HALLUC_CASES)
def test_is_counted_hallucination(name, exists, live, expected):
    assert is_counted_hallucination(name, exists, live) is expected


def test_hallucination_python_and_sql_agree():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE hv (package_norm TEXT, exists_in_master INT, live_status INT)")
    clause = sql_hallucination_clause("package_norm", "exists_in_master", "live_status")
    for name, exists, live, _ in HALLUC_CASES:
        conn.execute("DELETE FROM hv")
        conn.execute("INSERT INTO hv VALUES (?,?,?)", (name, exists, live))
        row = conn.execute(f"SELECT 1 FROM hv WHERE {clause}").fetchone()
        assert (row is not None) == is_counted_hallucination(name, exists, live), name
    conn.close()


def test_task_regression_cases():
    # These are the concrete names from the brief.
    assert is_noise("node:url")   # was leaking into the npm universal set
    assert is_noise("usr")        # extractor artifact from /usr/...
    assert is_noise("dart:io")
    # A real scoped package must not be mistaken for a path alias.
    assert keep("@redacted/pkg-a")
