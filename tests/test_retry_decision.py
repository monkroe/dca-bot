#!/usr/bin/env python3
"""Branch coverage for _retry_decision().

This is the guard that decides whether a leg Kraken refused may be attempted
again inside the same window. It is the only place in the bot where "try the
buy again" is allowed, so the line it must never cross is DP-3: a retry is safe
ONLY when Kraken said no and therefore no order exists.

The distinction that carries the whole design: an explicit rejection means no
order was created; a timeout or a dropped connection means the outcome is
UNKNOWN and the order may be live. Retrying the second case buys twice. Hence
an allowlist -- anything unrecognised is treated as unknown.

Real case behind it, 2026-07-30: AddOrder refused with EOrder:Insufficient
funds at 06:53 while the window ran to ~07:19, and the day was lost with usable
funds still on the account.
"""
from datetime import datetime, timedelta

from _harness import kr, Runner

RD = kr._retry_decision

NOW = datetime(2026, 7, 30, 6, 58)
DEADLINE = datetime(2026, 7, 30, 7, 19)
REFUSED = "limit AddOrder failed: ['EOrder:Insufficient funds']"


def t_the_real_case(r):
    ok, why = RD("failed_kraken", REFUSED, 0, NOW, DEADLINE)
    r.check("2026-07-30 refusal is retryable", ok, True)
    r.check_true("reason names the rejection", "explicit rejection" in why)


def t_only_failed_kraken(r):
    for st in ("filled", "limit_open", "claimed", "skipped_above_cap",
               "canceled_unfilled", "rejected_postonly", "failed_reconciliation"):
        r.check(f"{st} not retryable", RD(st, REFUSED, 0, NOW, DEADLINE)[0], False)


def t_unknown_outcome_never_retries(r):
    # THE case the allowlist exists for: the order may be live.
    for err in ("Connection timed out",
                "urlopen error [Errno 110]",
                "HTTP 502 Bad Gateway",
                "EOrder:Invalid price",     # a rejection, but not one funds can fix
                "",
                None):
        r.check(f"{err!r} -> no retry", RD("failed_kraken", err, 0, NOW, DEADLINE)[0], False)


def t_allowlist_members(r):
    for err in ("['EOrder:Insufficient funds']",
                "EAPI:Rate limit exceeded",
                "EGeneral:Temporary lockout",
                "EService:Unavailable",
                "EService:Busy"):
        r.check(f"{err[:28]} retryable", RD("failed_kraken", err, 0, NOW, DEADLINE)[0], True)


def t_case_insensitive(r):
    r.check("upper case matches", RD("failed_kraken", "EORDER:INSUFFICIENT FUNDS", 0, NOW, DEADLINE)[0], True)
    r.check("lower case matches", RD("failed_kraken", "eorder:insufficient funds", 0, NOW, DEADLINE)[0], True)


def t_window(r):
    r.check("one minute before deadline", RD("failed_kraken", REFUSED, 0, DEADLINE - timedelta(minutes=1), DEADLINE)[0], True)
    # `now > deadline` closes it, so the boundary itself still allows one attempt
    r.check("exactly at deadline", RD("failed_kraken", REFUSED, 0, DEADLINE, DEADLINE)[0], True)
    r.check("one second past", RD("failed_kraken", REFUSED, 0, DEADLINE + timedelta(seconds=1), DEADLINE)[0], False)
    r.check("window closed reason", RD("failed_kraken", REFUSED, 0, DEADLINE + timedelta(minutes=5), DEADLINE)[1], "window closed")


def t_retry_cap(r):
    r.check("first retry", RD("failed_kraken", REFUSED, 0, NOW, DEADLINE)[0], True)
    r.check("third retry", RD("failed_kraken", REFUSED, 2, NOW, DEADLINE)[0], True)
    r.check("fourth blocked", RD("failed_kraken", REFUSED, 3, NOW, DEADLINE)[0], False)
    r.check("custom cap of 1", RD("failed_kraken", REFUSED, 1, NOW, DEADLINE, retry_max=1)[0], False)


def t_guard_order(r):
    # A closed window must win even when everything else would allow a retry,
    # so that a late cycle can never place an order the window forbade.
    ok, why = RD("failed_kraken", REFUSED, 0, DEADLINE + timedelta(hours=2), DEADLINE)
    r.check("closed window blocks", ok, False)
    r.check("and says so", why, "window closed")


def t_retry_parts(r):
    # The attempt counter lives in the client id because `raw` gets replaced on
    # every failure -- a counter kept there would reset on the event that
    # increments it. So this parse IS the retry cap.
    P = kr._retry_parts
    r.check("fresh id", P("dca-KASUSD-2026-07-30-0700"), ("dca-KASUSD-2026-07-30-0700", 0))
    r.check("first retry", P("dca-KASUSD-2026-07-30-0700-r1"), ("dca-KASUSD-2026-07-30-0700", 1))
    r.check("third retry", P("dca-KASUSD-2026-07-30-0700-r3"), ("dca-KASUSD-2026-07-30-0700", 3))
    # Rotating an already-rotated id must not stack suffixes, or the count
    # freezes at 1 and the cap never bites.
    base, n = P("dca-KASUSD-2026-07-30-0700-r1")
    r.check("rotation is idempotent", P(f"{base}-r{n + 1}"), ("dca-KASUSD-2026-07-30-0700", 2))
    r.check("force id untouched", P("dca-KASUSD-2026-07-30-force-1753876543210"),
            ("dca-KASUSD-2026-07-30-force-1753876543210", 0))
    r.check("dry id untouched", P("dca-KASUSD-2026-07-30-0700-dry"),
            ("dca-KASUSD-2026-07-30-0700-dry", 0))
    r.check("empty", P(""), ("", 0))
    r.check("none", P(None), (None, 0))
    # Only the LAST "-r" is the counter. An earlier one belongs to the id.
    r.check("earlier -r is part of the base", P("dca-r2COIN-2026-07-30-0700-r2"),
            ("dca-r2COIN-2026-07-30-0700", 2))
    # "-r" with a non-numeric tail must fall through, not reach int(): a crash
    # here would take down the whole run over an id we merely did not expect.
    r.check("trailing -r alone", P("dca-KASUSD-2026-07-30-0700-r"),
            ("dca-KASUSD-2026-07-30-0700-r", 0))
    r.check("-r with words", P("dca-KASUSD-2026-07-30-0700-retry"),
            ("dca-KASUSD-2026-07-30-0700-retry", 0))
    r.check("-r with a negative", P("dca-KASUSD-2026-07-30-0700-r-1"),
            ("dca-KASUSD-2026-07-30-0700-r-1", 0))


TESTS = [
    ("attempt counter parsing", t_retry_parts),
    ("the real 2026-07-30 case", t_the_real_case),
    ("only failed_kraken", t_only_failed_kraken),
    ("unknown outcome never retries", t_unknown_outcome_never_retries),
    ("allowlist members", t_allowlist_members),
    ("case insensitive", t_case_insensitive),
    ("window bounds", t_window),
    ("retry cap", t_retry_cap),
    ("closed window wins", t_guard_order),
]

if __name__ == "__main__":
    raise SystemExit(Runner("_retry_decision").run(TESTS))
