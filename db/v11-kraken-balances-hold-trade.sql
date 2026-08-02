-- v11 -- the mirror stored what is held, never what is spendable
--
-- WHY. v10 added `kraken_open_orders` after 2026-07-30, when a maker leg was
-- refused with EOrder:Insufficient funds while the USD balance was, on paper,
-- more than the order needed: the money sat in a resting limit order. That fix
-- recorded the ORDERS. It did not record the consequence on the balance row,
-- so answering "was there spendable money at 06:53" still meant reading the
-- open-orders snapshot and reasoning about partial fills -- if a snapshot from
-- that minute happened to exist.
--
-- `hold_trade` is the same fact stated where it is needed. Kraken's BalanceEx
-- returns it per asset (docs.kraken.com, POST /private/BalanceEx), and
-- available = balance - hold_trade. The preflight has used BalanceEx since
-- 2026-07-30; only the mirror was still calling `Balance`, which has no such
-- field, and `balance_rows` dropped the value on the floor when it did arrive.
--
-- NULLABLE ON PURPOSE. `Balance` cannot supply it, and the snapshot writers
-- fall back to `Balance` when BalanceEx is unavailable rather than lose the
-- snapshot. NULL therefore means "not known at this snapshot", which is a
-- different statement from 0.0 and must not be written as one -- a zero here
-- would read as "nothing was held", the exact wrong answer on the day that
-- matters.
--
-- Rows written before this migration keep NULL. They were taken with `Balance`
-- and the held amount was never in the response.

alter table public.kraken_balances
  add column if not exists hold_trade numeric;

comment on column public.kraken_balances.hold_trade is
  'Amount committed to resting orders at snapshot time (Kraken BalanceEx). '
  'NULL = not known for this snapshot (taken via Balance, which omits it). '
  'Spendable = balance - coalesce(hold_trade, 0), and that coalesce is a '
  'GUESS whenever the column is NULL.';
