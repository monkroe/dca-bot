# DCA-R02: regime signal evaluation — specification and backtest design

**Status:** PROPOSED. A research specification, not an implementation
authorization. It changes no production code, no `dca_settings` or
`dca_orders` value and no live order. The backtest script it describes is
written only after Roberto accepts this document, and runs read-only.

**Date:** 2026-11-03 (Chicago)
**Roadmap:** `research/DCA_FUTURE_ROADMAP.md`. This document answers DCA-R02
("does the H7/H30/H90 combination add value over simpler rules?") for one
concrete candidate, an H30 dip bonus. Variable sizing as a production rule
(DCA-R03) and budget, reserve and carryover (DCA-R04) are **not approved**.
Their open questions are listed in section 9 and are not designed here.
**Reviews:** GT audit 2026-11-03: the current logic PASS, R02 research
RECOMMENDED, R03/R04 NOT APPROVED.

No monetary amounts appear in this document. Every amount is expressed in the
base unit `U` (one order's `base_quote_amount`). The bonus size is not set
here.

## 1. Question and hypothesis

**Question.** If KAS is bought in larger size when its price is below the
30-day trend, does that accumulate more KAS for the same capital than the
current policy, without draining the USD reserve or causing base buys to be
missed?

**Hypothesis H-R02 (written before any test run).** Over the test period
(section 6), variant C accumulates more KAS than both variant B and the
equal-spend control A′, at the same capital, fees and horizon. It must do so
without a worse minimum USD reserve than B beyond the bound fixed in
section 7, and without more missed base buys than B. It must also hold for
most of the pre-registered `d` grid, not only at its best point.

The null result ("no advantage") and `INSUFFICIENT EVIDENCE` are acceptable
outcomes. The roadmap (§4) forbids reporting only the most profitable
example.

## 2. Current production logic (verified against `main` `3081e4f`)

- **The cap is a veto.** `cap_decision()` skips a buy only when
  `mid > ref × (1 + cap_pct)` and, with the guard on, `mid > H90`. It never
  increases a buy. The documented production values are `cap_mode='ohlc_h7'`,
  `cap_pct=0.20` and `cap_require_above_h90=true` (`STATUS.md` in
  robert-os-hub, verified by SELECT on 2026-07-26; not re-read for this
  document).
- **There is no variable sizing.** `execute_pair()` sizes every event by
  `base_quote_amount`, and the DP-5 fallback is sized from the remaining base
  budget.
- **`bonus_quote_amount` sizes nothing.** It is read only by the two
  balance-message paths: the low-balance warning (`kraken_sync.py`) and the
  remaining-balance line after a fill (`_remaining_after_buy_line`). Both add
  it to the daily burn.
- **Finding (GT):** an optional bonus is therefore counted as a mandatory
  daily expense in both messages. This is an R04 question (section 9).
- **Technical debt (GT):** `execute_pair()` takes a `daily_burn` argument and
  does not use it.
- **H30 and H90 in production** come from `build_daily_metrics()`, whose last
  daily candle is the current, incomplete UTC day. The backtest does not copy
  this (section 4). Whether the live signal should change to completed days is
  an R03 question.

## 3. Variants

All three variants use the same capital, funding schedule, fee model, decision
times and horizon (section 5).

| Variant | Rule |
|---|---|
| **A. Plain DCA** | Buy `1 U` at every scheduled event. No cap, no bonus. |
| **B. Current policy** | Buy `1 U` at every event unless `cap_decision()` vetoes it. The cap parameters are the documented production values above; the guard is on. |
| **C. Bonus policy** | B, plus a bonus of `b U` at an event when the cap does not veto it and `price < H30 × (1 − d)`. |

Rules for C:

- The cap veto comes first, as in the v2.3 hierarchy. A vetoed event buys
  nothing, bonus included.
- The base buy is always funded before the bonus. If the cash cannot cover
  `1 U + b U`, the bonus is dropped and the event buys `1 U`. If it cannot
  cover `1 U`, the event is an unfunded base buy (section 7).
- **H90 − 10 % is classification and telemetry only.** Every event is
  labelled by price class (section 7), and results are reported per class. The
  class never changes a size or a veto.

**Control A′ (equal spend).** Plain DCA whose per-event size is C's total spend
divided by the number of events. This answers whether C's *timing* helped,
rather than C simply spending more. A′ is sized after the fact, so it is a
comparison control, not a tradable policy, and is reported as such.

The bonus multiple `b` is a ratio of `U`. Its grid is chosen by Roberto and
frozen in the pre-registration block (section 6) before the test period is
run. This document does not set it.

## 4. Data and time semantics (no lookahead)

- **Decision instant.** Each production order's target time in Chicago,
  converted to UTC per date (DST included), once per day per order. The
  backtest takes the order schedule as an input parameter; it does not read
  `dca_orders`.
- **Decision price.** The close of the last *completed* 4-hour Kraken candle
  that ends at or before the decision instant. The live bot uses the mid at
  that moment, which the backtest cannot see; this proxy is known at the time
  and never later.
- **Signal inputs.** H7, H30 and H90 are SMAs of Kraken daily closes of
  completed UTC days only, strictly before the UTC date of the decision
  instant. The incomplete current day is never used (GT). Gaps are
  forward-filled as in `ohlc.py` (`ffill_gaps`), and every filled day is
  counted in the output.
- **Pair.** `KASUSD`, the pair the DCA bot trades.
- **Source.** Committed Kraken daily (1440) and 4-hour (240) candles.
  Candidate sources, in order of preference:
  1. a snapshot of `dca_ohlc_history`, the fail-closed preserved candles. This
     needs a read-only export with Roberto's word;
  2. Kraken's published historical OHLCVT files, the same origin
     `ohlc_preserve.py` uses;
  3. the public OHLC endpoint, which serves only the most recent 720 candles
     per interval: about 120 days of 4-hour candles. This is likely too short
     for section 6.

  The chosen input file is pinned by SHA-256 in the output. Nothing is fetched
  during a scored run.
- **Warm-up.** At least 90 completed days before the first scored event, so
  that H90 exists on day one.
- **Lookahead test (required).** Changing any candle after a decision instant
  must leave every decision at or before that instant unchanged.

## 5. Equal capital, fees and horizon

- **Horizon.** The same first and last scored event for all variants within a
  period.
- **Capital and funding.** All variants start with the same cash and receive
  the same simulated deposits on the same dates. Two funding scenarios are
  pre-registered, and every result is reported under both:
  - **F1 (one lump):** all of the period's capital at the start;
  - **F2 (weekly):** equal weekly deposits.

  Capital is sized in `U`: `K × (events in the period) × 1 U`, where `K ≥ 1` is
  a headroom factor frozen in section 6. Deposits are simulation conditions,
  not a plan for real transfers.
- **Fees and execution.**
  - One all-in cost model for every variant: decision price × (1 + fee rate +
    impact allowance).
  - The fee rate defaults to the production maker fee rate. A sensitivity run
    uses the taker fee rate.
  - The impact allowance is a fixed number of basis points, frozen in
    section 6.
  - The bonus leg pays the same rate as the base leg.
  - No maker fill is simulated (section 7).
- **End of horizon.** Leftover USD is reported at face value and never
  converted to KAS for the primary metric.

## 6. Pre-registration and periods

- History is split chronologically into a **calibration period P1** and a
  separate **test period P2** that follows it. Each has its own warm-up
  (section 4), taken from the days just before it, so P2's warm-up may overlap
  the end of P1.
- **Before P2 is run,** a pre-registration block is committed (its commit hash
  is the record). It fixes:
  - the `d` grid (proposed: 0 %, 2.5 %, 5 %, 10 %);
  - the `b` grid (Roberto's choice);
  - the cap parameters;
  - `K`, the funding scenarios, the fee rates and the impact allowance;
  - the order schedule;
  - the P1 and P2 boundaries;
  - the input file hash;
  - the decision rule in section 7 and its reserve bound.
- P1 may be run any number of times to debug the tool and to look at
  behaviour. P2 is run once with the frozen block. A change after P2 has been
  seen makes a new pre-registration and a new, later test period; the old P2
  result is kept and marked `SUPERSEDED`.
- If the available history cannot give P1 and P2 at least 180 scored days
  each after warm-up, the result is `INSUFFICIENT EVIDENCE`, and no claim is
  made.

## 7. Metrics

Each metric is reported for each variant, `d`, `b` and funding scenario, in
P1 and P2 separately.

| Metric | Definition |
|---|---|
| **KAS accumulated** | Sum of filled KAS (primary). |
| **Average all-in cost per KAS** | USD spent including the cost model, divided by KAS. |
| **USD spent / USD left** | Totals at the end of the horizon. |
| **Worst USD reserve** | The minimum cash balance over the horizon, in `U` and in days of base buys. Also the date it occurred. |
| **Missed base buys** | Count and dates, split by reason: `cap_veto` (by design, B and C), `unfunded` (cash below `1 U`). A missed base buy caused by an earlier bonus is reported separately as `unfunded_after_bonus`. |
| **Stuck orders** | In the simulation: events that could not be funded in full (base or bonus dropped). The exchange-side part (resting, unfilled or `manual_required` maker legs) cannot be simulated from candles. It is reported from production telemetry (`dca_executions` statuses, read with Roberto's word) as an observed rate applied equally to every variant, and marked as such. |
| **Sensitivity to `d`** | All of the above for every grid point. An advantage that exists at one `d` only is reported as not robust. |
| **Bonus activity** | The number of bonus events, the longest run of consecutive bonus days, and the share of bonus spend per price class. |
| **Per class** | Each metric split by price class: `below_H90_minus_10` (price < H90 × 0.90), `below_H30_d` (price < H30 × (1 − d), and not the first class), `at_or_above_H30_d`, and `cap_vetoed`. |

**Decision rule (frozen in section 6).** H-R02 is supported only when every
one of these holds on P2, under both funding scenarios, for at least three of
the four `d` grid points:

- C accumulates more KAS than B **and** more than A′;
- C's worst reserve is not lower than B's by more than the bound frozen in
  section 6;
- C has no more `unfunded` and `unfunded_after_bonus` base buys than B.

Anything less is `NO ADVANTAGE`. Missing or inadequate data gives
`INSUFFICIENT EVIDENCE`. No result authorizes a live change: DCA-R03 and
DCA-R04 still need their own approval.

## 8. The backtest tool (design only)

- **Location and form.** `tools/r02_backtest.py`. Stdlib-only Python,
  read-only, no network during a scored run, no database access, no orders.
  It reads one pinned input file and writes one result file plus a summary on
  stdout.
- **Reuse of production code.** It imports `cap_decision()` from
  `src/kraken_run.py`, so the veto is the production function itself. The
  SMA and forward-fill helpers come from `src/ohlc.py`, applied to
  completed-day series only (section 4).
- **Determinism.** The same input file and pre-registration block always give
  byte-identical output, and the output records both hashes and the git
  commit.
- **Tests,** run under `test.sh` like the other pure tests:
  - no lookahead (section 4);
  - completed-day SMA: the incomplete day never enters H30 or H90;
  - DST at the decision instant;
  - the veto reproduces known production rows (from fixtures, not the
    database);
  - funding order: base before bonus, bonus dropped first;
  - equal capital and horizon across variants;
  - A′ spends exactly C's total;
  - per-class labels never change a size.

## 9. Open questions for DCA-R03 and DCA-R04 (not designed, not approved)

These are recorded so that R02's result can feed them later. No code, no
schema and no setting follows from this list.

**R03: variable sizing as a production rule**

- Q1. The live signal input: should production H30 and H90 move to completed
  UTC days only, as in the backtest, or keep the incomplete current day? The
  two give different decisions near day boundaries.
- Q2. The decision price: the live mid at the decision instant against the
  backtest's 4-hour proxy, and how large the difference is in practice.
- Q3. Interaction with the DP-5 fallback (I7 sizing from the remaining budget)
  and with re-peg: is the bonus part of the event's total target, or a
  separate leg with its own identity?
- Q4. Whether `bonus_quote_amount` is the right carrier for the bonus size, or
  whether a ratio of base is safer.

**R04: budget, limits, reserve and off semantics**

- Q5. Atomic daily limit: at most one bonus per order per Chicago day. How is
  it enforced against concurrent runs, retries, fallback and re-peg, and is
  it counted on requested or on filled amounts (partial fills)?
- Q6. Atomic monthly limit: a Chicago calendar month budget for bonus spend.
  How is it enforced atomically across runs, and what happens at a month
  boundary while an event is in flight?
- Q7. Reserve: a bonus only if at least N days of base buys remain afterwards.
  Which balance is authoritative (spendable vs held in resting orders, and how
  fresh the snapshot is), and is the reserve checked before or after fees?
- Q8. Off semantics: what `off` means exactly. Is it zero bonus with no other
  effect? Does an event already in flight finish its bonus? Is there a
  `shadow` mode that computes and logs the bonus without sizing it? And is
  `off` a kill switch that takes effect at the next event without a deploy?
- Q9. Burn accounting (GT finding): an optional bonus should not count as
  mandatory daily burn in the low-balance warning and the remaining-buys line.
  What should those lines show instead?
- Q10. The unused `daily_burn` argument of `execute_pair()` (technical debt):
  remove it, or give it a defined use.
- Q11. Telemetry for a live rule: the written `requested_quote_amount_bonus`,
  a reason code per event, and the signal reference used, so that every bonus
  decision can be rebuilt from the row (as `cap_price` is today).

## 10. Owner steps (each needs Roberto's word)

1. Accept or change this specification.
2. Choose the input source (section 4). Use of `dca_ohlc_history` needs a
   read-only export.
3. Fix the pre-registration block, including the `b` grid.
4. Then: the tool and its tests, in a separate PR. P1 runs, then the
   pre-registration commit, then one P2 run. A result entry is added to the
   roadmap register.
