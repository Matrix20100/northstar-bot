"""The pump.fun bonding-curve decode, checked against a real account.

The bytes below are the live curve account for $LOOM
(CgfbhqzqSHoygUwdywzRo8o9wZEbLtRAVYw8fXkPjiaF), read from mainnet on 19 Oct 2026 while the bot
was rejecting coins like it as "not SOL-backed". Its reserves are held outside the account, so
the account carried 1,493,520 lamports of rent against 870,873,131 lamports of real reserves.
"""
from __future__ import annotations

import base64

from northstar.research import Researcher

PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
LOOM_CURVE = (
    "F7f4N2DYrGBvaqEheM8DAMHj5cPYAQAAb9KO1ebQAgAreOgzAAAAAACAxqR+jQMAAD1BQmrW7ljpX2ySnNQN01lm"
    "CTmuhzh4d9F7lWM60meVAABydFNEVgOSJLzl0oxdsGCN2K+VChm87qKo0pQpdgNuXWQAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAZZr/Y/YAQAAAAAAAAAAAAAAAAAAAAAAAA=="
)


def account(data: str = LOOM_CURVE, lamports: int = 1_493_520, owner: str = PUMP_FUN_PROGRAM) -> dict:
    return {"owner": owner, "lamports": lamports, "data": [data, "base64"]}


def test_a_real_curve_decodes_and_counts_as_sol_quoted():
    c = Researcher._parse_pump_curve_account(account())
    assert c is not None
    assert c["virtual_token_reserves_raw"] == 1_072_539_797_383_791
    assert c["virtual_sol_reserves_lamports"] == 2_030_511_186_881
    assert c["real_token_reserves_raw"] == 792_639_797_383_791
    assert c["real_sol_reserves_lamports"] == 870_873_131
    assert c["token_total_supply_raw"] == 1_000_000_000_000_000
    assert c["complete"] is False
    assert c["creator"] == "587ax6v8hk7JnBTY6SUm9kxZ5Ey8YxFWvYxttsZPiNDa"
    assert c["quote_is_sol"] is True, "a pump.fun curve is SOL-quoted"
    assert c["quote_mint"] == "So11111111111111111111111111111111111111112"
    assert c["market_cap_sol"] is not None


def test_reserves_held_outside_the_account_do_not_make_it_non_sol():
    """The regression: rent-only lamports against real reserves used to read as a foreign quote."""
    c = Researcher._parse_pump_curve_account(account(lamports=1_493_520))
    assert c["real_sol_reserves_lamports"] > c["real_sol_reserves_lamports"] * 0  # reserves exist
    assert 1_493_520 < c["real_sol_reserves_lamports"], "the account cannot cover its own reserves"
    assert c["quote_is_sol"] is True


def test_an_account_from_another_program_is_not_decoded():
    assert Researcher._parse_pump_curve_account(account(owner="11111111111111111111111111111111")) is None
