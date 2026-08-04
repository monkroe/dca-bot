#!/usr/bin/env python3
"""Addendum: inspect the days the spec rule would actually veto, and test an H90 override."""
import pathlib
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import cap_backtest as bt

bt.EVAL_DAYS = 500
res = bt.evaluate("KASUSD")
rows = res["rows"]

print("Days where close/H7 >= 1.20 (the veto zone), full 500d window:\n")
print(f"{'date':<12}{'close':>11}{'H7':>11}{'c/H7':>8}{'H90':>11}{'c/H90':>8}{'p25':>11}{'vs p25':>8}")
print("-" * 80)
for r in sorted(rows, key=lambda r: -(r["close"] / r["H7"])):
    ratio = r["close"] / r["H7"]
    if ratio < 1.20:
        continue
    print(f"{str(r['d']):<12}{r['close']:>11.6f}{r['H7']:>11.6f}{ratio:>8.4f}"
          f"{r['H90']:>11.6f}{r['close']/r['H90']:>8.4f}{r['p25']:>11.6f}"
          f"{r['close']/r['p25']:>8.4f}")

print("\n\nRule comparison over 500d -- does an H90 override change anything?\n")
for label, fn in [
    ("H7 x 1.25 (spec)",            lambda r: r["close"] > r["H7"] * 1.25),
    ("H7 x 1.25 AND close > H90",   lambda r: r["close"] > r["H7"] * 1.25 and r["close"] > r["H90"]),
    ("H7 x 1.20 (spec)",            lambda r: r["close"] > r["H7"] * 1.20),
    ("H7 x 1.20 AND close > H90",   lambda r: r["close"] > r["H7"] * 1.20 and r["close"] > r["H90"]),
]:
    skips = [r for r in rows if fn(r)]
    cheap = [r for r in skips if r["close"] < r["H90"]]
    print(f"  {label:<30} skips={len(skips):>3}  of which below H90 = {len(cheap)}"
          + (f"   [{', '.join(str(r['d']) for r in skips)}]" if skips else ""))
