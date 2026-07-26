-- DCA cap (veto layer) — rebase the reference from our own execution mids
-- onto the project price standard: H7 = SMA of Kraken daily closes.
--
-- WHY: the legacy reference is AVG(mid) over our OWN dca_executions in the last
-- 7 days. Those are ~7 unevenly spaced points that sit on the recent low, so a
-- small uptick reads as "above cap". Backtest on 220d of KAS daily closes: the
-- legacy rule skips ~30% of ALL days, and 70% of those skips happened while the
-- price was BELOW H90 — cheap days, skipped. Without a carryover mechanism a
-- skip is not a saving, it is capital never deployed.
--
-- THE RULE (dca-bot-v2.3.md Phase 2 weights matrix, H7 veto row):
--   skip only if  mid > H7 * (1 + cap_pct)  AND  mid > H90
-- The H90 leg closes a blind spot of any 7-day mean: right after a crash a
-- violent bounce reads as far above H7 while still being far below H90. On 500d
-- of KAS history, 3 of the 4 days above H7 x 1.20 were below H90.
--
-- DEPLOY ORDER (non-strict): the bot code is SAFE WITHOUT this migration —
-- kraken_run reads these via settings.get(...) with legacy defaults, so a
-- missing column simply means "old behaviour". This migration adds the columns
-- with legacy defaults too; the behaviour change is the separate UPDATE below.
--
-- Additive only. Rollback / kill-switch: UPDATE dca_settings SET
-- cap_mode = 'exec_7d' WHERE id = 1;   (instant, no redeploy).

ALTER TABLE dca_settings
  -- 'exec_7d' = legacy (own execution mids) | 'ohlc_h7' = Kraken daily-close H7
  ADD COLUMN cap_mode text NOT NULL DEFAULT 'exec_7d',
  -- euphoria threshold above the reference (0.03 = legacy 3%, 0.20 = spec rule)
  ADD COLUMN cap_pct numeric NOT NULL DEFAULT 0.03,
  -- when true, also require price > H90 before skipping (crash-bounce guard)
  ADD COLUMN cap_require_above_h90 boolean NOT NULL DEFAULT false;

ALTER TABLE dca_settings
  ADD CONSTRAINT dca_settings_cap_mode_chk CHECK (cap_mode IN ('exec_7d', 'ohlc_h7'));

-- FLIP to the agreed rule (Roberto, 2026-07-26) — run when ready to go live:
--   UPDATE dca_settings
--      SET cap_mode = 'ohlc_h7', cap_pct = 0.20, cap_require_above_h90 = true
--    WHERE id = 1;
