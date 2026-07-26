# DCA Bot -- CHANGELOG

Conventions: dates are **Chicago** time (the bot's trading timezone); a "vakaras" entry may already be the next day in UTC. No concrete money amounts in this file -- rates, mechanisms, dates and counts only; amounts live in the DB and the bot's own messages. Strategy/design rationale lives in `robert-os-hub/docs/05-roadmap/dca-phase2-*.md`; this file records what shipped.

History before 2026-07-18 (Phase 1 -- Kraken + Strike execution, notifications, reconciliation, impact/all-in bps telemetry) is in `git log`; this changelog starts at Phase 2.

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
