# DCA-R02: regime signal evaluation — specification and backtest design (v2.1)

**Status:** PROPOSED v2.1. A research specification, not an implementation
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
RECOMMENDED, R03/R04 NOT APPROVED. GT review of `21b22b2`: REQUEST CHANGES,
answered by this version.

No monetary amounts appear in this document. Every amount is expressed in the
base unit `U` (one order's `base_quote_amount`). The bonus grid below is a
research grid for the backtest, not a bonus size for production.

**Changes from v2 (GT review of `d3381a6`: MINOR, all nine points PASS):**

- Roberto's decision: P2 form 1, one `b*` selected on P1, from the `b` grid
  0.5 U, 1 U, 2 U. Form 2 is dropped.
- M1: the `b*` selection is now a single fixed procedure (section 6). It uses
  one conservative score across both funding scenarios, requires the criteria
  at three of the four `d` values, breaks ties to the smaller `b`, and selects
  nothing when no `b` qualifies, in which case P2 is not run. P2 data cannot
  affect the formula or `b*`.
- M2: an `INVALID` A′ gives `INSUFFICIENT EVIDENCE` for that comparison, not
  `NO ADVANTAGE` (sections 3 and 7).

**Changes from v1 (GT review of `21b22b2`):**

1. No backward fill from a later price. A period without an earlier price is
   not scored (section 4).
2. 4-hour candles are keyed by their start. A candle is used only once it has
   closed, and a missing candle has a pre-decided rule (section 4).
3. A′ is a retrospective control. Its spend must equal C's, and the spec says
   what happens if it does not (section 3).
4. One P2 rule covers the whole `b` grid (0.5 U, 1 U, 2 U). No selection
   after P2 (section 6).
5. `unfunded_after_bonus` is measured by a control replay without the bonus
   (section 7).
6. Stuck orders: the historical rate is a context metric only and is never
   applied to the simulation (section 7).
7. New secondary metric: end value (section 7).
8. Q5: one bonus per pair per Chicago day, protected against a repeated event,
   fallback and re-peg (section 9).
9. Data: Kraken historical OHLCVT files for 240 and 1440 minutes, validated
   before P1 and P2 (section 4).

## 1. Question and hypothesis

**Question.** If KAS is bought in larger size when its price is below the
30-day trend, does that accumulate more KAS for the same capital than the
current policy, without draining the USD reserve or causing base buys to be
missed?

**Hypothesis H-R02 (written before any test run).** Over the test period
(section 6), variant C accumulates more KAS than both variant B and the
retrospective equal-spend control A′, at the same capital, fees and horizon.
It must do so without a worse minimum USD reserve than B beyond the bound
fixed in section 6, and without more missed base buys than B. It must also
hold for most of the pre-registered `d` grid, not only at its best point.

The null result (`NO ADVANTAGE`), `INSUFFICIENT EVIDENCE` and `NO CANDIDATE`
(section 6) are acceptable outcomes. The roadmap (§4) forbids reporting only the most profitable
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
- **Finding (GT).** An optional bonus is counted as a mandatory daily expense
  in both messages. This is an R04 question (section 9).
- **Technical debt (GT).** `execute_pair()` takes a `daily_burn` argument and
  does not use it.
- **H30 and H90 in production** come from `build_daily_metrics()`, whose last
  daily candle is the current, incomplete UTC day. The backtest does not copy
  this (section 4). Whether the live signal should change to completed days is
  an R03 question.
- **Finding (this review).** `ffill_gaps()` in `src/ohlc.py` seeds the first
  day of a series from the latest close in the 14 days before it. If there is
  none, it falls back to the **earliest close in the input**, which can be
  later than the day it fills. With production's 220-day fetch this path is
  practically never taken, so it changes no live decision today. It is
  recorded here, not changed. The backtest does not use this seeding
  (section 4).

## 3. Variants

All variants use the same capital, funding schedule, fee model, decision times
and horizon (section 5).

| Variant | Rule |
|---|---|
| **A. Plain DCA** | Buy `1 U` at every scheduled event. No cap, no bonus. |
| **B. Current policy** | Buy `1 U` at every event unless `cap_decision()` vetoes it. The cap parameters are the documented production values above; the guard is on. |
| **C. Bonus policy** | B, plus a bonus of `b U` at an event when the cap does not veto it and `price < H30 × (1 − d)`. |

Rules for C:

- The cap veto comes first, as in the v2.3 hierarchy. A vetoed event buys
  nothing, bonus included.
- **At most one bonus per pair per Chicago day.** It goes to the first eligible
  event of that day. Later events that day buy at most `1 U`, matching the
  production requirement in Q5 (section 9).
- The base buy is always funded before the bonus. If the cash cannot cover
  `1 U + b U`, the bonus is dropped and the event buys `1 U`. If it cannot
  cover `1 U`, the event is an unfunded base buy (section 7).
- **H90 − 10 % is classification and telemetry only.** Every event is labelled
  by price class (section 7), and results are reported per class. The class
  never changes a size or a veto.

**A′: retrospective equal-spend control.** A′ is plain DCA whose per-event size
is C's actual total USD spend (fees included) divided by the number of
scheduled events. It answers whether C's *timing* helped, rather than C simply
spending more.

- It is computed after C has run, from C's result, so it uses hindsight. It is
  a comparison control, never a tradable policy, and every report marks it
  `RETROSPECTIVE`.
- **The spend-equality check.** A′'s actual spend must equal C's within a
  relative tolerance of 1e-9 (floating point only; the backtest uses
  continuous volume, with no lot rounding). This is checked for every scenario,
  above all F2, where weekly funding can leave A′ unable to fund an event that
  C funded earlier.
- **If the check fails** for a scenario (for example, F2 liquidity leaves A′
  unable to fund an event), A′ is marked `INVALID` there. The comparison of C
  with A′ in that scenario is then `INSUFFICIENT EVIDENCE`, not
  `NO ADVANTAGE`: an invalid control proves nothing either way. A′ is never
  rescaled or re-sized to force equality, and the shortfall and its dates are
  reported. How this enters the overall verdict is defined in section 7.

## 4. Data and time semantics (no lookahead)

**Source (decided).** Kraken's historical OHLCVT files for `KASUSD`, at 240
and 1440 minutes, the same origin `ohlc_preserve.py` uses. The files are
obtained outside the scored run and pinned by SHA-256; nothing is fetched
during a scored run. `KASUSD` is the pair the DCA bot trades.

**Data validation (before any P1 or P2 run).** A read-only report is
committed with the pre-registration block. It covers, for each file:

- the SHA-256 of the file;
- the first and last candle;
- the number of candles against the number expected for that range;
- every missing timestamp, the longest run of consecutive missing candles,
  and their dates;
- duplicate timestamps, which are a hard failure;
- timestamps not on the interval grid, which are a hard failure;
- out-of-order rows, which are a hard failure;
- as information only: for each UTC day, the 1440 close against the close of
  the last 240 candle of that day.

A hard failure, or a missing-candle rate above the pre-registered limit,
gives `INSUFFICIENT EVIDENCE` for any period it touches.

**Candle timestamps.** In Kraken's OHLCVT files the timestamp is the candle's
**start**. A candle covers `[start, start + interval)` and is complete only
from `start + interval` on.

**Decision instant.** Each production order's target time in Chicago,
converted to UTC per date (DST included), once per day per order. The order
schedule is an input parameter; the backtest does not read `dca_orders`.

**Decision price.**

- The close of the 4-hour candle whose `start + 4 h` is the latest at or
  before the decision instant. That is the candle that closed most recently,
  and it is used only if `start + 4 h ≤ decision instant`.
- **Missing candle, pre-decided.** If the candle that should have closed in
  the 4 hours before the decision instant is absent, the event has no price.
  It is scored as `no_price` for **every** variant: no buy, no bonus, no
  veto. No older candle and no daily close is substituted. `no_price` events
  are counted per period. If they exceed the pre-registered limit, the result
  is `INSUFFICIENT EVIDENCE`.
- **Test.** A candle with `start + 4 h > decision instant` never supplies a
  price. Changing that candle's close changes no decision.

**Signal inputs.**

- H7, H30 and H90 are SMAs of 1440-minute closes of completed UTC days only,
  that is, days with `start + 24 h ≤ decision instant`. The incomplete current
  day is never used.
- **Gaps** are forward-filled from the latest earlier close only. Each fill is
  counted and reported. A run of consecutive filled days longer than the
  pre-registered limit makes the affected events `no_signal`, scored for every
  variant like `no_price`.
- **No backward fill.** A day is never filled from a later close. Days before
  the first real close in the file have no value. An SMA whose window reaches
  a day with no value does not exist, and an event whose H7, H30 or H90 does
  not exist is not scored. In particular, a period can start only once 90
  completed days with real or forward-filled values precede it.
- The backtest implements this fill itself. It does not call production
  `ffill_gaps()`, because of the seeding finding in section 2.

**Lookahead test (required).** Changing any candle that is not complete at a
decision instant leaves every decision at or before that instant unchanged.

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
  - The fee rate defaults to the production maker fee rate; a sensitivity run
    uses the taker fee rate.
  - The impact allowance is a fixed number of basis points, frozen in
    section 6.
  - The bonus leg pays the same rate as the base leg.
  - No maker fill, resting order or cancellation is simulated (section 7).
- **End of horizon.** Leftover USD is reported at face value. It never enters
  the primary metric. It enters only the secondary end value (section 7).

## 6. Pre-registration, periods and the P2 rule

- History is split chronologically into a **calibration period P1** and a
  separate **test period P2** that follows it. Each has its own warm-up
  (section 4), taken from the days just before it, so P2's warm-up may overlap
  the end of P1.
- **Before P2 is run,** a pre-registration block is committed; its commit hash
  is the record. It fixes:
  - the `d` grid (proposed: 0 %, 2.5 %, 5 %, 10 %);
  - the `b` grid: 0.5 U, 1 U, 2 U;
  - the `b*` selection procedure (below), unchanged;
  - the cap parameters;
  - `K`, the funding scenarios, the fee rates and the impact allowance;
  - the order schedule;
  - the P1 and P2 boundaries;
  - the file hashes and the data-validation report;
  - the limits for missing candles, `no_price`, `no_signal` and the longest
    forward-fill run;
  - the reserve bound and the decision rule in section 7.
- P1 may be run any number of times to debug the tool and to look at
  behaviour. P2 is run once with the frozen block. A change after P2 has been
  seen makes a new pre-registration and a new, later test period; the old P2
  result is kept and marked `SUPERSEDED`.
- If the available history cannot give P1 and P2 at least 180 scored days
  each after warm-up, the result is `INSUFFICIENT EVIDENCE` and no claim is
  made.

**The P2 rule: form 1, one `b*` selected on P1 (Roberto, 2026-11-03).**

The selection uses P1 data only. It is a fixed procedure, committed in the
pre-registration block before P2 is run, and nothing in it may change after
that.

1. **Eligibility of one grid point.** For a given `b`, `d` and funding
   scenario `F`, the point is eligible on P1 when both of these hold:
   - C's worst reserve is not lower than B's by more than the reserve bound;
   - C has no more `unfunded` plus `unfunded_after_bonus` base buys than B.
2. **Qualification of `b`.** `b` qualifies when at least three of the four `d`
   values are eligible in **both** F1 and F2. The same `d` must be eligible in
   both scenarios to count.
3. **Conservative score.** For a qualifying `b`, the score is the minimum,
   over both funding scenarios and over the `d` values counted in step 2, of
   `KAS accumulated by C / KAS accumulated by B`. Each ratio uses the same
   scenario for C and B. The minimum, not an average, is used, so one weak
   scenario or `d` value decides.
4. **Selection.** `b*` is the qualifying `b` with the highest score.
   - **Ties** (scores equal within a relative tolerance of 1e-9) go to the
     smaller `b`.
5. **No candidate.** If no `b` qualifies, no `b*` is selected and P2 is not
   run. The result is `NO CANDIDATE`: H-R02 is not supported. P2's data stay
   unseen, so a later, separately pre-registered study may still use them.
6. **Commit.** `b*`, its score and every P1 grid result are committed before
   P2 is run. On P2, only `b*` is judged, by the decision rule in section 7.
   The other two `b` values are shown in the P2 report for description only
   and cannot support H-R02.
7. **Isolation from P2.** The selection reads only P1 data. A test changes
   every P2 candle and checks that the score formula, the scores and `b*` stay
   identical.

Choosing a `b` or a `d` after seeing P2 is not allowed. Every grid point's P2
result is reported.

## 7. Metrics and decision rule

Each metric is reported for each variant, `d`, `b` and funding scenario, in
P1 and P2 separately.

| Metric | Definition |
|---|---|
| **KAS accumulated** | Sum of filled KAS. **Primary.** |
| **End value** (secondary) | KAS accumulated × the last decision price of the period, plus USD left. It depends on one end price, so it is reported next to the primary metric and never decides alone. |
| **Average all-in cost per KAS** | USD spent including the cost model, divided by KAS. |
| **USD spent / USD left** | Totals at the end of the horizon. |
| **Worst USD reserve** | The minimum cash balance over the horizon, in `U` and in days of base buys, with its date. |
| **Missed base buys** | Count and dates, by reason: `cap_veto` (by design, B and C), `unfunded`, `unfunded_after_bonus`, and the data reasons `no_price` and `no_signal` (equal for all variants). |
| **Sensitivity to `d`** | All of the above for every grid point. An advantage that exists at one `d` only is reported as not robust. |
| **Bonus activity** | The number of bonus events, the longest run of consecutive bonus days, and the share of bonus spend per price class. |
| **Per class** | Each metric split by price class: `below_H90_minus_10` (price < H90 × 0.90), `below_H30_d` (price < H30 × (1 − d), and not the first class), `at_or_above_H30_d`, and `cap_vetoed`. |
| **Stuck orders (context only)** | The historical rate of exchange-side problems in production (resting, unfilled, `manual_required` maker legs; from `dca_executions` statuses, read only with Roberto's word). Shown as context next to the results. **It is never applied to the simulation** and never changes a simulated buy, cost or metric. |

**`unfunded_after_bonus`, by control replay.**

- For each C run, the same path is replayed with the bonus set to zero, with
  everything else identical: funding, prices, cap, decision instants. By
  construction this replay is B, and a test asserts the identity.
- A base buy that is unfunded in C but funded in the replay is
  `unfunded_after_bonus`.
- A base buy unfunded in both is `unfunded`.

**Decision rule (frozen in section 6).** It is applied on P2 to `b*` only.
For each funding scenario and each `d` grid point there are four checks:

1. C accumulates more KAS than B;
2. C accumulates more KAS than A′. This check is `INSUFFICIENT EVIDENCE` when
   A′ is `INVALID` in that scenario;
3. C's worst reserve is not lower than B's by more than the reserve bound;
4. C has no more `unfunded` plus `unfunded_after_bonus` base buys than B.

A grid point **passes** when all four checks pass, in both funding scenarios.

**Verdict:**

- **Supported:** at least three of the four `d` grid points pass.
- **`INSUFFICIENT EVIDENCE`:** three passes cannot be reached, but would have
  been reached if every `INSUFFICIENT EVIDENCE` check had passed. An invalid
  A′ therefore never turns into `NO ADVANTAGE`. The same verdict applies when
  data are missing or inadequate (sections 4 and 6).
- **`NO ADVANTAGE`:** three passes cannot be reached even if every
  `INSUFFICIENT EVIDENCE` check had passed. In other words, the result fails
  on checks that were validly made.
- **`NO CANDIDATE`:** no `b*` was selected (section 6), so P2 is not run.

No result authorizes a live change: DCA-R03 and DCA-R04 still need their own
approval.

## 8. The backtest tool (design only)

- **Location and form.** `tools/r02_backtest.py` plus a separate read-only
  validator, `tools/r02_validate_ohlcvt.py`. Stdlib-only Python, no network,
  no database access, no orders. It reads the pinned files and writes one
  result file plus a summary on stdout.
- **Reuse of production code.** It imports `cap_decision()` from
  `src/kraken_run.py`, so the veto is the production function itself.
  Production SMA math is reused only where it is identical; candle selection
  and gap filling are the backtest's own, as defined in section 4.
- **Determinism.** The same files and pre-registration block always give
  byte-identical output. The output records both hashes and the git commit.
- **Tests,** run under `test.sh` like the other pure tests:
  - no lookahead;
  - no backward fill, and no scored event before 90 completed days with values;
  - 4-hour candles keyed by start, an unclosed candle never used, and the
    `no_price` rule;
  - completed-day SMA;
  - DST at the decision instant;
  - the veto reproduces known production rows (from fixtures, not the
    database);
  - one bonus per pair per Chicago day;
  - base funded before bonus;
  - the control replay equals B;
  - A′'s spend equality, and its `INVALID` path leading to
    `INSUFFICIENT EVIDENCE`, not `NO ADVANTAGE`;
  - the `b*` selection: eligibility, the three-of-four rule across both
    scenarios, the minimum score, ties to the smaller `b`, `NO CANDIDATE`, and
    no change to `b*` when P2 candles change;
  - equal capital and horizon across variants;
  - per-class labels never change a size;
  - the validator's hard failures: duplicates, off-grid timestamps, order.

## 9. Open questions for DCA-R03 and DCA-R04 (not designed, not approved)

These are recorded so that R02's result can feed them later. No code, no
schema and no setting follows from this list.

**R03: variable sizing as a production rule**

- **Q1.** The live signal input: should production H30 and H90 move to
  completed UTC days only, as in the backtest, or keep the incomplete current
  day? The two give different decisions near day boundaries.
- **Q2.** The decision price: the live mid at the decision instant against the
  backtest's 4-hour proxy, and how large the difference is in practice.
- **Q3.** Interaction with the DP-5 fallback (I7 sizing from the remaining
  budget) and with re-peg: is the bonus part of the event's total target, or a
  separate leg with its own identity?
- **Q4.** Whether `bonus_quote_amount` is the right carrier for the bonus
  size, or whether a ratio of base is safer.

**R04: budget, limits, reserve and off semantics**

- **Q5. Requirement: at most one bonus per pair per Chicago day.** The bonus
  must never be granted twice for the same pair and day, through any of these
  paths:
  - **a repeated event:** a second cron cycle, a retry or takeover with a
    rotated `cl_ord_id`, a `--force` run, or two orders on the same pair;
  - **the DP-5 fallback:** the fallback continues the same event's remaining
    target and never adds a second bonus;
  - **re-peg:** a new generation of the same maker execution carries the bonus
    already granted, never a new one.

  Open: how the grant is recorded atomically (for example a unique key on pair
  and Chicago date), and whether it counts as granted when requested or only
  when filled (partial fills).
- **Q6. Atomic monthly limit.** A Chicago calendar-month budget for bonus
  spend: how it is enforced atomically across runs, and what happens at a month
  boundary while an event is in flight.
- **Q7. Reserve.** A bonus only if at least N days of base buys remain
  afterwards. Which balance is authoritative (spendable vs held in resting
  orders, and how fresh the snapshot is), and is the reserve checked before or
  after fees?
- **Q8. Off semantics.** What `off` means exactly: zero bonus with no other
  effect? Does an event already in flight finish its bonus? Is there a
  `shadow` mode that computes and logs the bonus without sizing it? Is `off` a
  kill switch that takes effect at the next event without a deploy?
- **Q9. Burn accounting (GT finding).** An optional bonus should not count as
  mandatory daily burn in the low-balance warning and the remaining-buys line.
  What should those lines show instead?
- **Q10. The unused `daily_burn` argument of `execute_pair()` (technical
  debt).** Remove it, or give it a defined use.
- **Q11. Telemetry for a live rule.** The written `requested_quote_amount_bonus`,
  a reason code per event and the signal reference used, so that every bonus
  decision can be rebuilt from the row, as `cap_price` allows today.
- **Q12. The `ffill_gaps()` seeding finding (section 2).** Should production
  refuse to seed from a later close, as the backtest does?

## 10. Owner steps (each needs Roberto's word)

1. Accept or change this specification.
2. Provide the Kraken historical OHLCVT files for `KASUSD` (240 and 1440).
3. Approve the data-validation report and fix the pre-registration block. The
   P2 rule form is decided: form 1.
4. Then the validator, the tool and their tests, in a separate PR: P1 runs,
   then the pre-registration commit, then one P2 run. A result entry is added
   to the roadmap register.
