# DCA-R02: data validation report and data registration (v1)

**Registracijos būsena: NEUŽBAIGTA (tik duomenų dalis).**
Registration status: INCOMPLETE (data part only). This document fixes the input
data and the data window. The other pre-registration parameters are listed in
section 8. Several are still TBD and must be fixed before P2 is run.

**Status:** PROPOSED v1, for GT review. A documentation record only. It
authorizes no validator or backtest code, no database change, no bonus size
and no R03/R04 work; each of these needs Roberto's separate word.

**Date:** 2026-11-03 (Chicago)
**Specification:** [`DCA_R02_BACKTEST_SPEC.md`](DCA_R02_BACKTEST_SPEC.md)
(v2.1, merged in PR #1, main `4ce54e0`). Section numbers marked "spec §" refer
to that specification.
**Linear:** ROB-43.

## 1. Sources and SHA-256

| File | SHA-256 | Role |
|---|---|---|
| `Kraken_OHLCVT_Full.zip` | `bc273e9027470b07d790a7fa4df9bb43f0dfc527c20c6ecae48402e123e7b1c7` | Kraken's full historical OHLCVT archive, downloaded 2026-11-03 at about 09:35 CST. Data run to 2026-09-30. |
| `KASUSD_240.csv` (from the archive) | `60988896c919ca9fac72cd3d7f3a564725fd58d5a296fbf5e019687b411b19bd` | **Primary** source, 4-hour candles |
| `KASUSD_1440.csv` (from the archive) | `b9d884a70d03b87376e1dfa3406a15fa481cd1321fe332356efaed958c606619` | **Primary** source, daily candles |
| `KASUSD_1440_api_20261103.csv` | `13aa0d6bd4f69caeb6a9db1e465680ea765bd5745ba512f26b1580db900b6386` | Backup and cross-check only: daily candles from the public REST API (`OHLC`, `interval=1440`, no key), taken 2026-11-03 at about 09:21 CST with Roberto's permission |
| `raw_kasusd_1440.json` | `14834034d26c8ef1a21aa0314d6e925cad66f140aa5d358259029a7d2d4f2e3b` | The raw API response behind the CSV above |

**Who verified what:**

- **The archive (Nova, 2026-11-03).** The SHA-256 of each of its five download
  parts matched Kraken's `OHLCVT_Full_PARTS_SHA256SUMS.txt`, and the joined
  `Kraken_OHLCVT_Full.zip` matched Kraken's published SHA-256. Claude did not
  receive the archive and did not re-verify it.
- **The four extracted and API files (Claude, 2026-11-03).** Their SHA-256s
  were recomputed from the files themselves and match the values above. The
  checks in sections 2 to 4 were run read-only with one-off scripts outside the
  repository.

**Storage.** All files are kept in Roberto's Google Drive, folder
`KASUSD_OHLCVT_2026-11-03`. The data files are **not committed** to this
repository. The hashes above are the binding record: a run must use files whose
SHA-256 equals them.

## 2. Format and coverage

**Archive files.**

- ASCII, no BOM, no CR, final newline, no header row.
- Seven columns: `time, open, high, low, close, volume, trades`. There is no
  `vwap` column, and the specification does not use one.
- `time` is the candle **start** in UTC epoch seconds (spec §4). The data
  confirm this. KAS trading opened on Kraken 2024-11-19 at 14:00 UTC, and the
  first 4-hour candle is stamped 12:00, so it covers 12:00–16:00. If the stamp
  were the candle end, that candle would cover 08:00–12:00, before any
  trading.

| File | Rows | First | Last | Expected | Missing |
|---|---|---|---|---|---|
| `KASUSD_240.csv` | 4 082 | 2024-11-19 12:00 UTC | 2026-09-30 20:00 UTC | 4 083 | 1 (section 3) |
| `KASUSD_1440.csv` | 681 | 2024-11-19 | 2026-09-30 | 681 | 0 |

**Hard checks of spec §4, on both archive files: all PASS.**

- 0 duplicate timestamps.
- 0 timestamps off the interval grid.
- Timestamps strictly increasing.
- Every row satisfies `low ≤ open ≤ high` and `low ≤ close ≤ high`.
- No negative or zero volume.
- `trades` is a non-negative integer in every row.

**Missing-candle rate:** one 4-hour candle of 4 083 (about 0.02 %); no daily
candle.

**API backup files.**

- `KASUSD_1440_api_20261103.csv`: header `time,open,high,low,close,vwap,volume,count`,
  715 rows from 2024-11-19 to 2026-11-03.
  - 0 gaps, 0 duplicates, every stamp at 00:00 UTC, strictly increasing, OHLC
    valid.
- `raw_kasusd_1440.json`: `error` is empty, and all 715 rows equal the CSV
  field by field, compared as text.
  - `last` is `1793577600` (2026-11-02 00:00 UTC).
- **The last API row (2026-11-03) is the current, unfinished candle and is
  excluded.** Both the fetch time and the API's own `last` confirm this. That
  leaves **714 completed daily candles**, from 2024-11-19 to 2026-11-02.

## 3. Findings in the archive

**3.1 One 4-hour gap.** The candle starting **2025-11-01 16:00 UTC**
(16:00–20:00) is absent from `KASUSD_240.csv`.

- **No trades took place in that window.** The other five 4-hour candles of
  that day sum exactly to the archive's daily candle: 12 078 862.52646 KAS of
  volume and 3 620 trades. The data cannot say whether the market was quiet or
  the venue paused.
- **Handling: `no_price` only, never filled.** Under the pre-decided rule in
  spec §4, an event whose decision instant falls between 2025-11-01 20:00 and
  23:59 UTC is scored `no_price`, for every variant. Every other event is
  unaffected.
  - No price is substituted for the missing candle: no forward fill from an
    earlier candle, no back fill from a later one, and no daily close or API
    value in its place.
  - The candle is not reconstructed and is not counted as present.

**3.2 Consistency across intervals.** Aggregating the archive's 4-hour candles
by UTC day reproduces the archive's daily candle exactly, on all 681 days. This
covers open, high, low, close, volume and trades. It includes 2025-11-01,
where the aggregate has five candles.

## 4. Archive against API (cross-check only; sources are not mixed)

This comparison is a check of the archive. It does not change any archive
value, and nothing from the API file enters an R02 run (section 6).

- **681 overlapping days (2024-11-19 to 2026-09-30).** Open, high, low and
  close are identical on every day.
- **One difference, on 2026-09-10, in volume and trades only:**
  - API: 11 318 trades, volume 81 872 258.62850;
  - archive: 11 311 trades, volume 81 856 556.50880;
  - the API shows 7 more trades and 15 702.11970 KAS more volume. OHLC is
    identical.
- On that day the archive's own 4-hour candles sum to its daily candle, so the
  archive is internally consistent. Which source is right cannot be decided
  from these files.
- **The archive value stands.** The API value is recorded here as a finding
  only. It is not used, averaged or substituted.
- **No effect on R02.** The specification uses only closes: daily closes for
  the signal and 4-hour closes for the decision price. The closes are equal.
  Volume and trades are not used.

## 5. Fixed data window

- **The R02 data window ends 2026-09-30 (inclusive), the last day of the
  archive.** The last daily candle used is 2026-09-30. The last 4-hour candle
  used is the one starting 2026-09-30 20:00 UTC.
- **Newer data are not part of R02.** That includes October 2026 onward, even
  though the API file holds daily candles to 2026-11-02. Adding newer data
  requires a **new version of this registration**, and the change rule of spec
  §6 applies.
- **Length check against spec §6.** The window holds 681 daily candles. After
  the 90-day warm-up for H90, 591 days remain. P2's warm-up may overlap the end
  of P1, so P1 and P2 can each have at least 180 scored days. The window is
  therefore not `INSUFFICIENT EVIDENCE` by length alone. The P1/P2 boundary is
  still TBD (section 8).

## 6. Source roles

- **Primary source:** the Kraken OHLCVT archive files `KASUSD_240.csv` and
  `KASUSD_1440.csv`. Every R02 decision input comes from them.
- **Backup and cross-check only:** `KASUSD_1440_api_20261103.csv` and
  `raw_kasusd_1440.json`.
  - They are never mixed into a scored run.
  - They never fill an archive gap and never replace an archive value.
  - Why they exist: to keep the daily history from listing day after it leaves
    the public API's 720-entry window (around 2026-11-09), and to cross-check
    the archive (section 4).

## 7. Added backtest requirement (GT, 2026-11-03)

**`KAS(B) = 0` fails closed.**

- The `b*` score in spec §6, step 3, is the ratio
  `KAS accumulated by C / KAS accumulated by B`.
- If B accumulates zero KAS in any scenario or `d` value used for the score,
  the ratio is undefined. That `b` is treated as not qualifying and cannot be
  selected as `b*`. It is never given an infinite or default score.
- The tool's tests must include this case. If it leaves no qualifying `b`, the
  result is `NO CANDIDATE`.

## 8. Pre-registration checklist before P2

The list follows spec §6 and the sections it refers to.

- **Fixed** means the value is already in the merged specification. The
  section is cited.
- **Fixed here** means this document fixes it.
- **TBD** means the specification requires the value but does not give it. No
  value is proposed here.

| Item | Status | Value or source |
|---|---|---|
| File hashes | **Fixed here** | Section 1 |
| Data-validation report | **Fixed here** | Sections 2 to 4 |
| Data window end | **Fixed here** | 2026-09-30 inclusive (section 5) |
| Primary and backup source roles | **Fixed here** | Section 6 |
| Pair | Fixed | `KASUSD` (spec §4) |
| Candle semantics: stamp = start, use only once closed, missing 4-hour candle = `no_price` | Fixed | spec §4 |
| Signal inputs: H7, H30, H90 from completed UTC days; forward fill from earlier closes only; no backward fill | Fixed | spec §4 |
| Warm-up of 90 completed days with values before a period | Fixed | spec §4, §6 |
| Minimum of 180 scored days for each of P1 and P2 | Fixed | spec §6 |
| **P1 start and end** | **TBD** | Required by spec §6. Constraint: spec §4 and §6 (warm-up and length) |
| **P2 start and end** | **TBD** | Required by spec §6. Must follow P1 and end no later than 2026-09-30 (section 5) |
| `b` grid | Fixed | 0.5 U, 1 U, 2 U (spec §6) |
| P2 rule form and the `b*` selection procedure, including ties to the smaller `b` and `NO CANDIDATE` | Fixed | Form 1 (spec §6) |
| `KAS(B) = 0` handling | **Fixed here** | Fails closed (section 7) |
| `d` grid | **TBD** (confirm) | spec §6 says "proposed: 0 %, 2.5 %, 5 %, 10 %"; a proposal, not yet fixed |
| Cap parameters | Fixed as documented | spec §2, §3: `cap_mode='ohlc_h7'`, `cap_pct=0.20`, `cap_require_above_h90=true`, recorded from `STATUS.md` (SELECT of 2026-07-26), not re-read |
| Funding scenarios F1 and F2 | Fixed | spec §5: one lump; equal weekly deposits |
| **Headroom factor `K`** | **TBD** | spec §5 requires `K ≥ 1`; no value given |
| Cost model formula | Fixed | spec §5: decision price × (1 + fee rate + impact allowance) |
| **Fee rates** | **TBD** | spec §5 fixes the rule (production maker fee rate, with the taker rate for a sensitivity run). The numeric rates are not in the specification |
| **Impact allowance** | **TBD** | spec §5: a fixed number of basis points; no value given |
| **Order schedule** (decision times per day, Chicago) | **TBD** | spec §4: an input parameter; no value given |
| Limit on missing candles (data validation) | **TBD** | spec §4, §6 |
| Limit on `no_price` events per period | **TBD** | spec §4, §6 |
| Limit on `no_signal` events per period | **TBD** | spec §4, §6 |
| Longest allowed forward-fill run | **TBD** | spec §4, §6 |
| **Reserve bound** | **TBD** | spec §6, §7 (used in `b*` eligibility and in the decision rule); no value given |
| Decision rule and verdicts | Fixed | spec §7 |
| A′ spend-equality tolerance | Fixed | 1e-9 relative (spec §3) |
| `b*` tie tolerance | Fixed | 1e-9 relative (spec §6) |
| Price classes | Fixed | spec §7, including H90 × 0.90 |

**The registration is complete only when every TBD row has a value fixed by
Roberto,** committed as a new version of this document, before P2 is run.

**Not authorized by this document:** validator or backtest code, any database
change, a bonus size, and R03 or R04 work. Each needs Roberto's separate word.
