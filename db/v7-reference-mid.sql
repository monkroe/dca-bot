-- v7 — the reference mid that impact_bps / all_in_bps are measured against
--
-- WHY: dca-bot-v2.3.md §142 specified a reference join — "nearest snapshot
-- within 180s of the fill, marked mid_source='snapshot', else the run's ticker
-- mid marked 'ticker_fallback'". The snapshot side shipped (dca_mid_snapshots
-- has been collecting since 2026-02-21) but the JOIN never did. finalize_order
-- hardcoded mid_source='ticker_fallback' and measured bps against whatever mid
-- the row already carried — the T0 ticker mid, sampled minutes BEFORE the fill.
--
-- The cost is not cosmetic. impact_bps is supposed to mean "how much worse than
-- the market did we buy". Measured against a mid taken ten minutes earlier it
-- mostly reports how far the market drifted in those ten minutes. Recomputed
-- against the nearest snapshot, the 33 historical rows move by 2.0 bps on
-- average, up to 19.6 bps, and 6 of them change sign.
--
-- WHY A NEW COLUMN INSTEAD OF REUSING mid: `mid` is the T0 ticker mid and it is
-- the cap decision's evidence — over_cap replays as `mid > cap_price`. The bps
-- reference is a different observation at a different instant, so it gets its
-- own column. Overwriting `mid` would silently invalidate every cap replay.
--
-- ANCHOR: Kraken's own closetm (the instant the order actually closed), not
-- execution_finished_at (the instant a later cron cycle noticed). Measured over
-- the 33 historical rows: polling lag averages 17.7s and reaches 151.4s. The
-- window meant here is THIS join's +/-180s around the anchor -- not the cron
-- cycle, not the order's time window, not the cap reference -- so 151.4s is 84%
-- of its 180s half-width. It changes the chosen snapshot on only 1 of 33 rows,
-- but that row's impact moves 14.1 bps. closetm falls back to
-- execution_finished_at if absent.
--
-- WINDOW IS SYMMETRIC, and this is a choice the spec left open. §142 says
-- "nearest within 180s" without a direction. Symmetric means the reference can
-- be a snapshot taken up to 180s AFTER the fill, so the metric absorbs a little
-- post-trade drift. Backward-only was measured and is worse: 6 of 33 rows would
-- have no snapshot at all and fall back to the ~10-minute-old ticker mid,
-- trading a small bias for a large one. In the current data 26 of 33 references
-- precede the fill, 7 follow it, mean distance 17.4s.
--
-- ORDER OF DEPLOY: apply this migration BEFORE pushing dca-bot v1.5.0. These
-- columns are WRITTEN, so the bot cannot run against a schema that lacks them.
--
-- BACKFILL: unlike v6, history IS honestly recoverable here — the snapshots are
-- real observations already stored, not a reconstruction. All 33 rows carrying
-- impact_bps have a closetm and a snapshot inside the 180s window. The backfill
-- is db/v7-backfill-reference-mid.sql, applied separately and reviewed first.

alter table dca_executions
  add column if not exists ref_mid    numeric,
  add column if not exists ref_mid_ts timestamptz;

comment on column dca_executions.ref_mid is
  'The mid that impact_bps and all_in_bps are measured against: the mid of the dca_mid_snapshots row nearest the fill (Kraken closetm) within 180s, else the run''s ticker mid. mid_source says which. Distinct from `mid`, which is the T0 ticker mid the cap decision used. NULL = no reference was available and no bps were computed.';

comment on column dca_executions.ref_mid_ts is
  'When ref_mid was observed. Lets the 180s join rule be checked after the fact: abs(ref_mid_ts - closetm) <= 180s must hold whenever mid_source = ''snapshot''.';

comment on column dca_executions.mid_source is
  'Where ref_mid came from. ''snapshot'' = a dca_mid_snapshots row within 180s of the fill (the intended path). ''ticker_fallback'' = no snapshot that close, so the run''s ticker mid was used. NULL = no mid at all, bps not computed. Before v1.5.0 this column was hardcoded to ''ticker_fallback'' on every row regardless, and rows from that era are not distinguishable by this column alone — use ref_mid IS NULL to spot them.';
