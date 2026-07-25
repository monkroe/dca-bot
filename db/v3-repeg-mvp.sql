-- DCA Phase 2 — Re-peg (bid-chase) MVP config.
-- Adds the three settings that gate and bound the maker-leg bid-chase.
--
-- DEPLOY ORDER (non-strict): the bot code is SAFE WITHOUT this migration —
-- kraken_run reads these via settings.get(...) with defaults, so a missing
-- column simply means "disabled". This migration only makes the toggle
-- persist in the DB so re-peg can be turned on.
--
-- Additive only. Rollback / kill-switch: UPDATE dca_settings SET
-- repeg_enabled = false WHERE id = 1;  (instant, no redeploy).
--
-- Ships DISABLED by default (repeg_enabled = false) — enable only after the
-- dry-run scenarios (repeg_fill / repeg_reject / repeg_cap) are validated.

ALTER TABLE dca_settings
  -- master switch; false = current static-limit behaviour (unchanged)
  ADD COLUMN repeg_enabled boolean NOT NULL DEFAULT false,
  -- max re-posts per maker leg per day (churn / rate-limit guard)
  ADD COLUMN repeg_max integer NOT NULL DEFAULT 5,
  -- only re-peg once best bid is >= this many ticks above our resting price
  ADD COLUMN repeg_min_ticks integer NOT NULL DEFAULT 1;

-- To enable later (after dry-run validation), e.g.:
--   UPDATE dca_settings SET repeg_enabled = true, repeg_max = 3 WHERE id = 1;
