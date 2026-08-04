#!/usr/bin/env python3
"""Multi-pair view: how the agreed cap rule (H7 x 1.20 AND price > H90) would
behave across other crypto, vs the legacy rule. Read-only, no orders."""
import pathlib
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import cap_backtest as bt

PAIRS = ["KASUSD", "XBTUSD", "ETHUSD", "SOLUSD", "XRPUSD",
         "ADAUSD", "LINKUSD", "AVAXUSD", "DOTUSD"]

CAP_PCT = 0.20


def run(days):
    bt.EVAL_DAYS = days
    print(f"\n{'='*92}\nWindow: last {days} days   |   rule = skip if close > H7*1.20 AND close > H90\n{'='*92}")
    print(f"{'pair':<9}{'max c/H7':>10}{'p99':>8}{'legacy skips':>14}{'cheap':>7}"
          f"{'NEW skips':>11}{'cheap':>7}{'avg cost vs no-cap':>21}")
    print("-" * 92)
    for pair in PAIRS:
        try:
            res = bt.evaluate(pair)
        except Exception as e:
            print(f"{pair:<9}  -- unavailable ({type(e).__name__}: {e})")
            continue
        rows, ratios = res["rows"], res["ratios"]
        legacy = res["rules"]["LIVE (exec x1.03)"]

        new_skips = [r for r in rows if r["close"] > r["H7"] * (1 + CAP_PCT) and r["close"] > r["H90"]]
        new_cheap = [r for r in new_skips if r["close"] < r["H90"]]

        # avg cost under the NEW rule vs buying every day
        skip_days = {r["d"] for r in new_skips}
        buys = [r for r in rows if r["d"] not in skip_days]
        units_new = sum(1.0 / r["close"] for r in buys)
        avg_new = len(buys) / units_new if units_new else float("nan")
        avg_base = res["rules"]["no cap"]["avg_cost"]
        delta = (avg_new / avg_base - 1) * 100

        print(f"{pair:<9}{max(ratios):>10.4f}{bt.pct(ratios, 99):>8.4f}"
              f"{legacy['skips']:>8} ({legacy['skips']/len(rows)*100:>3.0f}%){legacy['cheap_skips']:>7}"
              f"{len(new_skips):>11}{len(new_cheap):>7}{delta:>20.2f}%")


for d in (220, 500):
    run(d)

print("\nlegacy = today's live rule (own exec mids x1.03)   cheap = skipped while below H90")
print("NEW    = H7 x 1.20 AND close > H90 (the rule just implemented)")
