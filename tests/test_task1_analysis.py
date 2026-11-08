"""Offline tests for scripts/task1_analysis.py (classification rules; no databases, no network)."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import task1_analysis as T  # noqa: E402

UTC = timezone.utc
R1 = datetime(2026, 11, 7, 12, tzinfo=UTC)
R2 = R1 + timedelta(hours=22)


def rounds(e, r1, r2=None, osv="NO_MAL"):
    m1, m2 = T.REG_METHODS[e]
    out = {1: {m1: (r1[0], r1[2] if len(r1) > 2 else {}, R1), m2: (r1[1], {}, R1), "osv": (osv, {}, R1)}}
    if r2:
        out[2] = {m1: (r2[0], r2[2] if len(r2) > 2 else {}, R2), m2: (r2[1], {}, R2)}
    return out


def test_valid_pypi():
    assert T.valid_pypi("requests") and T.valid_pypi("a") and T.valid_pypi("zope-interface")
    assert not T.valid_pypi("-foo") and not T.valid_pypi("foo-") and not T.valid_pypi("a b")
    assert not T.valid_pypi("foo\nbar") and not T.valid_pypi("")


def test_valid_npm():
    assert T.valid_npm("left-pad") and T.valid_npm("@scope/pkg") and T.valid_npm("a.b_c")
    assert not T.valid_npm(".hidden") and not T.valid_npm("_x") and not T.valid_npm("Upper")
    assert not T.valid_npm("a b") and not T.valid_npm("@scope") and not T.valid_npm("@a/b/c")
    assert not T.valid_npm("x" * 215) and not T.valid_npm("node_modules") and not T.valid_npm("pkg!")


def test_final_state_basic():
    assert T.final_state("pypi", rounds("pypi", ("EXISTS", "EXISTS")))["category"] == "EXISTS"
    assert T.final_state("pypi", rounds("pypi", ("ABSENT", "ABSENT"), ("ABSENT", "ABSENT")))["category"] == "ABSENT"
    # ABSENT in round 1 only is not enough
    st = T.final_state("pypi", rounds("pypi", ("ABSENT", "ABSENT")))
    assert st["category"] == "UNKNOWN" and st["unknown_reason"] == "no_round2"
    # D14
    st = T.final_state("pypi", rounds("pypi", ("ABSENT", "EXISTS"), ("ABSENT", "EXISTS")))
    assert st["category"] == "EXISTS_SIMPLE_ONLY"


def test_final_state_gap_and_mal():
    r = rounds("pypi", ("ABSENT", "ABSENT"), ("ABSENT", "ABSENT"))
    for m in T.REG_METHODS["pypi"]:
        s, md, _ = r[2][m]
        r[2][m] = (s, md, R1 + timedelta(hours=11))
    assert T.final_state("pypi", r)["unknown_reason"] == "rounds_less_than_12h_apart"
    st = T.final_state("pypi", rounds("pypi", ("ABSENT", "ABSENT"), ("ABSENT", "ABSENT"), osv="MAL"))
    assert st["category"] == "REMOVED_MALICIOUS" and st["mal"]


def test_d13_overrides_two_method_rule():
    doc = {"time_unpublished": {"time": "2026-01-01"}, "time_created": "2025-01-01T00:00:00Z"}
    st = T.final_state("npm", rounds("npm", ("EXISTS", "ABSENT", doc), ("EXISTS", "ABSENT", doc)))
    assert st["base"] == "UNKNOWN" and st["category"] == "UNPUBLISHED"
    hold = {"security_holding": True, "time_created": "2026-06-01T00:00:00Z"}
    st = T.final_state("npm", rounds("npm", ("EXISTS", "EXISTS", hold)))
    assert st["category"] == "SECURITY_HOLDING" and st["created"] == datetime(2026, 6, 1, tzinfo=UTC)


def test_asof():
    lo, hi = datetime(2026, 4, 22, 12, 50, tzinfo=UTC), datetime(2026, 4, 22, 12, 54, tzinfo=UTC)
    st = {"category": "EXISTS", "created": None}
    assert T.asof({"category": "ABSENT"}, lo, hi) == T.H_ABSENT
    assert T.asof(st, lo, hi) == T.U_NO_DATE
    assert T.asof({**st, "created": hi + timedelta(days=1)}, lo, hi) == T.H_LATER
    assert T.asof({**st, "created": lo - timedelta(days=1)}, lo, hi) == T.FP
    assert T.asof({**st, "created": lo + timedelta(minutes=1)}, lo, hi) == T.U_D2       # D2
    assert T.asof({**st, "created": lo}, None, None) == T.U_NO_GEN_TIME
    assert T.asof({"category": "EXISTS_SIMPLE_ONLY"}, lo, hi) == T.U_SIMPLE
    assert T.asof({"category": "REMOVED_MALICIOUS"}, lo, hi) == T.U_MAL
    assert T.asof({"category": "UNKNOWN"}, lo, hi) == T.U_STATE
