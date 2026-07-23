"""Single source of truth for the hallucination-set noise filter.

Background
----------
Before this module the "noise filter" (which drops non-package strings such as
`node:url`, `@/components`, `$VAR`, `/usr` path fragments, …) lived in TWO places
that drifted apart:

  * ``final_sql_filter.txt``           — missing ``node:`` / ``dart:`` / ``//``
  * ``NOISE_FILTER`` copy-pasted into  — had them

so the SQL path let ``node:url`` into the universal set while the Python path did
not. This module defines every rule ONCE. Both the Python predicate
(:func:`is_noise`) and the SQL keep-clause (:func:`sql_keep_clause`) are derived
from the same ``RULES`` table, and ``tests/test_noise_filter.py`` cross-checks
that they agree name-for-name — so they can never silently diverge again.

Each rule keeps a name that is NOT noise. A row survives the filter iff EVERY
rule's keep-condition is true. Every rule carries a one-line ``catches`` comment
describing exactly what it removes, and has a positive + negative test.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

DEFAULT_COL = "h.package_norm"

# Top-level Unix filesystem directories. These leak in as bare "package" names
# when the import extractor splits an absolute path like ``/usr/lib/python3`` and
# keeps a lone segment. `usr` was in the published universal set for this reason.
PATH_ARTIFACTS = frozenset({
    "usr", "etc", "var", "opt", "home", "tmp",
    "root", "sys", "proc", "dev", "mnt", "srv", "boot", "sbin",
})

# Tutorial / boilerplate placeholder names an LLM emits when it does not commit to
# a real dependency. (`your-*` and `sample*` are handled by their own prefix rules.)
PLACEHOLDERS = frozenset({
    "foo", "bar", "baz", "qux", "quux", "foobar",
    "mypackage", "my-package", "mymodule", "my-module", "mylib", "my-lib",
    "packagename", "package-name", "modulename", "module-name",
    "somepackage", "some-package", "example", "example-package", "test-package",
    "placeholder", "changeme", "yourmodule", "yourpackage",
})

SINGLE_SYMBOLS = frozenset({"~", ",", "//", ".", "..", "/", "\\"})


def _sql_str_set(items: frozenset[str]) -> str:
    return "(" + ", ".join("'" + i.replace("'", "''") + "'" for i in sorted(items)) + ")"


@dataclass(frozen=True)
class Rule:
    id: str
    catches: str                       # human-readable: what this rule removes
    keep_sql: Callable[[str], str]     # col -> SQL boolean, TRUE when the name is KEPT
    drop_py: Callable[[str], bool]     # name -> True when the name is NOISE (dropped)


RULES: tuple[Rule, ...] = (
    Rule("dart_package_prefix", "Dart 'package:' import syntax (package:flutter/material)",
         lambda c: f"{c} NOT LIKE 'package:%'", lambda n: n.startswith("package:")),
    Rule("node_prefix", "Node.js built-in with node: prefix (node:fs, node:url) — not an npm package",
         lambda c: f"{c} NOT LIKE 'node:%'", lambda n: n.startswith("node:")),
    Rule("dart_prefix", "Dart built-in with dart: prefix (dart:io, dart:async)",
         lambda c: f"{c} NOT LIKE 'dart:%'", lambda n: n.startswith("dart:")),
    Rule("path_alias_at", "Bundler path alias (@/components, @/utils)",
         lambda c: f"{c} NOT LIKE '@/%'", lambda n: n.startswith("@/")),
    Rule("ph_your", "Placeholder 'your-…' (your-module, your-api-key)",
         lambda c: f"{c} NOT LIKE 'your-%'", lambda n: n.startswith("your-")),
    Rule("ph_whatever", "Placeholder 'whatever…'",
         lambda c: f"{c} NOT LIKE 'whatever%'", lambda n: n.startswith("whatever")),
    Rule("ph_sample", "Placeholder 'sample…' (sample-app, sample-package)",
         lambda c: f"{c} NOT LIKE 'sample%'", lambda n: n.startswith("sample")),
    Rule("template_var", "Shell/template variable ($VAR, $PACKAGE)",
         lambda c: f"{c} NOT LIKE '$%'", lambda n: n.startswith("$")),
    Rule("version_at_latest", "Version tag left on the name (react@latest)",
         lambda c: f"{c} NOT LIKE '%@latest'", lambda n: n.endswith("@latest")),
    Rule("asset_jpg", "Image asset path leaked as an import (logo.jpg)",
         lambda c: f"{c} NOT LIKE '%.jpg%'", lambda n: ".jpg" in n),
    Rule("asset_png", "Image asset path leaked as an import (icon.png)",
         lambda c: f"{c} NOT LIKE '%.png%'", lambda n: ".png" in n),
    Rule("angle_brackets", "Angle-bracket placeholder (<package-name>, <your-lib>)",
         lambda c: f"{c} NOT LIKE '%<%' AND {c} NOT LIKE '%>%'",
         lambda n: "<" in n or ">" in n),
    Rule("curly_braces", "Curly-brace template ({{pkg}}, ${{name}})",
         lambda c: f"{c} NOT LIKE '%{{%' AND {c} NOT LIKE '%}}%'",
         lambda n: "{" in n or "}" in n),
    Rule("single_symbol", "Bare punctuation / path fragment (~, ',', //, ., .., /)",
         lambda c: f"{c} NOT IN {_sql_str_set(SINGLE_SYMBOLS)}",
         lambda n: n in SINGLE_SYMBOLS),
    Rule("path_artifact", "Lone filesystem path segment from an absolute path (/usr/… -> usr)",
         lambda c: f"{c} NOT IN {_sql_str_set(PATH_ARTIFACTS)}",
         lambda n: n in PATH_ARTIFACTS),
    Rule("placeholder_word", "Tutorial placeholder name (foo, mypackage, example)",
         lambda c: f"{c} NOT IN {_sql_str_set(PLACEHOLDERS)}",
         lambda n: n in PLACEHOLDERS),
    Rule("too_long", "Absurdly long token (>50 chars — never a real package name)",
         lambda c: f"LENGTH({c}) <= 50", lambda n: len(n) > 50),
    Rule("has_whitespace", "Multi-word string (contains a space)",
         lambda c: f"{c} NOT LIKE '% %'", lambda n: " " in n),
    Rule("nonempty", "Empty string",
         lambda c: f"{c} <> ''", lambda n: n == ""),
)


def is_noise(name: str | None) -> bool:
    """True when *name* should be dropped from the hallucination set."""
    if name is None:
        return True
    return any(rule.drop_py(name) for rule in RULES)


def keep(name: str | None) -> bool:
    """True when *name* survives the filter (i.e. is a plausible package name)."""
    return not is_noise(name)


def sql_keep_clause(col: str = DEFAULT_COL) -> str:
    """SQL boolean expression that is TRUE for names to KEEP.

    Use as ``... AND (<sql_keep_clause()>)`` in a WHERE clause. Generated from the
    same ``RULES`` as :func:`is_noise`, so the two never diverge.
    """
    parts = [f"    {rule.keep_sql(col)}" for rule in RULES]
    return "(\n" + "\n    AND ".join(parts) + "\n)"


def rules_doc() -> str:
    """Human-readable listing of every rule and what it catches."""
    return "\n".join(f"- {r.id}: {r.catches}" for r in RULES)


def sql_hallucination_clause(
    pkg_col: str = "h.package_norm",
    exists_col: str = "v.exists_in_master",
    live_col: str = "v.live_status",
) -> str:
    """SQL boolean that is TRUE when a mention counts as a hallucination.

    A name counts iff it is (a) absent from the fresh master list, (b) NOT
    confirmed to exist by a live HTTP 200 from the registry, and (c) not noise.
    The `live <> 200` term is what enforces the readiness criterion "no name in
    the final set returns HTTP 200".
    """
    return (
        f"{exists_col} = 0\n"
        f"    AND ({live_col} IS NULL OR {live_col} <> 200)\n"
        f"    AND {sql_keep_clause(pkg_col)}"
    )


def is_counted_hallucination(name: str | None, exists_in_master: int, live_status: int | None) -> bool:
    """Python mirror of :func:`sql_hallucination_clause`."""
    return exists_in_master == 0 and live_status != 200 and keep(name)


if __name__ == "__main__":
    print(sql_keep_clause())
    print("\n# Rules:\n" + rules_doc())
