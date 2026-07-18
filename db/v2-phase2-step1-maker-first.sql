-- DCA Phase 2 Step 1 schema (approved 2026-07-18, see robert-os-hub
-- docs/05-roadmap/dca-phase2-step1-state-machine.md).
-- DEPLOY ORDER (strict): this migration FIRST, then bot code with
-- order_strategy support. Additive only; rollback = order_strategy='market'.

-- 2.1 Strategy switch + maker fee rate
ALTER TABLE dca_settings
  ADD COLUMN order_strategy text NOT NULL DEFAULT 'market'
    CHECK (order_strategy IN ('market','maker_first')),
  ADD COLUMN maker_fee_rate numeric NOT NULL DEFAULT 0.004;

-- 2.2 Limit price audit trail + strategy-unit link (no FK by design:
-- deleting an order must not touch history; legacy rows stay NULL)
ALTER TABLE dca_executions
  ADD COLUMN limit_price numeric,
  ADD COLUMN dca_order_id bigint;

-- 2.3 Constraint-grade uniqueness on the STRATEGY UNIT (I2):
-- at most ONE maker_limit and ONE maker_fallback leg per day-order.
CREATE UNIQUE INDEX dca_exec_leg_per_event_uniq
  ON dca_executions (dca_order_id, trade_date_chicago, attempt_type)
  WHERE attempt_type IN ('maker_limit','maker_fallback');

-- 2.4 Open-limit inspection index (next-run phase)
CREATE INDEX idx_dca_executions_limit_open
  ON dca_executions (pair, trade_date_chicago)
  WHERE status = 'limit_open';
