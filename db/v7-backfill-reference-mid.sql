-- v7 BACKFILL — recompute impact_bps / all_in_bps against the reference mid
--
-- Run AFTER db/v7-reference-mid.sql. Separate file on purpose: this REWRITES
-- metrics that have already been reported, so it is reviewed and applied
-- deliberately, not as a side effect of a deploy.
--
-- WHY THIS IS HONEST TO BACKFILL (v6 was not): the numbers come from
-- dca_mid_snapshots — real observations recorded at the time, still stored,
-- never modified. Nothing is reconstructed or estimated. Verified before
-- writing: all 33 rows carrying impact_bps have a Kraken closetm AND a snapshot
-- inside the 180s window, so none are left half-converted.
--
-- WHAT MOVES: mean |delta| 2.0 bps, max 19.6 bps, 6 of 33 rows change sign.
-- The moves are systematic, not noise — the July maker-first rows read as
-- slightly negative impact (bought below the market) and become positive. That
-- was the stale reference flattering the maker leg: T0 mid was sampled ~10
-- minutes before a fill that rested at the bid while the market moved.
--
-- KNOWN LIMIT, deliberately not addressed here: snapshots are written once per
-- cron cycle (~5 min), so "nearest within 180s" can still be up to 180s away.
-- In this backfill the rows whose snapshot lands within seconds barely move
-- (2026-03-05, -03-06: 1s away) while the ones 97-148s away move the most. The
-- reference is now near the fill instead of ~10 min before it, which is the
-- fix; making it tight would need a snapshot taken AT fill time, which is a
-- separate decision about API budget.
--
-- REVERSIBLE: the pre-backfill values are recoverable from the row itself —
-- old_impact_bps = (avg_price / mid - 1) * 10000, since `mid` (the T0 ticker
-- mid) is untouched by this script and was the old reference.

begin;

with anchored as (
  select id, pair, avg_price, filled_quote_cost, fee_quote, filled_base_volume,
         coalesce(
           to_timestamp((((raw #>> '{}')::jsonb ->> 'closetm'))::float),
           execution_finished_at
         ) as anchor
  from dca_executions
  where impact_bps is not null
    and ref_mid is null            -- idempotent: never touches a converted row
),
joined as (
  select a.*,
         s.mid as ref_mid,
         s.ts  as ref_mid_ts
  from anchored a
  cross join lateral (
    select s.mid, s.ts
    from dca_mid_snapshots s
    where s.pair = a.pair
      and s.ts between a.anchor - interval '180 seconds'
                   and a.anchor + interval '180 seconds'
    order by abs(extract(epoch from (s.ts - a.anchor)))
    limit 1
  ) s
)
update dca_executions e
set ref_mid     = j.ref_mid,
    ref_mid_ts  = j.ref_mid_ts,
    mid_source  = 'snapshot',
    impact_bps  = round((((j.avg_price / j.ref_mid) - 1) * 10000)::numeric, 4),
    all_in_bps  = round((((((j.filled_quote_cost + j.fee_quote) / j.filled_base_volume)
                           / j.ref_mid) - 1) * 10000)::numeric, 4)
from joined j
where e.id = j.id;

-- Guard: every row that had bps must now carry a reference. If this fails the
-- transaction rolls back and nothing is half-converted.
do $$
declare missing int;
begin
  select count(*) into missing
  from dca_executions
  where impact_bps is not null and ref_mid is null;
  if missing > 0 then
    raise exception 'backfill incomplete: % row(s) still have impact_bps without ref_mid', missing;
  end if;
end $$;

commit;

-- Verification (run after commit):
--   select trade_date_chicago, mid_source,
--          round(ref_mid, 6) as ref_mid,
--          round(abs(extract(epoch from (ref_mid_ts - execution_finished_at)))::numeric, 0) as secs_from_observed,
--          round(impact_bps, 1) as impact_bps,
--          round(((avg_price / mid - 1) * 10000)::numeric, 1) as impact_under_old_reference
--   from dca_executions
--   where impact_bps is not null
--   order by trade_date_chicago desc;
