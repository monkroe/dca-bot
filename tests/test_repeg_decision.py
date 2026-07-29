#!/usr/bin/env python3
"""Branch coverage for _repeg_decision().

This is the only evidence that the bid-chase logic is correct. Re-peg has never
fired against Kraken (as of 2026-07-28), and the Phase 2 acceptance verdict has
a deadline of 2026-08-11 after which it closes with re-peg marked
`untested-in-production`. Until a live fill exists, THIS FILE is the proof --
which is why it belongs in the repository rather than in a chat transcript.

Guard order matters and is asserted directly: repeg_max is checked before
everything else, so an exhausted counter must skip even when every other
condition would fire. A reordering that "looks equivalent" would let a leg
re-peg past its limit.

KAS numbers throughout: tick 0.00001, lot_decimals 8.
"""
from _harness import kr, Runner

RP = kr._repeg_decision

TICK = 0.00001
LOT = 8
ORDERMIN = 100.0        # KAS ordermin, well below a real leg
COST = 5.00             # quote budget for the leg
BASE = 0.02800          # our resting limit


def call(**over):
    """Defaults describe a healthy leg that SHOULD re-peg; each test perturbs one thing."""
    kwargs = dict(
        cur_price=BASE,
        bid=BASE + TICK,
        ask=BASE + 5 * TICK,
        ref_price=0.02500,
        h90=None,
        cap_pct=0.20,
        require_above_h90=False,
        tick=TICK,
        min_ticks=1,
        repeg_count=0,
        repeg_max=3,
        ordermin=ORDERMIN,
        cost_target=COST,
        lot_decimals=LOT,
    )
    kwargs.update(over)
    return RP(**kwargs)


def t_happy_path(r):
    action, detail = call()
    r.check("healthy leg re-pegs", action, "repeg")
    r.check("new price is the best bid", detail, BASE + TICK)


def t_max_reached(r):
    r.check("count == max -> skip", call(repeg_count=3)[1], "repeg_max reached")
    r.check("count > max -> skip", call(repeg_count=4)[1], "repeg_max reached")


def t_max_minus_one_allowed(r):
    r.check("count == max-1 still fires", call(repeg_count=2)[0], "repeg")


def t_max_checked_first(r):
    # Everything else is also wrong here; the reason must still be the counter.
    action, detail = call(repeg_count=3, bid=BASE - TICK, ask=BASE - TICK)
    r.check("counter is the FIRST guard", detail, "repeg_max reached")
    r.check("and it skips", action, "skip")


def t_bid_not_above(r):
    r.check("bid equal to our price -> skip", call(bid=BASE)[1], "bid not above resting price")
    r.check("bid below our price -> skip", call(bid=BASE - TICK)[1], "bid not above resting price")


def t_bid_exactly_at_threshold(r):
    # Guard is `bid < cur_price + min_ticks * tick`, so the threshold itself fires.
    r.check("bid exactly 1 tick above -> repeg", call(bid=BASE + TICK)[0], "repeg")


def t_min_ticks_two(r):
    # min_ticks is CONFIGURATION (repeg_min_ticks), not a constant. Raising it
    # raises the bar, and the near-miss must skip.
    r.check("min_ticks 2, bid +1 tick -> skip",
            call(min_ticks=2, bid=BASE + TICK)[1], "bid not above resting price")
    r.check("min_ticks 2, bid +2 ticks -> repeg",
            call(min_ticks=2, bid=BASE + 2 * TICK)[0], "repeg")


def t_spread_collapsed(r):
    r.check("bid above ask -> skip",
            call(bid=BASE + 5 * TICK, ask=BASE + 2 * TICK)[1], "spread collapsed (would cross)")


def t_bid_equals_ask(r):
    # `bid >= ask` -- posting at the ask would cross and lose maker status.
    px = BASE + 3 * TICK
    r.check("bid == ask -> skip", call(bid=px, ask=px)[1], "spread collapsed (would cross)")


def t_cap_vetoes(r):
    # The new bid is above ref * 1.20 -> the same veto the taker fallback respects.
    action, detail = call(bid=0.03100, ask=0.03200, ref_price=0.02500, cap_pct=0.20)
    r.check("above cap -> skip", action, "skip")
    r.check_true("reason names the cap", detail.startswith("above cap ("))


def t_cap_boundary_allows(r):
    # Exactly at the cap is NOT above it, so the leg still re-pegs.
    ref = 0.02500
    r.check("bid exactly at cap -> repeg",
            call(bid=ref * 1.20, ask=ref * 1.30, ref_price=ref, cap_pct=0.20)[0], "repeg")


def t_cap_missing_reference(r):
    # DP-4 again: no reference means no veto, here as well.
    r.check("ref None -> re-peg proceeds", call(ref_price=None, bid=0.05000, ask=0.06000)[0], "repeg")


def t_cap_h90_guard_reaches_repeg(r):
    # The H90 floor must apply on this path too, not only at T0.
    r.check("above cap but below H90 -> repeg",
            call(bid=0.03100, ask=0.03200, ref_price=0.02500,
                 h90=0.04000, require_above_h90=True)[0], "repeg")
    r.check("above cap and above H90 -> skip",
            call(bid=0.03100, ask=0.03200, ref_price=0.02500,
                 h90=0.03000, require_above_h90=True)[0], "skip")


def t_below_ordermin(r):
    # A budget too small for the exchange minimum must not produce a doomed order.
    r.check("volume under ordermin -> skip",
            call(ordermin=1_000_000.0)[1], "new_vol below ordermin")


def t_volume_zero(r):
    # lot_decimals 0 truncates 5.00 / 0.028 = 178.5 -> 178, still fine; make the
    # budget tiny so the floor lands on zero.
    r.check("volume floors to 0 -> skip",
            call(cost_target=0.001, lot_decimals=0, ordermin=0.0)[1], "new_vol below ordermin")


def t_volume_exactly_ordermin(r):
    # `new_vol < ordermin` is the guard, so equality proceeds.
    bid = BASE + TICK
    vol = kr.floor_to_decimals(COST / bid, LOT)
    r.check("volume == ordermin -> repeg", call(ordermin=vol)[0], "repeg")


def t_returns_bid_not_ask(r):
    # The re-post must be at the BID. Posting at the ask would cross and the
    # post-only flag would reject it, dropping the day to a taker fallback.
    _, detail = call(bid=BASE + 2 * TICK, ask=BASE + 9 * TICK)
    r.check("re-post price is the bid", detail, BASE + 2 * TICK)


TESTS = [
    ("happy path", t_happy_path),
    ("repeg_max reached", t_max_reached),
    ("repeg_max minus one", t_max_minus_one_allowed),
    ("repeg_max checked first", t_max_checked_first),
    ("bid not above resting price", t_bid_not_above),
    ("bid exactly at threshold", t_bid_exactly_at_threshold),
    ("min_ticks = 2", t_min_ticks_two),
    ("spread collapsed", t_spread_collapsed),
    ("bid equals ask", t_bid_equals_ask),
    ("cap vetoes", t_cap_vetoes),
    ("cap boundary allows", t_cap_boundary_allows),
    ("cap missing reference", t_cap_missing_reference),
    ("H90 guard on the re-peg path", t_cap_h90_guard_reaches_repeg),
    ("below ordermin", t_below_ordermin),
    ("volume floors to zero", t_volume_zero),
    ("volume exactly ordermin", t_volume_exactly_ordermin),
    ("re-posts at the bid", t_returns_bid_not_ask),
]

if __name__ == "__main__":
    raise SystemExit(Runner("_repeg_decision").run(TESTS))
