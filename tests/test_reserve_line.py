"""What the fill notification says about money that did NOT move.

WHY THIS EXISTS. On 2026-08-04 the buy notification ended with
"Liko: $0.00 (0 pirkimų)". That was true about spendable cash and false about
the account: $10.00 was sitting in a KASUSD limit buy placed at 20:09 the
evening before, and nothing in the message said so. Roberto reads the Kraken
app, sees ten dollars, and has to decide which of the two is lying.

The same fact had already been taught to the evening warning on 2026-08-03.
This is the second code path, and it was missed -- the shape this project keeps
hitting (one truth, two renderings, one of them fixed).

Both functions here are pure so the wording can be checked without a key.
`_remaining_after_buy_line` and `_resting_orders_lines` are not tested: they are
the IO wrappers, and everything they decide is decided here.
"""
import sys

from _harness import Runner, kr

# Same live fixture as the warning's test, for the same reason: this is the
# order that produced the complaint.
REAL_ORDER = {"pair": "KASUSD", "side": "buy", "ordertype": "limit",
              "price": 0.02555, "vol": 391.38943, "vol_exec": 0.0}

# The market at the moment of the 2026-08-04 fill notification.
MID_AT_FILL = 0.02649

# What attach_distances() computes for the fixture above; the renderers read
# this field rather than a mid, so both messages print one figure from one dict.
DIST_AT_FILL = "-3.5%"


def test_usd_holds_exposes_raw_parts(t):
    # The warning renders from `label`; this message renders from the parts.
    # If these disappear, the fill line silently loses the price and volume.
    h = kr.usd_holds([REAL_ORDER])[0]
    t.check("pair", h["pair"], "KASUSD")
    t.check("price", h["price"], 0.02555)
    t.check("vol", round(h["vol"], 5), 391.38943)
    t.check("ordertype", h["ordertype"], "limit")
    # The warning's rendering must be untouched by the parts riding along.
    t.check("label unchanged", h["label"], "391.39 KAS @ $0.02555")


def test_distance_is_signed_and_measured_from_the_market(t):
    # A buy limit UNDER the market reads negative: that is how far the price
    # must fall before the order executes.
    t.check("below market", kr.distance_to_market(0.02555, MID_AT_FILL), "-3.5%")
    # A limit above the market keeps the plus sign rather than dropping it,
    # so the two cases can never be confused when skimmed.
    t.check("above market", kr.distance_to_market(0.02755, MID_AT_FILL), "+4.0%")
    t.check("at market", kr.distance_to_market(MID_AT_FILL, MID_AT_FILL), "+0.0%")


def test_distance_is_empty_rather_than_zero_when_unknown(t):
    # A failed ticker read must drop the parenthetical. "0.0%" would announce
    # that the order is about to fill, which is the opposite of not knowing.
    t.check("mid zero", kr.distance_to_market(0.02555, 0), "")
    t.check("mid none", kr.distance_to_market(0.02555, None), "")
    t.check("mid negative", kr.distance_to_market(0.02555, -1), "")
    t.check("price zero", kr.distance_to_market(0, MID_AT_FILL), "")


def test_line_names_the_order_and_the_distance(t):
    h = kr.usd_holds([REAL_ORDER])[0]
    t.check("line", kr.resting_order_line(dict(h, dist=DIST_AT_FILL)),
            "\nLimit Order: 391.39 KAS @ $0.02555 (-3.5%)")


def test_line_survives_a_missing_mid(t):
    # No mid, no parenthetical -- but the order itself is still named, because
    # the money being committed is the part that matters.
    h = kr.usd_holds([REAL_ORDER])[0]
    t.check("no tail", kr.resting_order_line(h),
            "\nLimit Order: 391.39 KAS @ $0.02555")


def test_price_is_not_padded_to_a_fixed_scale(t):
    # `:.5f` would print a $60,000 order as "$60000.00000". `:g` keeps both a
    # sub-cent altcoin and a five-figure price readable.
    big = {"pair": "XBTUSD", "price": 60000.0, "vol": 0.001, "ordertype": "limit"}
    t.check("no trailing zeros", kr.resting_order_line(big),
            "\nLimit Order: 0.00 XBT @ $60000")


def test_caption_comes_from_the_order_not_from_a_guess(t):
    # usd_holds filters on side and quote currency, never on type. A resting
    # stop order captioned "Limit Order" would be the one claim in the line
    # that cannot be checked against the numbers next to it.
    t.check("plain limit", kr.order_type_label("limit"), "Limit Order")
    t.check("stop", kr.order_type_label("stop-loss-limit"), "Stop Loss Limit Order")
    t.check("take profit", kr.order_type_label("take-profit"), "Take Profit Order")
    # Unknown types print verbatim rather than being mapped to "Limit".
    t.check("unknown kept", kr.order_type_label("iceberg"), "Iceberg Order")
    t.check("missing", kr.order_type_label(None), "Order")
    t.check("blank", kr.order_type_label("  "), "Order")


def test_caption_follows_a_stop_order_through_the_line(t):
    stop = dict(REAL_ORDER, ordertype="stop-loss-limit")
    h = kr.usd_holds([stop])[0]
    t.check("captioned by type", kr.resting_order_line(dict(h, dist=DIST_AT_FILL)),
            "\nStop Loss Limit Order: 391.39 KAS @ $0.02555 (-3.5%)")


def test_no_em_dash_anywhere(t):
    h = kr.usd_holds([REAL_ORDER])[0]
    t.check("no em dash", "—" in kr.resting_order_line(dict(h, dist=DIST_AT_FILL)), False)


if __name__ == "__main__":
    sys.exit(Runner("fill notification: reserve + resting orders").run([
        ("usd_holds exposes raw parts", test_usd_holds_exposes_raw_parts),
        ("distance is signed, measured from market",
         test_distance_is_signed_and_measured_from_the_market),
        ("distance empty rather than zero when unknown",
         test_distance_is_empty_rather_than_zero_when_unknown),
        ("line names the order and the distance",
         test_line_names_the_order_and_the_distance),
        ("line survives a missing mid", test_line_survives_a_missing_mid),
        ("price not padded to a fixed scale", test_price_is_not_padded_to_a_fixed_scale),
        ("caption comes from the order", test_caption_comes_from_the_order_not_from_a_guess),
        ("caption follows a stop order", test_caption_follows_a_stop_order_through_the_line),
        ("no em dash", test_no_em_dash_anywhere),
    ]))
