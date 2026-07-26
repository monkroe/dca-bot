-- Low-balance warning threshold.
--
-- WHY: a funding gap does not fail loudly. The bot skips the day, and a
-- skipped day is never bought back because there is no carryover. That is
-- exactly how 2026-07-23 was lost (Kraken held $0.49 at the window). The
-- warning fires while there is still time to top up.
--
-- Notification ONLY -- it never changes a trading decision -- so unlike the
-- cap and re-peg switches this one ships ENABLED. The code default is 5 days
-- even without this migration; the column exists so the threshold is tunable
-- and can be switched off.
--
-- Semantics: warn when (Kraken USD balance / sum of enabled orders' daily
-- amounts) < low_balance_warn_days. Set to 0 to disable.
--
-- Additive only. Fires at most once per day: the balance preflight sits after
-- the day-unique claim insert, so later runs in the same window return at the
-- 409 before reaching it.

ALTER TABLE dca_settings
  ADD COLUMN low_balance_warn_days integer NOT NULL DEFAULT 5;

ALTER TABLE dca_settings
  ADD CONSTRAINT dca_settings_low_balance_warn_days_chk
  CHECK (low_balance_warn_days >= 0);
