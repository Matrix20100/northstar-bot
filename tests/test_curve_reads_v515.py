"""v5.1.5: 9 of 17 open runner calls logged "curve account did not parse" after the v5.1.4 restart, so their prices
came from DexScreener (the source of the fake milestones). A finished curve whose reserves pump.fun emptied at
migration is now read as migrated, and every curve that still cannot be read logs exactly why."""
from __future__ import annotations

import base64
import struct

from northstar.research import PUMP_CURVE_DISCRIMINATOR, PUMP_FUN_PROGRAM, Researcher

CREATOR = bytes(range(1, 33))


def curve(vt=1_073_000_000_000_000, vs=30_000_000_000, rt=793_100_000_000_000, rs=0, ts=1_000_000_000_000_000,
          complete=0, extra=b"", disc=PUMP_CURVE_DISCRIMINATOR, owner=PUMP_FUN_PROGRAM, cut=None):
    raw = disc + struct.pack("<QQQQQ", vt, vs, rt, rs, ts) + bytes([complete]) + CREATOR + extra
    if cut:
        raw = raw[:cut]
    return {"owner": owner, "data": [base64.b64encode(raw).decode(), "base64"]}


def test_a_live_curve_still_reads():
    st = Researcher._parse_pump_curve_account(curve())
    assert st and not st["complete"] and st["market_cap_sol"] > 0


def test_a_finished_curve_with_emptied_reserves_reads_as_migrated():
    st = Researcher._parse_pump_curve_account(curve(vt=0, vs=0, rt=0, rs=0, complete=1))
    assert st and st["complete"] is True and st["market_cap_sol"] is None and st["creator"]


def test_a_live_curve_with_empty_reserves_is_still_refused():
    assert Researcher._parse_pump_curve_account(curve(vt=0, vs=0)) is None


def test_newer_layouts_with_extra_fields_read():
    st = Researcher._parse_pump_curve_account(curve(extra=bytes(70)))
    assert st and st["market_cap_sol"] > 0


def test_every_failure_says_why():
    P = Researcher.curve_parse_problem
    assert "not the pump.fun program" in P(curve(owner="Other111"))
    assert "unknown account type" in P(curve(disc=bytes(8)))
    assert "old curve layout" in P(curve(cut=49))
    assert "virtual tokens 0" in P(curve(vt=0, vs=0))
    assert "not base64" in P({"owner": PUMP_FUN_PROGRAM, "data": {"parsed": {}}})
