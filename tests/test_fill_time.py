"""The timestamp the fill notification prints.

WHY THIS EXISTS. On 2026-08-05 the KAS buy notification was headed
"2026-08-05 06:58:10 CDT". Kraken's own `closetm` for that order says the fill
happened at 06:54:26 CT. The 224 second difference was our polling interval:
the maker leg rests in the book, and a later cron cycle finalizes it. Nothing
in the message said the stamp was a reading time, so it read as the moment the
trade happened.

`fill_timestamp` already existed for this exact distinction and was wired into
the reference-mid join only. One instant, two renderings, one of them fixed --
the shape this repo keeps hitting.

The fixture is the real order OACYVR-DFEPB-XKNGHN as Kraken returned it.
"""
import sys
from datetime import datetime, timezone

from _harness import Runner, kr

# QueryOrders for OACYVR-DFEPB-XKNGHN, trimmed to the fields under test.
REAL_ORDER = {"opentm": 1785930800.522602, "closetm": 1785930866.314067,
              "status": "closed", "vol_exec": "383.67331", "price": "0.02596"}

# When the run that finalized it did the read: 2026-08-05 11:58:10.600641 UTC.
OBSERVED_AT = datetime(2026, 8, 5, 11, 58, 10, 600641, tzinfo=timezone.utc)


def test_stamp_is_the_fill_not_the_reading(t):
    stamp = kr.fill_time_label(kr.fill_timestamp(REAL_ORDER, OBSERVED_AT))
    t.check("closetm wins", stamp, "2026-08-05 06:54:26 CDT")
    # Named explicitly rather than left implied by the line above: this exact
    # string is what shipped, and it is the defect.
    t.check("not the observation", stamp == "2026-08-05 06:58:10 CDT", False)


def test_polling_lag_is_no_longer_printed(t):
    fill_at = kr.fill_timestamp(REAL_ORDER, OBSERVED_AT)
    t.check("lag seconds", round((OBSERVED_AT - fill_at).total_seconds(), 1), 224.3)


def test_missing_closetm_falls_back_to_the_reading(t):
    # A fill we cannot time is stamped with the time we saw it, which is the
    # honest remaining answer. Dropping the line would be worse: the message
    # would lose its date entirely.
    no_close = {k: v for k, v in REAL_ORDER.items() if k != "closetm"}
    t.check("absent", kr.fill_time_label(kr.fill_timestamp(no_close, OBSERVED_AT)),
            "2026-08-05 06:58:10 CDT")
    t.check("null", kr.fill_time_label(kr.fill_timestamp(dict(REAL_ORDER, closetm=None), OBSERVED_AT)),
            "2026-08-05 06:58:10 CDT")
    t.check("garbage", kr.fill_time_label(kr.fill_timestamp(dict(REAL_ORDER, closetm="soon"), OBSERVED_AT)),
            "2026-08-05 06:58:10 CDT")


def test_zone_label_is_read_not_typed(t):
    # A hand-typed "CDT" was shipped in strike_run and was wrong half the year.
    # January must print CST from the same code path.
    winter = {"closetm": datetime(2026, 1, 15, 18, 30, 0, tzinfo=timezone.utc).timestamp()}
    t.check("winter", kr.fill_time_label(kr.fill_timestamp(winter, OBSERVED_AT)),
            "2026-01-15 12:30:00 CST")


def test_stamp_is_chicago_not_utc(t):
    # The whole message is Chicago time; a UTC stamp would put the morning buy
    # at midday and agree with nothing else Roberto sees.
    t.check("offset applied",
            kr.fill_time_label(kr.fill_timestamp(REAL_ORDER, OBSERVED_AT)).endswith("CDT"), True)
    t.check("not utc hour",
            "11:54:26" in kr.fill_time_label(kr.fill_timestamp(REAL_ORDER, OBSERVED_AT)), False)


if __name__ == "__main__":
    sys.exit(Runner("fill notification: the timestamp").run([
        ("stamp is the fill, not the reading", test_stamp_is_the_fill_not_the_reading),
        ("polling lag no longer printed", test_polling_lag_is_no_longer_printed),
        ("missing closetm falls back", test_missing_closetm_falls_back_to_the_reading),
        ("zone label read, not typed", test_zone_label_is_read_not_typed),
        ("stamp is Chicago, not UTC", test_stamp_is_chicago_not_utc),
    ]))
