#!/usr/bin/env python3
"""Branch coverage for cap_decision() and cap_params().

cap_decision is the PURE veto shared by the T0 check, the DP-5 fallback
re-check and the re-peg guard. If it drifts, all three drift together, which is
exactly why it was extracted -- so it is the one function in the buy path that
must not be trusted to a syntax check.

Two behaviours here are load-bearing and easy to "fix" into bugs:
  * missing data NEVER skips (DP-4: the day must not end unbought while funds
    exist). A None reference, a None H90 with the guard on -- both proceed.
  * the H90 floor exists to stop a 7-day mean from vetoing the cheapest days.
    Straight after a crash a violent bounce reads as far above H7 while still
    sitting well below H90. That is the day an accumulator wants. See
    "07-26 legacy" and "crash bounce" below.
"""
from _harness import kr, Runner

CAP = kr.cap_decision


def t_price_none(r):
    r.check("price None -> no skip", CAP(None, 0.028, 0.030, 0.20, True), (False, None))


def t_ref_none(r):
    # No reference resolved (Kraken OHLC unreachable). DP-4: proceed.
    r.check("ref None -> no skip", CAP(0.028, None, 0.030, 0.20, True), (False, None))


def t_ref_zero(r):
    r.check("ref 0 -> no skip", CAP(0.028, 0.0, 0.030, 0.20, True), (False, None))


def t_ref_negative(r):
    r.check("ref < 0 -> no skip", CAP(0.028, -1.0, 0.030, 0.20, True), (False, None))


def t_below_cap(r):
    r.check("price below cap -> no skip", CAP(0.0280, 0.0250, None, 0.20, False), (False, None))


def t_exactly_at_cap(r):
    # `price <= cap_price` is the guard, so the boundary itself must NOT skip.
    ref = 0.0250
    r.check("price == cap exactly -> no skip", CAP(ref * 1.20, ref, None, 0.20, False), (False, None))


def t_just_above_cap_guard_off(r):
    skip, detail = CAP(0.0250 * 1.20 + 1e-9, 0.0250, None, 0.20, False)
    r.check("just above cap, guard off -> skip", skip, True)
    r.check_true("detail mentions the cap price", detail and "cap $" in detail)


def t_above_cap_guard_on_h90_missing(r):
    # Guard on but no H90 available: missing data must not veto a buy.
    r.check("above cap, guard on, H90 None -> no skip",
            CAP(0.0350, 0.0250, None, 0.20, True), (False, None))


def t_above_cap_guard_on_below_h90(r):
    # THE crash-bounce case the H90 floor was added for.
    r.check("above cap but below H90 -> no skip",
            CAP(0.0350, 0.0250, 0.0400, 0.20, True), (False, None))


def t_above_cap_guard_on_equal_h90(r):
    # `price <= h90` -> the boundary proceeds.
    r.check("above cap, price == H90 -> no skip",
            CAP(0.0350, 0.0250, 0.0350, 0.20, True), (False, None))


def t_above_cap_guard_on_above_h90(r):
    skip, detail = CAP(0.0450, 0.0250, 0.0400, 0.20, True)
    r.check("above cap AND above H90 -> skip", skip, True)
    r.check_true("detail present", bool(detail))


def t_detail_format(r):
    # The reason text is parsed by humans reading Telegram, and the percentage
    # is measured against the REFERENCE, not against the cap price.
    # ref 0.025, cap_pct 0.20 -> cap_price 0.030. Price 0.0305 is 22.00% over
    # the REFERENCE and only 1.67% over the cap; the reason must report 22.00%.
    skip, detail = CAP(0.0305, 0.025, None, 0.20, False)
    r.check("skip", skip, True)
    r.check("pct is vs ref, not vs cap", detail.startswith("+22.00% vs ref"), True)
    r.check_true("cap price is shown too", "cap $0.030000" in detail)


def t_cap_pct_zero(r):
    # cap_pct 0 makes the cap the reference itself.
    r.check("cap_pct 0, price == ref -> no skip", CAP(0.025, 0.025, None, 0.0, False), (False, None))
    skip, _ = CAP(0.0250001, 0.025, None, 0.0, False)
    r.check("cap_pct 0, price above ref -> skip", skip, True)


def t_legacy_0726_regression(r):
    # Production numbers from the 2026-07-26 skip, legacy exec_7d mode:
    # "Mid $0.028865 > cap $0.028800 (+3.23% vs 7D ref)". cap_pct was 0.03,
    # so ref = 0.028800 / 1.03. The legacy rule DID skip that day.
    ref = 0.028800 / 1.03
    skip, _ = CAP(0.028865, ref, None, 0.03, False)
    r.check("07-26 legacy rule skips", skip, True)
    # The rebased rule (H7 x 1.20, H90 guard on) must NOT skip the same day.
    skip2, _ = CAP(0.028865, ref, 0.031, 0.20, True)
    r.check("07-26 rebased rule buys", skip2, False)


def t_params_defaults(r):
    r.check("cap_pct None -> default", kr.cap_params({})[0], kr.CAP_PCT_DEFAULT)
    r.check("require None -> default", kr.cap_params({})[1], kr.CAP_REQUIRE_ABOVE_H90_DEFAULT)


def t_params_valid(r):
    pct, req = kr.cap_params({"cap_pct": "0.20", "cap_require_above_h90": True})
    r.check("cap_pct parses from string", pct, 0.20)
    r.check("require passes through", req, True)


def t_params_invalid(r):
    # A malformed setting must fall back, never crash the run.
    r.check("cap_pct garbage -> default", kr.cap_params({"cap_pct": "abc"})[0], kr.CAP_PCT_DEFAULT)
    r.check("cap_pct list -> default", kr.cap_params({"cap_pct": []})[0], kr.CAP_PCT_DEFAULT)


def t_params_require_false(r):
    r.check("require False stays False",
            kr.cap_params({"cap_require_above_h90": False})[1], False)


def t_telemetry_reconstructible(r):
    # cap_telemetry must make the decision checkable by arithmetic afterwards:
    # in ohlc_h7 mode, cap_price / h7 - 1 == cap_pct.
    tel = kr.cap_telemetry(0.0250, 0.20, 0.0400)
    r.check("h90 stored", tel["h90"], 0.0400)
    r.check("cap_price = ref * (1 + pct)", round(tel["cap_price"], 10), round(0.0250 * 1.20, 10))
    r.check("no reference -> cap_price None", kr.cap_telemetry(None, 0.20, 0.04)["cap_price"], None)


TESTS = [
    ("price None", t_price_none),
    ("ref None", t_ref_none),
    ("ref zero", t_ref_zero),
    ("ref negative", t_ref_negative),
    ("below cap", t_below_cap),
    ("exactly at cap", t_exactly_at_cap),
    ("just above cap, guard off", t_just_above_cap_guard_off),
    ("above cap, H90 missing", t_above_cap_guard_on_h90_missing),
    ("above cap, below H90 (crash bounce)", t_above_cap_guard_on_below_h90),
    ("above cap, equal H90", t_above_cap_guard_on_equal_h90),
    ("above cap, above H90", t_above_cap_guard_on_above_h90),
    ("detail format", t_detail_format),
    ("cap_pct zero", t_cap_pct_zero),
    ("07-26 legacy regression", t_legacy_0726_regression),
    ("params defaults", t_params_defaults),
    ("params valid", t_params_valid),
    ("params invalid", t_params_invalid),
    ("params require False", t_params_require_false),
    ("telemetry reconstructible", t_telemetry_reconstructible),
]

if __name__ == "__main__":
    raise SystemExit(Runner("cap_decision").run(TESTS))
