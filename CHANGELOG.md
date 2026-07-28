# DCA Bot -- CHANGELOG

Conventions: dates are **Chicago** time (the bot's trading timezone); a "vakaras" entry may already be the next day in UTC. No concrete money amounts in this file -- rates, mechanisms, dates and counts only; amounts live in the DB and the bot's own messages. Strategy/design rationale lives in `robert-os-hub/docs/05-roadmap/dca-phase2-*.md`; this file records what shipped.

History before 2026-07-18 (Phase 1 -- Kraken + Strike execution, notifications, reconciliation, impact/all-in bps telemetry) is in `git log`; this changelog starts at Phase 2.

## 2026-07-28 (antradienis -- Chicago, session 21)

### VALIDATION -- cap telemetry confirmed on its first live day
- 2026-07-28 is the FIRST row carrying `h7` / `h30` / `h90` / `cap_price` together. The 07-27 row is NULL on all four and that is correct, not a regression: v1.4.2 and v1.4.3 were committed at 07:23 and 07:35 CT, and that day's execution had already finished at 06:58 CT. Yesterday's fill ran on the previous code
- The day bought on the maker leg, below both the cap and H90 -- `over_cap` false, `over_h90` false, reconstructed by arithmetic from the row alone, exactly as v1.4.3 intended

### fix(telemetry): the OHLC context is written once, at T0 -- v1.4.4
- **Why**: the first live row failed its own invariant. `cap_price / h7 - 1` read **20.0243%** for a 20% cap. Root cause: `cap_price` and `h90` are written at T0, but `h7` / `h30` were written at FILL time -- and since the maker-first cutover the fill is observed by a later cron cycle, a fresh process, which refetched OHLC. On 07-28 the two fetches sat 10 minutes apart (`execution_started_at` 06:53:19, `ohlc_ts` 07:03:21) and H7 had moved. The stored anchor was not the anchor the cap was computed from
- The decision itself was never affected: `mid`, `cap_price` and `h90` all come from the same T0 fetch, so `skip <=> mid > cap_price AND (guard off OR mid > h90)` held throughout. Only the reconstruction path was wrong, and only through `h7`
- T0 now writes `h7` / `h30` / `ohlc_ts` in the same run that derives `cap_price` from them, placed OUTSIDE the cap branch so `force` runs and reference-less days still carry market context
- `finalize_order()` reads `h7` on the terminal-guard query it already made -- no extra round trip -- and when it is present, reuses the stored context instead of refetching. That also drops one public OHLC call per fill. Rows written before this version still refetch, so nothing regresses for them
- The fill notification now reports the numbers stored on the row, so the message and the row can no longer disagree
- **Telemetry only, cannot alter a decision** -- hence patch
- Validation: 23 hermetic branches driving the REAL `finalize_order` (T0 context reused, payload carries no `h7`/`h30`/`ohlc_ts`, `cap_price / h7 - 1 == cap_pct` to 1e-12, legacy NULL row still refetches and writes, Kraken OHLC outage writes NULL and still fills, Mid present/absent, message shape, terminal row untouched) -- all pass; `test.sh` green

### feat(ux): notifications read as a financial system, not as a log line
- **Why**: Roberto's review. Two messages carried text that only made sense to whoever wrote the code
- **Fill**: pair renders as `KAS/USD`, not `KASUSD` (`pair_label()`, display only -- never fed back to Kraken). Market context becomes its own block, separated from the settlement figures by a blank line: what was paid above, what the market looked like below. Every line drops out when its data is missing -- no `N/A`, no `unknown`, no placeholders
- **`Mid:` now shows the price, not a source label.** It used to print `mid_source`, which has exactly ONE non-NULL value in the entire codebase (`kraken_run.py:717`, hardcoded `"ticker_fallback"`) -- confirmed against the DB: 33 rows `ticker_fallback`, 95 NULL, nothing else. A constant carries no information and read like a leaked variable name. No mid, no line
- **Low balance** rewritten in full Lithuanian with diacritics, bulleted, with the ask and the consequence in a closing sentence. Kept at one decimal (`~4.9 d.`) rather than rounding: the threshold is 5 days, and a message reading `~5 d. (riba – 5 d.)` would look like it contradicts itself
- Emoji unchanged -- one status icon per message, from `ICONS`, no additions
- `strike_run.py` deliberately untouched: it has not executed since 2026-04-25 and its `mid_source` does have a real label. Left for whenever Strike comes back

### feat(telemetry): record the re-peg decision before it is lost -- v1.5.1
- **Why, and why it could not wait**: `_maybe_repeg` computed the decision every inspection cycle and sent it to a `print()`. That lives in a GitHub Actions log which rotates, so **this week's decisions are already gone**. Unlike every other open item, this one does not degrade future precision -- it destroys data that would otherwise exist. Each day without it is an observation lost permanently, which is why it shipped ahead of the acceptance deadline it exists to serve
- **The measurement it rescues.** While re-peg was disabled, "re-peg opportunity" and "fallback day" were the same event, so the fallback count worked as a proxy -- and indeed both historical candidates (07-24 at +1 tick, 07-25 at +3 ticks) ARE the two fallback days. That proxy self-destructs now re-peg is live: from here an opportunity ends in a re-peg and a maker fill, not a fallback. Without this table, 14 days with zero fallbacks could not distinguish "no opportunity arose" from "re-peg quietly consumed them" -- exactly the question the deadline is meant to settle
- **One row per CYCLE, not per decision.** Written on every path through `_maybe_repeg` including all six early returns, so a cycle is never silently absent: the negatives ("rested through five cycles, bid never above the limit") are the frequency measurement. `ticks_above` keeps near-misses visible, which a boolean cannot -- 07-21 and 07-28 both sat at exactly 0.0 ticks, one tick short of firing, invisible in `dca_executions`
- Migration `db/v8-repeg-probe-log.sql` (new table `dca_repeg_log`), applied to Benas AI before the push. No new API calls -- `ticker` and `pair_info` were already fetched on that path. The insert is wrapped: a logging failure can never keep a leg from being re-pegged or a day from being bought
- Telemetry only, decision untouched -- hence patch
- Validation: 23 hermetic branches (fire, near-miss at 0.0 ticks, bid below limit, spread collapse, all six early returns, NULL ticks when market data is unavailable, identity columns, and an exploding insert that must not raise) plus 2 asserting `_repeg_decision` itself is unchanged -- all pass; the 24 v1.5.0 and 23 v1.4.4 branches still pass; `test.sh` green

### CORRECTION -- "opportunities became rarer by construction" was wrong
- Stated earlier the same day that widening `time_window_minutes` 15->30 reduced re-peg opportunities. **It increases them.** Traced in code: `deadline = window_end - CRON_CYCLE_MINUTES` and `_window_bounds_for` gives `target +/- w/2`, so maximum rest is `w - 5` -- 10 min under the old window, 25 min under the new. That is 2.5x more exposure, not less
- The 10-minute figure is confirmed by the data: both fallback days rested exactly 9.9 min. That constant is derived FROM the window, so widening did move it
- Roberto's reasoning, which the code then confirmed: on a day the bid rises, waiting does not help the leg fill -- it only delays the fallback. So widening cannot reduce fallbacks on precisely the days that are re-peg opportunities. Zero fallbacks since 07-26 is the market, not the configuration
- The claim reversed twice in one session. The sequence was intuition -> methodology with a guessed timeout -> verified fact, so the reason it moved is `deadline = window_end - 5`, not indecision

### feat(telemetry): the reference join, four months late -- v1.5.0
- **Why**: `dca-bot-v2.3.md:142` specified it -- nearest `dca_mid_snapshots` row within 180s of the fill, marked `mid_source = 'snapshot'`, else the run's ticker mid as `'ticker_fallback'`. The snapshot side shipped and has been collecting since 2026-02-21 (49,213 rows, 20 pairs). **The join never did.** `finalize_order` hardcoded `'ticker_fallback'` and measured bps against the mid already on the row -- the T0 ticker mid, sampled ~10 minutes BEFORE the fill. Found while checking whether the constant could be dropped from the notification
- The damage is to meaning, not to trading. `impact_bps` is supposed to say "how much worse than the market did we buy"; against a ten-minute-old reference it mostly reported how far the market drifted in those ten minutes
- `resolve_reference_mid()` does the join and returns `(ref_mid, mid_source, ref_mid_ts)`. A lookup failure or an empty window degrades to the old ticker mid -- this is telemetry and must never be able to block a fill
- **Anchored on Kraken's `closetm`**, the instant the order actually closed, NOT `execution_finished_at`, the instant a later cron cycle noticed. Measured over the 33 rows: polling lag averages 17.7s but reaches **151.4s**. The window meant here is the **snapshot join window of this change, +/-180s around the anchor** -- no other window (the cron cycle, the order's time window, the cap reference) is involved -- so 151.4s is 84% of its 180s half-width, spent on our own polling, which would drag the window most of the way off the event it describes. In practice it changes which snapshot is picked on only **1 of 33** rows -- but that row's impact moves by 14.1 bps, so the cheap correctness is worth taking. Falls back to `execution_finished_at` when `closetm` is absent
- **The window is symmetric on purpose, and the spec did not say so.** §142 says "nearest within 180s" without a direction; taking it symmetrically means the reference can be a snapshot up to 180s AFTER the fill, so the metric absorbs a little post-trade drift. The alternative -- backward-only -- was measured and is worse: **6 of 33** rows would lose their reference entirely and fall back to the ~10-minute-old ticker mid, trading a small bias for a large one. As it stands 26 of 33 references precede the fill and 7 follow it, mean distance 17.4s
- New columns `ref_mid` / `ref_mid_ts` (migration `db/v7-reference-mid.sql`). **`mid` is deliberately NOT reused**: it is the T0 ticker mid and it is the cap decision's evidence -- `over_cap` replays as `mid > cap_price`. Overwriting it would silently invalidate every cap replay. **DEPLOY ORDER MATTERS**: these columns are WRITTEN, so migrate before the code ships
- `mid_source` finally carries information, so the notification uses it again -- but as plain words, not the enum: an unqualified `Mid:` line means the market at the fill, `Mid: $x (run ticker)` means no snapshot was close enough and the bps are a weaker claim
- **Not a patch**: this changes what two shipped metrics mean. Minor, though not major -- no buying decision is touched, and the cap path never read these columns
- Validation: 24 hermetic branches driving the REAL `finalize_order` and `resolve_reference_mid` (snapshot beats T0 mid, nearest wins, 180s boundary on both sides, wrong pair excluded, query bounded, no-snapshot fallback labelled, no reference at all leaves bps NULL and still fills, lookup exception degrades, missing `closetm`, `mid` column untouched, both message shapes) -- all pass, plus the 23 v1.4.4 branches as regression; `test.sh` green

### BACKFILL prepared, not applied -- `db/v7-backfill-reference-mid.sql`
- Unlike v6, history here IS honestly recoverable: the snapshots are real observations already stored, not a reconstruction. Verified before writing -- all 33 rows carrying `impact_bps` have both a `closetm` and a snapshot inside the window, so none can be left half-converted
- Mean |delta| 2.0 bps, max 19.6 bps, **6 of 33 rows change sign**. The moves are systematic: the July maker-first rows read as slightly negative impact and become positive. The stale reference was flattering the maker leg, which matters because Phase 2 acceptance is partly judged on it
- Idempotent (`where ref_mid is null`), wrapped in a transaction with a post-condition that aborts on any row left behind, and reversible -- the old value is recomputable as `(avg_price / mid - 1) * 10000` since `mid` is untouched
- **Not applied.** It rewrites reported metrics; that is Roberto's call

### OPEN -- the reference is near the fill, but not at it
- Snapshots are written once per cron cycle (~5 min), so "nearest within 180s" can still be up to 180s away. In the backfill the rows whose snapshot lands within seconds barely move (2026-03-05, -03-06: 1s away, ~0 delta) while the ones 97-148s away move the most -- so some of the remaining number is still reference staleness, not execution quality
- Closing that gap means taking a snapshot AT fill time, which is a decision about Kraken API budget, not a bug fix. Filed

## 2026-07-27 (pirmadienis -- Chicago, session 19)

### feat(telemetry): cap decisions reconstructible from the row -- v1.4.3
- **Why**: closes the OPEN item raised the same day. `dca_executions` stored `h7` / `h30` / `mid` but neither the 90-day floor the guard reads nor the threshold actually applied. A skip row explained itself only in free text; a BUY row said nothing about the cap at all, so "did the H90 guard save this day?" was unanswerable after the fact
- `cap_telemetry()` returns the two columns; the T0 check writes them on **BOTH** outcomes, so a normal buy now carries its cap evidence too
- A stored row replays the decision by arithmetic, no text parsing: `skip <=> mid > cap_price AND (guard off OR mid > h90)`. The mode is inferable as well -- under `ohlc_h7`, `cap_price / h7 = 1 + cap_pct` exactly, which does not hold for the legacy exec-mid reference
- **Scope limit, deliberate**: the columns describe the **T0** decision. The DP-5 fallback re-check and the re-peg guard run in later cycles against a fresher mid; writing their numbers onto the same row would pair a T0 `mid` with a later `cap_price` and read as a contradiction. Those paths stay auditable through their existing enum `reason` markers, which are unchanged
- Migration `db/v6-cap-telemetry.sql` (`h90`, `cap_price`, both nullable, with column comments). **DEPLOY ORDER MATTERS**: unlike v4/v5 these columns are WRITTEN, so the migration must be applied BEFORE the code ships. Applied to Benas AI and verified before push
- Not backfilled: the legacy cap reference averaged rows that have since changed, so historical `cap_price` is not honestly recoverable. Pre-migration rows stay NULL, which reads correctly as "not recorded"
- Telemetry only -- cannot alter a decision, hence patch
- Validation: 10 added branches (threshold arithmetic, missing/zero reference, missing H90, legacy pct) including 3 replay tests that assert a stored row reproduces `cap_decision`'s verdict on the live 07-27 numbers, a crash-bounce and a euphoria day -- 42 cap branches total, all pass; 32 finalize branches still pass; `test.sh` green

### fix(telemetry): market context restored on cross-run fills -- v1.4.2
- **Why**: `h7` / `h30` / `ohlc_ts` have been NULL on every execution since 2026-07-18, and the fill notification lost its `H7 | H30` line. Root cause: `finalize_order()` writes those from `ohlc_ctx`, but of its 8 call sites only the same-run T0 path passed one. After the maker-first cutover a fill is almost always finalized by a LATER cron cycle -- a fresh process -- so the context was gone. Introduced by Step 2 (session 16), surfaced by reviewing the first post-cap live fill
- `finalize_order()` now resolves the pair and lazily calls `get_ohlc_ctx(pair)` when no context was handed in. Cached per run, so the T0 path still does not refetch and the inspection path costs at most one public OHLC call
- Pair resolution prefers the pair encoded in `cl_ord_id` (OUR config string) over Kraken's `descr.pair`, which may return an exchange alias; `descr.pair` stays as fallback. Also removes a latent `None.replace()` on the notification path when neither source resolved
- Fill notification now carries `H7 | H30 | H90` at the same precision as its Price line. H7 is the cap anchor and H90 the guard, so both belong there now that the veto reads them; H30 has no decision weight but the ORDER of the three reads the trend at a glance (`H7 < H30 < H90` = drifting down), which no single number does. `h90` is NOT stored as a column -- see the open item below
- **Telemetry only, cannot alter a decision** -- hence patch. The cap has always fetched its own context in whichever process it runs; it was never affected. Kraken OHLC unavailable still writes NULL and never blocks a fill
- Validation: 32 offline branches driving the REAL `finalize_order` with hermetic stubs (cross-run path, `-fb` and `-r1` leg ids, handed-in context not refetched, empty context, H7-only and H90-only contexts, notification order, unparseable id, no pair at all, exchange alias) -- all pass; the 32 cap/low-balance branches still pass; `test.sh` green

- Raised the "cap decisions are not reconstructible from the DB" gap here; CLOSED the same day by v1.4.3 above

### VALIDATION -- cap rule confirmed on its first live day
- First execution under `cap_mode='ohlc_h7'` BOUGHT, maker leg, at the maker fee rate. The day sat ~3% above H7 but ~7% BELOW H90 -- a cheap day by the 90-day trend
- The legacy rule, recomputed from the same `dca_executions` rows it would have read, put its cap BELOW the day's mid: **07-27 would have been a second consecutive skip, again on a cheap day.** This is the live confirmation of the backtest diagnosis
- The exec-mid bias assumed in the session 18 backtest (ref ~= H7 x 0.990) reproduced exactly on this second independent observation
- Re-peg still NOT proven live: the maker leg filled inside the first inspection cycle at its original limit, so there was no drifting bid to chase. Needs a day that buys but misses the first maker fill

## 2026-07-26 (sekmadienis -- Chicago, session 18)

### feat(ops): low-balance warning -- v1.4.1
- **Why**: a funding gap does not fail loudly. The bot skips the day, and a skipped day is never bought back because there is no carryover. That is exactly how 2026-07-23 was lost (Kraken held $0.49 at the window). Open idea since then; built now
- `warn_if_low_balance()`: after the balance preflight confirms the day is covered, compare the balance against the daily burn and send a Telegram warning when fewer than `low_balance_warn_days` days of buys remain
- **Daily burn = SUM of every enabled order's amount**, not just the order being executed -- computed once in `main()` and passed into `execute_pair`, so the figure stays correct if more orders are added
- **Notification only.** It never changes a trading decision, so unlike the cap and re-peg switches it ships ENABLED (default 5 days). Code default holds even without the migration; the column exists to tune it or switch it off with 0
- Fires at most once per day by construction: the balance preflight sits AFTER the day-unique claim insert, so later runs in the same window return at the 409 before reaching it. Skipped entirely in `dry_run`, and not emitted when the balance already fails the day (that path has its own alert)
- Config in `dca_settings` (migration `db/v5-low-balance-warning.sql`): `low_balance_warn_days` (default 5, 0 = off, CHECK >= 0)
- Validation: 10 added branches (above/at/below threshold, disabled, custom threshold, missing column, zero burn, garbage value, multi-order burn) -- 32 offline branches total, all pass; `test.sh` green

### feat(cap): veto layer rebased onto the H7 daily-close standard -- v1.4.0
- **Why**: the cap reference was `AVG(mid)` over our OWN `dca_executions` from the last 7 days -- ~7 unevenly spaced points that sit on the recent low, so a small uptick read as "above cap". Backtest over 220d of KAS daily closes: the legacy rule skips **~22% of ALL days**, and **62% of those skips happened while the price was below H90** (500d: 28% skipped, 59% of them cheap). Same pattern on 8 other pairs (16-25% skipped, 63-95% of skips cheap), so this was never KAS-specific
- **The rule** (`dca-bot-v2.3.md` Phase 2 weights matrix, H7 veto row + "KAINU DEFINICIJOS" price standard): skip only if `mid > H7 * (1 + cap_pct)` **AND** `mid > H90`, where H7/H90 are SMAs of **Kraken daily closes**, not our own fills
- **Why the H90 leg**: any 7-day mean has a structural blind spot -- straight after a crash a violent bounce reads as far above H7 while still sitting far below H90, i.e. exactly the cheap day an accumulator wants. On 500d of KAS, 3 of the 4 days above `H7 x 1.20` were below H90. The guard adds no tunable number (H90 is already computed every run) and can only PREVENT skips, never cause them
- **Why a near-inert veto is correct**: the aggressive cap did improve average cost per dollar, but without a carryover mechanism a skip is not a saving -- it is capital never deployed (220d: ~29% fewer units accumulated for ~2% better price). Carryover + weight sizing is spec §2, i.e. layer 2-3, not this change
- `cap_decision()`: PURE veto shared by all three call sites (T0 check, DP-5 fallback re-check, re-peg guard) so they can never drift apart. Missing reference or missing H90 = NO skip (DP-4: the day must never end unbought while funds exist)
- `get_cap_context()` routes by mode; `get_ohlc_ctx()` caches the OHLC fetch per run (T0 hands in the context it already built for Phase 1.5 telemetry)
- `_repeg_decision()` now takes `h90` + cap params and calls the same `cap_decision`; dry-run scenario `repeg_cap` re-tuned so its veto still fires under BOTH cap modes
- Config in `dca_settings` (migration `db/v4-cap-h7-ohlc.sql`): `cap_mode` (`exec_7d` default | `ohlc_h7`), `cap_pct` (0.03 default), `cap_require_above_h90` (false default). Code is safe WITHOUT the migration -- missing columns read as legacy. Kill-switch: `cap_mode='exec_7d'`, instant, zero deploy
- Ships with LEGACY defaults: deploying changes nothing until the flip UPDATE is run

### VALIDATION STATUS -- cap rule
- Covered: 22-branch offline test of `cap_decision` / `cap_params` / `get_cap_context` / the `_repeg_decision` cap leg (real 07-26 numbers, the 2025-11-26 crash-bounce shape, exact cap and H90 boundaries, missing-data paths, legacy-mode regression) -- all pass; `test.sh` green
- Covered: 220d and 500d backtests on 9 pairs against Kraken daily closes, driven through the LIVE `ohlc.py` functions
- NOT covered: a live run under `cap_mode='ohlc_h7'`. Backtest price proxy is the daily close (00:00 UTC) while the real buy happens inside the configured execution window, so intraday spikes between closes are invisible and the true veto-zone count could be slightly higher
- The legacy skip rates above model the exec-mid reference as H7 x 0.990 -- the ratio measured on the live 2026-07-26 skip row (ref $0.027961 vs H7 $0.028241). One observation, so treat the exact percentage as approximate; the direction and the "most skips were cheap" finding hold at any plausible bias
- Strike (`strike_run.py`) carries the SAME legacy cap and is NOT touched by this change (dormant: zero enabled orders). BTC is bought via Strike, so this fix does not reach BTC until Strike gets the same treatment

## 2026-07-25 (diena + vakaras -- Chicago; UTC jau 07-26, session 17)

### feat(maker): re-peg (bid-chase) MVP -- v1.3.0 (0e429ec)
- **Why**: the maker limit was STATIC -- posted once at the window-start bid and never re-priced. On 07-24 and 07-25 the KAS bid drifted up/flat, the resting order was left behind the book and never filled, so both days ended in a taker fallback (0.800% instead of 0.400%). Anticipated in the roadmap doc under "if bid-pegging proves too passive"
- `_repeg_decision()`: PURE decision function (no I/O), shared by the live path and the dry-run harness so both exercise identical logic
- `_maybe_repeg()`: live path, hooked into `run_maker_inspection`'s "open, waiting" branch. Deadline / fallback / TTL logic untouched
- Guards: best bid must exceed our resting price by >= `repeg_min_ticks`, must stay maker (`bid < ask`), must be under the same 7D cap the fallback respects, and `repeg_count < repeg_max`
- Scope (MVP): **zero-fill legs only**. A partially filled leg keeps the existing deadline -> fallback path; partial-aware re-peg deferred
- Crash safety (claim-first, preserves the DP-3 no-double-buy discipline): cancel + confirm zero-fill, then park the row as `claimed` / `order_id` NULL / `raw.kraken_cl` = the NEXT client id BEFORE AddOrder. Reconciliation now searches `raw.kraken_cl`, so a crash mid-repeg restores the resting order instead of orphaning it
- Repost rejected by post-only (would cross): the old leg is already canceled zero-fill, so the event resolves straight to a taker fallback for the full budget -- mirrors the initial post-only-reject path
- `raw` on a successful repost nests the Kraken result under `last_result` instead of overwriting -- otherwise `repeg_count` would reset every cycle and the max-count guard would never bind
- Dry-run harness: scenarios 7-9 (`repeg_fill` / `repeg_reject` / `repeg_cap`) appended to SCENARIO_SEQUENCE
- Config in `dca_settings` (migration `db/v3-repeg-mvp.sql`): `repeg_enabled` (default false), `repeg_max`, `repeg_min_ticks`. The code is safe WITHOUT the migration -- missing columns read as defaults, i.e. disabled. Kill-switch: `repeg_enabled=false`, instant, zero deploy
- Shipped default-OFF, then ENABLED live 07-25 evening with a conservative `repeg_max`

### VALIDATION STATUS -- re-peg is NOT yet proven live
- Covered: 11-branch unit test of `_repeg_decision` (trigger, min_ticks, spread collapse, cap incl. exact boundary, max count, ordermin, no-history) -- all pass; the 3 new scenarios driven through the real `_inspect_dry_limit` with hermetic stubs (no prod DB, no Kraken, no `dry_run` flip, so live buying was never paused) -- all pass; `test.sh` green
- NOT covered: **a real live fill.** 07-26 was skipped by the 7D cap, so re-peg has still never fired against Kraken. First real proof needs a day that BUYS but misses the maker fill. The Phase 2 acceptance verdict must not count it as validated before then
- Deliberate tradeoff: the in-bot dry-run harness writes to prod and needs `dry_run=true`, which would halt real buying -- offline validation was chosen instead

### config: live order window widened (no deploy)
- `dca_orders.time_window_minutes` for the live order 15 -> 30, so the maker leg rests ~25 min instead of ~10 (`deadline = window_end - CRON_CYCLE_MINUTES`; the last cycle stays reserved so a fallback still lands inside the window, per I6). Stopgap for the static-limit problem, not a fix

### KNOWN GAP -- 7D cap mis-fires in the current regime (2026-07-26)
- The cap's reference is a 7-day mean, which currently sits on the recent low, so a small uptick reads as "above cap" and skips a buy that is cheap against the 30d / 90d / all-history mean and far below cost basis
- Constraint on the fix: a static price anchor is wrong -- it would stop buying if the market recovers. The anchor must be trend-relative
- Agreed follow-up (not started): (1) recurring daily OHLC append so `dca_ohlc_history` becomes a live uniform daily series -- today it is a ONE-TIME backfill by design (run 07-17 to fill the no-purchase gap), static, not broken; (2) cap rebased onto a long-horizon anchor, skipping only on genuine euphoria. `dca_mid_snapshots` is live and unbroken since February but is recency-weighted, so it needs daily resampling to be an honest long-horizon input

## 2026-07-19 (session 16 tesinys)

### fix: dry run must not depend on real funds (258e7a7)
- The balance preflight gated dry runs on REAL Kraken funds, which killed the first Step 3 evidence attempt; in dry mode the check is now informational only

### Step 4 -- LIVE CUTOVER to maker-first
- `order_strategy='maker_first'`, `dry_run=false` (Roberto GO). First live maker fill 07-20. Rollback = one UPDATE back to `'market'`

## 2026-07-18 (session 16)

### feat: Phase 2 Step 2 -- maker-first execution, limit + market fallback -- v1.2.0 (62e4519)
- Post-only limit leg at best bid (`oflags` incl. `post`), cross-run open-order inspection (the cron cadence IS the timer), cancel -> confirm -> readback before any fallback, fallback sized by the REMAINING quote budget so the per-event money invariant `cost + fee <= budget` holds by construction
- Deployed DORMANT in strict order: migration first, then code, with `order_strategy='market'` so live behaviour was unchanged until the cutover

### fix: review hardening (070a4ac)
- Reconciliation race re-check (closed -> open -> closed): a limit that filled between the ClosedOrders miss and the OpenOrders miss would otherwise have been marked failed with real money spent
- `finalize_order` terminal guard; reason semantics verified (reason NULL = fallback decision pending, reason set = event terminal)

### test: Step 3 harness (290d829)
- Reviewer-ordered, auto-advancing scenario sequencer: fill100, nofill, partial40, post-only reject, crash-after-cancel, crash-after-fallback-submit. Evidence window passed 6/6 with zero double-buys and the money invariant exact to the cent
