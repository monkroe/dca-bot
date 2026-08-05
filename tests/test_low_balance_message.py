"""The evening low-balance warning: what it counts, and what it names.

WHY THIS IS TESTED. The arithmetic was already right on 2026-08-03 -- the
message printed spendable USD, correctly excluding money held by a resting
order. What was wrong was the LABEL. It said "Kraken USD: $10.00" while the
account held $20.00, because $10.00 was committed to a KASUSD limit buy. The
app said one number, the warning said half of it, and nothing explained the
gap.

Two failure modes live here, and neither shows up as an exception:

  1. Naming a held-money number as if it were the whole balance. Roberto
     reconciles against the Kraken app; a number that cannot be found there
     teaches him to distrust the message that matters most.
  2. Explaining a USD shortfall with an order that holds a DIFFERENT currency.
     On 2026-08-02 a KASUSDT buy and a KASUSD buy were open at once. Only the
     second one had anything to do with the DCA budget, and "KASUSDT" ends in
     "USDT" -- a prefix check on "USD" would have blamed the wrong order.

Both are silent. The message sends, reads fluently, and is wrong.
"""
import sys

from _harness import Runner, kr

# The live 2026-08-03 22:00 snapshot, kept as the fixture because it is the
# case that produced the complaint: ZUSD 20.0023 total, 10.00 held by
# OUWGSB-K6PQP-T6KLJA, buy 391.38943 KASUSD @ 0.02555.
REAL_ORDER = {"pair": "KASUSD", "side": "buy", "ordertype": "limit",
              "price": 0.02555, "vol": 391.38943, "vol_exec": 0.0}
USDT_ORDER = {"pair": "KASUSDT", "side": "buy", "ordertype": "limit",
              "price": 0.026, "vol": 12519.90384, "vol_exec": 0.0}


def test_usd_holds_picks_only_usd_quoted_buys(t):
    holds = kr.usd_holds([REAL_ORDER, USDT_ORDER])
    t.check("one hold", len(holds), 1)
    t.check("cost", round(holds[0]["cost"], 2), 10.00)
    t.check("label", holds[0]["label"], "391.39 KAS @ $0.02555")


def test_usd_holds_ignores_sells(t):
    # A resting SELL holds the base asset, not USD. Counting it would inflate
    # the explanation and leave the numbers not adding up.
    sell = dict(REAL_ORDER, side="sell")
    t.check("sell ignored", kr.usd_holds([sell]), [])


def test_usd_holds_subtracts_filled_volume(t):
    # A partially filled order holds only what is left.
    part = dict(REAL_ORDER, vol=400.0, vol_exec=300.0, price=0.05)
    holds = kr.usd_holds([part])
    t.check("remainder only", round(holds[0]["cost"], 2), 5.00)


def test_usd_holds_empty_and_none(t):
    t.check("none", kr.usd_holds(None), [])
    t.check("empty", kr.usd_holds([]), [])


def test_message_names_the_order(t):
    # `dist` is what attach_distances() writes from a live ticker. Set here by
    # hand so the wording stays testable without a network call -- the same
    # split as the message text itself.
    headline, body = kr.low_balance_message(
        10.0023, 10.0, 5.0, total=20.0023, held=10.0,
        holds=[dict(h, dist="-3.5%")
               for h in kr.usd_holds([REAL_ORDER, USDT_ORDER])])
    t.check("headline", headline, "Dėmesio: senka DCA lėšų likutis")
    t.check("total shown", "• Bendras USD likutis: $20.00" in body, True)
    t.check("hold shown", "• Laukia pirkimo: $10.00 (391.39 KAS @ $0.02555, -3.5%)" in body, True)
    t.check("free shown", "• Laisva pirkimams: $10.00" in body, True)
    # The number that used to be labelled "Kraken USD" must no longer be.
    t.check("no lying label", "• USD likutis: $" in body, False)
    # NO INSTRUCTION. There is enough for the buy; the resting order is a
    # position Roberto chose, not a problem to fix.
    t.check("no cancel advice", "atšauk" in body, False)
    t.check("no would-cover line", "pakaktų" in body, False)


def test_message_stays_short_when_nothing_is_held(t):
    # With no hold, "Kraken USD" is TRUE, and two extra lines reading $0.00
    # would be noise in the one message that must not be skimmed.
    _, body = kr.low_balance_message(10.0, 10.0, 5.0, total=10.0, held=0.0, holds=[])
    t.check("plain label", "• USD likutis: $10.00" in body, True)
    t.check("no total line", "Bendras USD likutis" in body, False)
    t.check("no hold line", "Laukia pirkimo" in body, False)
    t.check("no cancel hint", "atšauk" in body, False)


def test_held_without_orders_states_amount_only(t):
    # OpenOrders is a SEPARATE Kraken key permission from Balance. A key that
    # can read balances and not orders knows the amount and not the reason --
    # and must not invent one.
    _, body = kr.low_balance_message(10.0, 10.0, 5.0, total=20.0, held=10.0, holds=[])
    t.check("amount stated", "• Rezervuota orderiuose: $10.00" in body, True)
    t.check("no invented order", "@" in body, False)


def test_escalation_below_one_day(t):
    # A fraction below one day is not "running low", it is a scheduled failure.
    headline, body = kr.low_balance_message(4.0, 10.0, 5.0, total=4.0, held=0.0)
    t.check("headline", headline, "RYTOJ DCA PIRKIMAS NEPAVYKS")
    t.check("says LAISVU", "LAISVŲ" in body, True)
    t.check("says never", "NEVYKDOMI" in body, True)


def test_held_money_named_only_when_it_would_change_the_outcome(t):
    # Tomorrow fails AND the held amount alone would cover it: worth saying,
    # because the app does not show that in one glance. Still a fact, not an
    # order to unwind the position.
    _, body = kr.low_balance_message(4.0, 10.0, 5.0, total=24.0, held=20.0,
                                     holds=kr.usd_holds([dict(REAL_ORDER, price=0.05, vol=400.0)]))
    t.check("named", "Orderyje laukia $20.00 – jų pakaktų rytojaus pirkimui." in body, True)
    t.check("still no imperative", "atšauk" in body, False)


def test_held_money_not_named_when_it_would_not_help(t):
    # Tomorrow fails and the hold is too small to save it. Naming it would be
    # a false lead: cancelling changes nothing, the money still is not there.
    _, body = kr.low_balance_message(4.0, 10.0, 5.0, total=7.0, held=3.0, holds=[])
    t.check("not named", "pakaktų" in body, False)


def test_exactly_one_day_does_not_escalate(t):
    # The live 2026-08-03 case sat at exactly 1.0 days and correctly did NOT
    # escalate: that evening's buy still had its money. Off-by-one here would
    # cry wolf on the night before the night that matters.
    headline, _ = kr.low_balance_message(10.0, 10.0, 5.0, total=20.0, held=10.0)
    t.check("no escalation at 1.0", headline, "Dėmesio: senka DCA lėšų likutis")


def test_more_than_three_orders_are_summarised(t):
    many = [dict(REAL_ORDER, price=0.01 * (i + 1)) for i in range(5)]
    _, body = kr.low_balance_message(10.0, 10.0, 5.0, total=60.0, held=50.0,
                                     holds=kr.usd_holds(many))
    t.check("three listed", body.count("• Laukia pirkimo:"), 3)
    t.check("rest counted", "... ir dar 2" in body, True)


def test_no_em_dash_anywhere(t):
    # Standing rule: no long dash in any message, in either language.
    _, body = kr.low_balance_message(10.0023, 10.0, 5.0, total=20.0023, held=10.0,
                                     holds=kr.usd_holds([REAL_ORDER]))
    t.check("no em dash", "—" in body, False)


if __name__ == "__main__":
    sys.exit(Runner("low-balance warning (holds + wording)").run([
        ("usd_holds keeps only USD-quoted buys", test_usd_holds_picks_only_usd_quoted_buys),
        ("usd_holds ignores sells", test_usd_holds_ignores_sells),
        ("usd_holds subtracts filled volume", test_usd_holds_subtracts_filled_volume),
        ("usd_holds on empty input", test_usd_holds_empty_and_none),
        ("message names the order", test_message_names_the_order),
        ("message stays short with no hold", test_message_stays_short_when_nothing_is_held),
        ("held without orders states amount only", test_held_without_orders_states_amount_only),
        ("escalation below one day", test_escalation_below_one_day),
        ("held money named only when it changes the outcome",
         test_held_money_named_only_when_it_would_change_the_outcome),
        ("held money not named when it would not help",
         test_held_money_not_named_when_it_would_not_help),
        ("exactly one day does not escalate", test_exactly_one_day_does_not_escalate),
        ("more than three orders summarised", test_more_than_three_orders_are_summarised),
        ("no em dash", test_no_em_dash_anywhere),
    ]))
