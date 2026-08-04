#!/usr/bin/env python3
"""Roberto's scenario: a week around 0.27-0.29, then a jump to 0.34 and a week
fluctuating 0.34-0.38. Runs the SHIPPED rule day by day.

Rule: skip only if price > H7 * 1.20 AND price > H90.
The rule is scale-invariant (pure ratios), so his round numbers work as-is.
"""
import random

CAP_PCT = 0.20

# ~90 days of prior history in the 0.27-0.29 band, so H90 sits there too.
random.seed(7)
history = [round(random.uniform(0.27, 0.29), 4) for _ in range(83)]
# the explicit last week he described
history += [0.28, 0.29, 0.27, 0.28, 0.29, 0.28, 0.27]

# the jump week he described
jump_week = [0.34, 0.36, 0.35, 0.38, 0.37, 0.36, 0.38]
# and a second week still in the same band, to show the steady state
second_week = [0.37, 0.35, 0.36, 0.38, 0.36, 0.37, 0.35]


def h(series, n):
    w = series[-n:]
    return sum(w) / len(w)


print(f"pries soki:  H7 = {h(history,7):.4f}   H90 = {h(history,90):.4f}\n")
print(f"{'diena':<7}{'kaina':>8}{'H7':>9}{'cap':>9}{'H90':>9}{'>cap?':>7}{'>H90?':>7}{'':>4}{'sprendimas'}")
print("-" * 68)

series = history[:]
skips = 0
for i, price in enumerate(jump_week + second_week, start=1):
    series.append(price)                 # today's close enters H7 immediately
    h7, h90 = h(series, 7), h(series, 90)
    cap = h7 * (1 + CAP_PCT)
    over_cap, over_h90 = price > cap, price > h90
    skip = over_cap and over_h90
    skips += skip
    tag = "  <-- savaite 2" if i == 8 else ""
    print(f"{i:<7}{price:>8.4f}{h7:>9.4f}{cap:>9.4f}{h90:>9.4f}"
          f"{('TAIP' if over_cap else 'ne'):>7}{('TAIP' if over_h90 else 'ne'):>7}"
          f"{'':>4}{'SKIP' if skip else 'PERKA'}{tag}")

print(f"\nis viso praleista: {skips} is {len(jump_week+second_week)} dienu")

# Boundary check: how high would the FIRST jump day have to be to trigger a skip?
s6 = sum(history[-6:])
thr = (1 + CAP_PCT) * s6 / (7 - (1 + CAP_PCT))
print(f"\nribos skaiciavimas pirmai sokio dienai:")
print(f"  paskutiniu 6 uzdarymu suma = {s6:.4f}")
print(f"  skip'as prasideda nuo      = {thr:.4f}  (+{(thr/history[-1]-1)*100:.1f}% nuo {history[-1]:.4f})")
