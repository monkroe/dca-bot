-- v8 — one row per inspection cycle a maker leg is alive
--
-- WHY: the re-peg decision is computed every cycle and then thrown away. It
-- reaches a `print()` in `_maybe_repeg` and nothing else, so it lives only in a
-- GitHub Actions log that rotates. This week's decisions are already gone.
--
-- That makes the gap unlike every other open item: missing snapshot density
-- degrades FUTURE measurements, but a missing decision log DESTROYS data that
-- would otherwise exist. Every day without it is an observation lost for good,
-- not deferred.
--
-- WHAT IT UNBLOCKS. Until re-peg went live, "re-peg opportunity" and "fallback
-- day" were the same event, so the fallback count was a usable proxy for how
-- often the condition occurs. That proxy self-destructs now: from here an
-- opportunity ends in a re-peg and a maker fill, not a fallback. Without this
-- table, 14 days with zero fallbacks cannot distinguish "no opportunity arose"
-- from "re-peg quietly consumed them" — which is exactly the question the
-- acceptance deadline exists to answer.
--
-- ONE ROW PER CYCLE, NOT PER DECISION. The frequency question needs the
-- negatives: "the leg rested through 5 cycles and the bid was never above it"
-- is the measurement. Rows are therefore written on every path through
-- `_maybe_repeg`, including the early returns, so a cycle is never silently
-- absent. `ticks_above` is the distance in ticks between the best bid and our
-- resting price -- it makes near-misses visible, which a boolean never would:
-- 2026-07-21 and 2026-07-28 both sat at exactly 0.0 ticks, one tick short of
-- firing, and that is invisible in the executions table.
--
-- Telemetry only. The insert is wrapped so a failure can never block a fill.
--
-- ORDER OF DEPLOY: apply BEFORE pushing dca-bot v1.5.1 -- the table is WRITTEN.

create table if not exists dca_repeg_log (
  id                  bigserial primary key,
  ts                  timestamptz not null default now(),
  cl_ord_id           text        not null,
  dca_order_id        bigint,
  trade_date_chicago  date,
  pair                text,
  limit_price         numeric,
  bid                 numeric,
  ask                 numeric,
  ticks_above         numeric,
  repeg_count         integer,
  action              text        not null,
  detail              text
);

create index if not exists dca_repeg_log_day_idx
  on dca_repeg_log (trade_date_chicago, ts);
create index if not exists dca_repeg_log_cl_idx
  on dca_repeg_log (cl_ord_id, ts);

comment on table dca_repeg_log is
  'One row per maker-leg inspection cycle: the re-peg decision as it was made, including the cycles where nothing happened. Exists because the fallback count stopped being a usable proxy for re-peg opportunity the moment re-peg went live. Telemetry only -- never read by a decision.';

comment on column dca_repeg_log.ticks_above is
  'floor(best bid - our resting limit) in ticks at decision time. >= repeg_min_ticks is the fire condition; 0.0 is a near-miss one tick short. NULL = the cycle returned before market data was available (see action/detail).';

comment on column dca_repeg_log.action is
  '''repeg'' = fired; ''skip'' = evaluated and declined (detail carries the reason from _repeg_decision); ''not_evaluated'' = returned before the decision could be computed (disabled, partial fill, no order id, market data unavailable).';
