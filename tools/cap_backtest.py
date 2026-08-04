#!/usr/bin/env python3
"""
Cap-rule backtest (offline, read-only).

Question: how would the 7D cap have behaved over the last 220 days under
  (a) the current threshold, and (b) the v2.3 spec rule mid > H7 * 1.25 ?

Reuses the LIVE modules (src/ohlc.py) so SMA/ffill semantics are identical
to what kraken_run.py computes each run. No DB, no Kraken private API,
no orders. Nothing is written anywhere.
"""

import pathlib
import sys
from datetime import timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from ohlc import fetch_ohlc, ffill_gaps, calc_sma, calc_percentile  # noqa: E402

EVAL_DAYS = 220          # days we score (overridable: argv[1])
LOOKBACK = 200           # extra history so H180/p25 exist on day 1 of the eval window
THRESHOLDS = [1.03, 1.10, 1.15, 1.20, 1.25, 1.30]

# The LIVE rule's reference is not H7 -- it is the mean of our own execution mids
# over 7 days. Measured once against live data (2026-07-26): exec-mid ref 0.027695
# vs true H7 0.028237 => ref sat ~1.9% BELOW H7. One observation, not a constant,
# but it fixes the direction: the live rule skips MORE than a pure-H7 proxy.
EXEC_MID_BIAS = 0.990


def build_series(pair: str):
    total = EVAL_DAYS + LOOKBACK
    raw = fetch_ohlc(pair, total)
    end = raw[-1].d
    start = end - timedelta(days=total - 1)
    return ffill_gaps(raw, start=start, end=end)


def day_context(series, i):
    """Metrics as the bot would see them on day i (inclusive), mirroring build_daily_metrics."""
    upto = series[: i + 1]
    closes = [p.close for p in upto]
    window = closes[-220:]   # percentile window stays 220d even when the eval window is longer
    return {
        "d": series[i].d,
        "close": series[i].close,
        "H7": calc_sma(upto, 7),
        "H30": calc_sma(upto, 30),
        "H90": calc_sma(upto, 90),
        "H180": calc_sma(upto, 180),
        "p25": calc_percentile(window, 25),
    }


def evaluate(pair: str):
    series = build_series(pair)
    idxs = range(len(series) - EVAL_DAYS, len(series))
    rows = [day_context(series, i) for i in idxs]
    rows = [r for r in rows if r["H7"] and r["H90"]]

    ratios = sorted(r["close"] / r["H7"] for r in rows)

    out = {"pair": pair, "rows": rows, "ratios": ratios, "rules": {}}

    # baseline: no cap at all
    out["rules"]["no cap"] = score(rows, None)
    # today's LIVE rule: ref = own exec mids (~1.9% below H7) x 1.03
    out["rules"]["LIVE (exec x1.03)"] = score(rows, 1.03, ref_bias=EXEC_MID_BIAS)
    for t in THRESHOLDS:
        out["rules"][f"H7 x {t:.2f}"] = score(rows, t)
    return out


def score(rows, threshold, ref_bias=1.0):
    """Equal-$ DCA on every non-skipped day (skip = no buy, no carryover -- current bot behaviour)."""
    buys, skips = [], []
    for r in rows:
        skipped = threshold is not None and r["close"] > r["H7"] * ref_bias * threshold
        (skips if skipped else buys).append(r)

    budget_per_day = 1.0
    units = sum(budget_per_day / r["close"] for r in buys)
    spent = budget_per_day * len(buys)
    avg_cost = spent / units if units else float("nan")

    # A skip is "cheap" (the 2026-07-26 failure mode) if price was below the long trend.
    cheap_skips = [r for r in skips if r["close"] < r["H90"]]
    deep_skips = [r for r in skips if r["close"] < r["p25"]]
    euphoria_skips = [r for r in skips if r["close"] > r["H90"] * 1.15]

    return {
        "skips": len(skips),
        "buys": len(buys),
        "avg_cost": avg_cost,
        "units_per_dollar": units / spent if spent else float("nan"),
        "cheap_skips": len(cheap_skips),
        "deep_skips": len(deep_skips),
        "euphoria_skips": len(euphoria_skips),
        "skip_days": [r["d"] for r in skips],
    }


def pct(sorted_vals, p):
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * (p / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def report(res):
    pair = res["pair"]
    rows, ratios = res["rows"], res["ratios"]
    print("=" * 74)
    print(f"{pair} -- {len(rows)} scored days  ({rows[0]['d']} .. {rows[-1]['d']})")
    print("=" * 74)

    last = rows[-1]
    print(f"\nLast day {last['d']}: close={last['close']:.6f}  H7={last['H7']:.6f}  "
          f"H30={last['H30']:.6f}  H90={last['H90']:.6f}")
    print(f"  close/H7 = {last['close']/last['H7']:.4f}")

    print("\n-- close/H7 distribution (what a threshold actually means) --")
    for p in (50, 75, 90, 95, 97.5, 99, 100):
        print(f"  p{p:<5} = {pct(ratios, p):.4f}")

    print("\n-- rules --")
    hdr = f"{'rule':<19}{'skips':>7}{'buys':>7}{'avg cost':>12}{'units/$':>11}{'cheap':>8}{'deep':>7}{'euph':>7}"
    print(hdr)
    print("-" * len(hdr))
    base = res["rules"]["no cap"]["avg_cost"]
    for name, s in res["rules"].items():
        delta = (s["avg_cost"] / base - 1) * 100
        print(f"{name:<19}{s['skips']:>7}{s['buys']:>7}{s['avg_cost']:>12.6g}"
              f"{s['units_per_dollar']:>11.5g}{s['cheap_skips']:>8}{s['deep_skips']:>7}"
              f"{s['euphoria_skips']:>7}   ({delta:+.2f}% vs no cap)")

    print("\n  cheap = skipped while below H90 (the 07-26 failure mode)")
    print("  deep  = skipped while below the 220d p25 (worst kind of miss)")
    print("  euph  = skipped while >15% above H90 (a skip that arguably earned its keep)")

    for name in ("H7 x 1.03", "H7 x 1.20", "H7 x 1.25"):
        s = res["rules"][name]
        if 0 < s["skips"] <= 14:
            print(f"\n  {name} skip days: {', '.join(str(d) for d in s['skip_days'])}")
        elif s["skips"]:
            print(f"\n  {name} skip days: {s['skips']} days (too many to list)")
        else:
            print(f"\n  {name} skip days: none")


if __name__ == "__main__":
    args = sys.argv[1:]
    pairs = []
    for a in args:
        if a.startswith("--days="):
            EVAL_DAYS = int(a.split("=", 1)[1])
        else:
            pairs.append(a)
    for pair in pairs or ["KASUSD"]:
        report(evaluate(pair))
        print()
